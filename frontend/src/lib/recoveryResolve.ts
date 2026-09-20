/**
 * W3：管理员恢复/退场流程（`wps_recovery_resolve`）的纯逻辑层。
 *
 * 契约见 `docs/BRIDGE-WPS-CONTRACT-R6.md` §7。前端必须如实呈现：
 *
 * - 这个入口**永不写云端**（`cloud_write=false`），只改本地 journal 审计；
 * - `retire_guarded` 只是「带审计退场」，**同一 target_date + 云表的防重复闸门仍在**：
 *   未知写入结果不会因为退场/归档变成可以自动重传；
 * - 失败时保留阻断；不提供「删除账本」「直接重传」「强制清除 uncertain」捷径；
 * - 刷新状态是只读的，**不得**自动触发恢复写入；
 * - 重复提交同一决策是幂等的（`already_retired` / `changed=false`），不重复写盘。
 *
 * 纯逻辑模块，Node 测试可直接覆盖（`recoveryResolve.test.ts`）。
 */
import type {
  WpsRecoveryDecision,
  WpsRecoveryOperation,
  WpsRecoveryResolvePayload,
  WpsRecoveryResolveResult,
} from './bridge.ts'
import { CONCURRENT_TASK_TEXT, journalIssueOf } from './wpsFailure.ts'

/** 内部 operation_id 格式：`wps-<16 位小写 hex>`。 */
export const OPERATION_ID_PATTERN = /^wps-[0-9a-f]{16}$/

/** 恢复面板在任何情况下都不允许自动恢复写入。 */
export const RECOVERY_AUTO_RESUME_ALLOWED = false

/**
 * 恢复提交的单飞 + 乱序保护。实现见 {@link SingleFlightGate}；
 * 这里保留 `ResolveGate` 这个名字给恢复流程使用（同一语义，便于阅读）。
 */
export { SingleFlightGate as ResolveGate } from './singleFlight.ts'

export interface DecisionSpec {
  decision: WpsRecoveryDecision
  label: string
  /** 用户可见的风险说明（必须展示）。 */
  risk: string
  /** 这个决策实际会改什么。 */
  effect: string
  /** `retire_guarded` 需要勾选「已人工核对云端表结构」。 */
  requiresStructureCheck: boolean
  /** 决策后防重复闸门是否仍然保留。 */
  guardRetained: boolean
  /** 决策后是否允许自动重传（恒为 false）。 */
  autoRetryAllowed: false
  /** 决策后的下一步。 */
  nextActionLabel: string
}

export const DECISION_SPECS: Record<WpsRecoveryDecision, DecisionSpec> = {
  retire_guarded: {
    decision: 'retire_guarded',
    label: '带审计退场（保留防重复闸门）',
    risk: '只把旧任务带审计地退出全局 pending；同一「目标日期 + 云表」的防重复闸门仍然生效，之后的写入计划会被以 uncertain + manual_reconcile 拒绝。退场不等于可以重传。',
    effect: '本地 journal 写入审计记录；不写云端；防重复闸门保留。',
    requiresStructureCheck: true,
    guardRetained: true,
    autoRetryAllowed: false,
    nextActionLabel: '继续人工只读核对云端（manual_reconcile）',
  },
  cloud_verified: {
    decision: 'cloud_verified',
    label: '只读核对云端：确认已完成',
    risk: '重新只读云端并按实际证据判定；证明不了就失败（cloud_verify_failed），不是强制清除。',
    effect: '只读云端 + 本地 journal 审计；不写云端。',
    requiresStructureCheck: false,
    guardRetained: false,
    autoRetryAllowed: false,
    nextActionLabel: '按判定结果决定是否重新预览',
  },
  cloud_untouched: {
    decision: 'cloud_untouched',
    label: '只读核对云端：确认完全未执行',
    risk: '同样需要云端证据；证明不了就失败（cloud_not_untouched），不得当作可以重传。',
    effect: '只读云端 + 本地 journal 审计；不写云端。',
    requiresStructureCheck: false,
    guardRetained: false,
    autoRetryAllowed: false,
    nextActionLabel: '确认未执行后重新预览（repreview）',
  },
  keep: {
    decision: 'keep',
    label: '只记录审计备注（保持阻断）',
    risk: '只写审计备注，不改变任何阻断状态；这是最保守的选项。',
    effect: '本地 journal 写入审计备注；不写云端；阻断保持。',
    requiresStructureCheck: false,
    guardRetained: true,
    autoRetryAllowed: false,
    nextActionLabel: '继续人工只读核对',
  },
}

export const DECISION_ORDER: WpsRecoveryDecision[] = [
  'keep', 'retire_guarded', 'cloud_verified', 'cloud_untouched',
]

/** 状态 → 允许的动作（R6 §6 表格）。 */
const STATUS_ACTIONS: Record<string, string[]> = {
  planned: ['recover_journal', 'manual_reconcile'],
  writing: ['recover_journal', 'manual_reconcile'],
  ledger_pending: ['recover_journal', 'manual_reconcile'],
  uncertain: ['manual_reconcile'],
  retired_guarded: ['manual_reconcile'],
  failed: ['repreview'],
  not_started: ['repreview'],
  verified: [],
}

export interface RecoveryOperationView {
  operationId: string
  operationRef: string
  targetDate: string
  targetRefs: string[]
  sheetCount: number
  status: string
  statusLabel: string
  errorCode: string
  pending: boolean
  cloudChecked: boolean
  manualRequired: boolean
  /** 允许动作（服务端白名单**原始**机器码，便于与后端/日志对齐）。 */
  allowedActions: string[]
  /** 允许动作的中文解释，与 `allowedActions` 一一对应。 */
  allowedActionLabels: string[]
  /** 人类可读的风险说明。 */
  risk: string
  /** 是否仍然阻断写入。 */
  blockingRetained: boolean
  /** 该批次可提交的管理员决策；空数组 = 只读，无解除入口。 */
  decisions: WpsRecoveryDecision[]
  /** 是否为幂等退场后的状态。 */
  alreadyRetired: boolean
}

const ACTION_LABELS: Record<string, string> = {
  manual_reconcile: '人工只读核对（不要重传）',
  recover_journal: '走本地日志恢复/修复',
  repreview: '重新预览',
}

export function recoveryActionLabel(action: string): string {
  return ACTION_LABELS[action] || `服务端允许动作：${action}`
}

/** `retired_guarded` 专用文案（R6 §6）。 */
export const RETIRED_GUARDED_LABEL = '已带审计退场（防重复闸门仍保留）'
export const RETIRED_GUARDED_CODE = 'wps_recovery_retired_guarded'
export const RETIRED_GUARDED_RISK = '该批次已带审计退场，但同一「目标日期 + 云表」仍被防重复闸门阻断；证实之前不能重新上传。'

export const RECOVERY_STATUS_LABELS: Record<string, string> = {
  planned: '已计划（可能已写云端）',
  writing: '写入中（可能已写云端）',
  ledger_pending: '账本待落盘（可能已写云端）',
  uncertain: '无法判定 · 待核对',
  retired_guarded: RETIRED_GUARDED_LABEL,
  failed: '已确认未写入',
  not_started: '已确认完全未执行',
  verified: '已确认完成',
}

/** `allowed_next_actions`（服务端白名单）→ 前端可提供的决策集合。 */
export function decisionsFor(operation: Pick<WpsRecoveryOperation, 'status' | 'allowed_next_actions' | 'error_code'>): WpsRecoveryDecision[] {
  const status = String(operation.status || '')
  const actions = Array.isArray(operation.allowed_next_actions)
    ? operation.allowed_next_actions.map(String)
    : (STATUS_ACTIONS[status] ?? [])
  // 已退场：只剩只读核对，任何“再退一次/清除”都不给。
  if (status === 'retired_guarded' || operation.error_code === RETIRED_GUARDED_CODE) {
    return ['keep']
  }
  if (status === 'verified' || actions.includes('repreview') && actions.length === 1) {
    return []
  }
  const decisions: WpsRecoveryDecision[] = []
  if (actions.includes('recover_journal')) {
    decisions.push('retire_guarded', 'cloud_verified', 'cloud_untouched')
  }
  if (actions.includes('manual_reconcile')) {
    if (!decisions.includes('retire_guarded')) decisions.push('retire_guarded')
    decisions.push('keep')
  }
  if (actions.includes('repreview') && decisions.length === 0) {
    // failed / not_started：已确认未写入，只需重新预览，不需要管理员解阻断。
    return []
  }
  return DECISION_ORDER.filter((decision) => decisions.includes(decision))
}

export function recoveryOperationView(operation: WpsRecoveryOperation | null | undefined): RecoveryOperationView | null {
  if (!operation) return null
  const status = String(operation.status || '')
  const allowedActions = Array.isArray(operation.allowed_next_actions)
    ? operation.allowed_next_actions.map(String)
    : []
  const effectiveActions = allowedActions.length ? allowedActions : (STATUS_ACTIONS[status] ?? [])
  const blockingRetained = status === 'retired_guarded' || operation.error_code === RETIRED_GUARDED_CODE
    || Boolean(operation.pending) || operation.manual_required === true
  const decisions = decisionsFor(operation)
  let risk = '该批次没有拿到确定的执行结论，未核实前不能重新上传。'
  if (status === 'retired_guarded' || operation.error_code === RETIRED_GUARDED_CODE) risk = RETIRED_GUARDED_RISK
  else if (status === 'uncertain') risk = '无法判定写入结果；退场只清理全局 pending，同目标防重复闸门仍在。'
  else if (status === 'failed' || status === 'not_started') risk = '已确认未写入；重新预览即可，不需要解阻断。'
  else if (status === 'verified') risk = '已确认完成，无需操作。'
  else if (operation.pending) risk = '未完成操作可能已经写云端；必须人工只读核对后再决定。'
  return {
    operationId: operation.operation_id || '',
    operationRef: operation.operation_ref || '',
    targetDate: operation.target_date || '',
    targetRefs: Array.isArray(operation.target_refs) ? operation.target_refs : [],
    sheetCount: Number(operation.sheet_count || 0),
    status,
    statusLabel: RECOVERY_STATUS_LABELS[status] || `未知状态 ${status || '（空）'}`,
    errorCode: operation.error_code || '',
    pending: Boolean(operation.pending),
    cloudChecked: Boolean(operation.cloud_checked),
    manualRequired: Boolean(operation.manual_required),
    allowedActions: effectiveActions,
    allowedActionLabels: effectiveActions.map(recoveryActionLabel),
    risk,
    blockingRetained,
    decisions,
    alreadyRetired: status === 'retired_guarded' && !operation.pending,
  }
}

export interface ResolveDraftInput {
  operationId: string
  decision: WpsRecoveryDecision | ''
  confirm: string
  note: string
  structureChecked: boolean
  /** 服务端给出的允许决策白名单；缺省时用本地映射结果。 */
  allowedDecisions?: WpsRecoveryDecision[]
}

export interface ResolveBuildOk {
  ok: true
  payload: WpsRecoveryResolvePayload
}

export interface ResolveBuildError {
  ok: false
  code: 'invalid_operation_id' | 'decision_not_allowed' | 'decision_required'
    | 'note_required' | 'confirmation_required' | 'structure_confirmation_required'
  message: string
}

/**
 * 本地校验并构造请求体。
 *
 * **只**产出契约允许的 4 个决策；不可能产出「删除账本」「直接重传」这类动作。
 */
export function buildResolvePayload(input: ResolveDraftInput): ResolveBuildOk | ResolveBuildError {
  const operationId = String(input.operationId || '').trim()
  if (!OPERATION_ID_PATTERN.test(operationId)) {
    return {
      ok: false, code: 'invalid_operation_id',
      message: '操作标识格式非法：必须是内部格式 wps-<16位小写hex>，请用「刷新状态（只读）」重新取。',
    }
  }
  const decision = input.decision
  if (!decision) {
    return { ok: false, code: 'decision_required', message: '请先选择处置动作。' }
  }
  if (input.allowedDecisions && !input.allowedDecisions.includes(decision)) {
    return {
      ok: false, code: 'decision_not_allowed',
      message: `服务端不允许该动作；可选：${input.allowedDecisions.map((item) => DECISION_SPECS[item].label).join('、') || '（无）'}`,
    }
  }
  const note = String(input.note || '').trim()
  if (note.length < 4) {
    return { ok: false, code: 'note_required', message: '请填写至少 4 个字的人工核对说明（会写入 journal 审计）。' }
  }
  if (String(input.confirm || '') !== decision) {
    return { ok: false, code: 'confirmation_required', message: `请逐字输入“${decision}”进行确认。` }
  }
  const needsStructure = DECISION_SPECS[decision].requiresStructureCheck
  if (needsStructure && input.structureChecked !== true) {
    return {
      ok: false, code: 'structure_confirmation_required',
      message: '该动作要求先确认「已人工核对云端表结构」。',
    }
  }
  return {
    ok: true,
    payload: {
      operation_id: operationId,
      decision,
      confirm: decision,
      note,
      ...(needsStructure ? { confirm_structure_checked: true } : {}),
    },
  }
}

export interface ResolveOutcomeView {
  ok: boolean
  tone: 'success' | 'warning' | 'danger' | 'info'
  title: string
  detail: string
  nextStep: string
  /** 失败后阻断是否仍然保留。 */
  blockingRetained: boolean
  /** 是否写云端（本入口恒为 false）。 */
  cloudWrite: boolean
  /** 是否真的改了本地状态（already_retired 时为 false）。 */
  changed: boolean
  /** 幂等重复提交。 */
  duplicate: boolean
  /** 是否允许自动重传（恒为 false）。 */
  autoRetryAllowed: boolean
  code: string
}

const RESOLVE_FAILURE: Record<string, { title: string; detail: string; nextStep: string; tone: 'danger' | 'warning' }> = {
  forbidden: {
    title: '当前账号没有该权限',
    detail: '该恢复入口仅管理员可用（服务端 403 admin_only）；本次没有改任何文件。',
    nextStep: '请联系管理员处理；普通用户不需要也不应该能解除阻断。',
    tone: 'warning',
  },
  invalid_operation_id: {
    title: '操作标识格式非法',
    detail: '服务端只接受内部格式 wps-<16位小写hex>；本次没有改任何文件。',
    nextStep: '用「刷新状态（只读）」重新取操作标识。',
    tone: 'warning',
  },
  decision_not_allowed: {
    title: '该处置动作不被允许',
    detail: '服务端白名单拒绝了该决策；本次没有改任何文件。',
    nextStep: '改用服务端允许的动作，或只做人工只读核对。',
    tone: 'warning',
  },
  note_required: {
    title: '缺少人工核对说明',
    detail: 'note 为空或少于 4 个字符；本次没有改任何文件。',
    nextStep: '补充说明后重新提交。',
    tone: 'warning',
  },
  confirmation_required: {
    title: '逐字确认不匹配',
    detail: 'confirm 必须与 decision 完全相同；本次没有改任何文件。',
    nextStep: '重新逐字输入决策名后提交。',
    tone: 'warning',
  },
  structure_confirmation_required: {
    title: '未确认已核对云端表结构',
    detail: '退场动作要求 confirm_structure_checked=true；本次没有改任何文件。',
    nextStep: '确认已人工核对云端表结构后重试。',
    tone: 'warning',
  },
  operation_conflict: {
    title: '另一个任务正在运行',
    detail: `已有互斥操作占用中；本次恢复请求被拒绝，没有改任何文件。`,
    nextStep: `${CONCURRENT_TASK_TEXT}；可用「刷新状态（只读）」查看当前是谁在跑。`,
    tone: 'warning',
  },
  not_found: {
    title: '找不到该操作',
    detail: '该 operation 不存在或已归档；本次没有改任何文件。',
    nextStep: '重新只读查询恢复状态。',
    tone: 'warning',
  },
  not_pending: {
    title: '该批次已确认完成，无需备注',
    detail: '服务端判定无需操作；本次没有改任何文件。',
    nextStep: '无需操作；如需继续请重新预览。',
    tone: 'warning',
  },
  cli_unavailable: {
    title: '需要读云端但云同步组件不可用',
    detail: '该决策需要只读访问云端，当前 kdocs-cli 不可用；本次没有改任何文件。',
    nextStep: '先完成云文档授权/修复组件后重试，或改选「只记录审计备注」。',
    tone: 'warning',
  },
  cloud_verify_failed: {
    title: '云端证明不了「已完成」',
    detail: '只读核对没有拿到可以支撑“已完成”的证据，因此不解除阻断。',
    nextStep: '下一步是人工只读核对（manual_reconcile）；不得当作可以重传。',
    tone: 'danger',
  },
  cloud_not_untouched: {
    title: '云端证明不了「完全未执行」',
    detail: '只读核对无法证明零写入，因此不解除阻断。',
    nextStep: '下一步是人工只读核对（manual_reconcile）；不得当作可以重传。',
    tone: 'danger',
  },
  journal_write_failed: {
    title: '本地日志持久化失败',
    detail: '审计记录没有落盘，本次处置不生效；没有改任何文件。',
    nextStep: '先修复本地磁盘/权限问题，再重新只读核对；确认前不要重复提交。',
    tone: 'danger',
  },
  journal_unreadable: {
    title: '本地恢复日志不可读',
    detail: '服务端读不到本地日志，无法安全处置；没有改任何文件。',
    nextStep: '联系管理员修复本地日志后重试；不要删除账本，也不要直接重传。',
    tone: 'danger',
  },
  local_state_blocked: {
    title: '本地日志版本不受支持或已损坏',
    detail: '意图日志不可用，服务端失败关闭；没有改任何文件。',
    nextStep: '请升级到匹配版本或联系管理员迁移本地日志；不要删除日志，也不要直接重传。',
    tone: 'danger',
  },
  unsupported_status_requires_manual: {
    title: '存在不受支持的状态，必须人工处理',
    detail: '服务端保留了未知原始状态并继续阻断，不能用云端核对决策绕过。',
    nextStep: '只能用「只记录审计备注」留痕，并人工只读核对。',
    tone: 'danger',
  },
  internal_error: {
    title: '服务端内部错误',
    detail: '未预期异常；本次没有改任何文件。',
    nextStep: '查看日志后重试，或联系管理员。',
    tone: 'danger',
  },
}

/** 响应 → 用户可见结论。失败一律「保持阻断」，且永不声称成功。 */
export function recoveryResolveView(result: WpsRecoveryResolveResult | null | undefined): ResolveOutcomeView {
  const cloudWrite = result?.cloud_write === true
  const changed = result?.changed === true
  const duplicate = result?.audit?.duplicate === true || result?.status === 'already_retired'
  const guardRetained = result?.scope?.guard_retained !== false
  const base: ResolveOutcomeView = {
    ok: false, tone: 'warning', title: '恢复处置未完成', detail: '没有拿到明确结果。',
    nextStep: '只读核对状态后再决定；不要重复提交。',
    blockingRetained: true, cloudWrite, changed, duplicate, autoRetryAllowed: false, code: '',
  }
  if (!result) return base

  const code = String(result.code || result.status || '')
  if (result.ok !== true) {
    const mapped = RESOLVE_FAILURE[code]
    if (mapped) {
      return { ...base, ok: false, code, tone: mapped.tone, title: mapped.title, detail: mapped.detail, nextStep: mapped.nextStep }
    }
    if (journalIssueOf(code, String(result.reason || ''))) {
      return {
        ...base, ok: false, code, tone: 'danger',
        title: '本地日志不可用，处置未生效',
        detail: result.reason || '本地日志不可用；没有改任何文件。',
        nextStep: '先修复本地日志，再重新只读核对；不要删除账本，也不要直接重传。',
      }
    }
    return {
      ...base, ok: false, code,
      title: '恢复处置失败（阻断保持）',
      detail: result.reason || '服务端拒绝或未能完成本次处置；没有改任何文件。',
      nextStep: result.next_action || '只读核对状态后重试；不要走删除账本或直接重传的捷径。',
    }
  }

  // `verified_on_disk === false`：服务端重新读盘**没有**确认效果已落盘。
  // 这时不能照着"已写入审计退场记录"平铺直叙，必须提示以只读核对为准并保持阻断。
  const notOnDisk = result.verified_on_disk === false

  switch (String(result.status || '')) {
    case 'retired_guarded':
      return {
        ...base, ok: true, code, tone: 'warning', changed: true, duplicate: false,
        blockingRetained: guardRetained,
        title: notOnDisk ? '退场已受理，但重新读盘未确认落盘 · 待核对' : RETIRED_GUARDED_LABEL,
        detail: notOnDisk
          ? '服务端返回成功，但重新读盘没有确认审计记录已落盘。本地状态可能仍是退场前，请以只读核对为准。'
          : '已写入审计退场记录；没有写云端。同一「目标日期 + 云表」的防重复闸门仍然生效，未知写入结果不会因此变成可以重传。',
        nextStep: notOnDisk
          ? '先点「刷新状态（只读）」核对本地日志与阻断状态，再决定下一步；不要重复提交。'
          : (result.next_action || '下一步：继续人工只读核对云端（manual_reconcile）。'),
      }
    case 'cloud_verified':
      return {
        ...base, ok: true, code, tone: 'success', changed: true,
        blockingRetained: false,
        title: '云端只读核对：已确认完成',
        detail: '已按云端实际证据判定为完成；本入口没有写云端。',
        nextStep: result.next_action || '按判定结果决定是否需要重新预览。',
      }
    case 'cloud_untouched':
      return {
        ...base, ok: true, code, tone: 'success', changed: true,
        blockingRetained: false,
        title: '云端只读核对：已确认完全未执行',
        detail: '已按云端实际证据判定为未写入；本入口没有写云端。',
        nextStep: result.next_action || '重新预览后再决定是否上传（repreview）。',
      }
    case 'keep_recorded':
      return {
        ...base, ok: true, code, tone: 'warning', changed: true,
        blockingRetained: true,
        title: notOnDisk ? '备注已受理，但重新读盘未确认落盘 · 待核对' : '已记录审计备注（阻断保持）',
        detail: notOnDisk
          ? '服务端返回成功，但重新读盘没有确认审计备注已落盘；阻断保持，请以只读核对为准。'
          : '只是留痕，没有解除任何阻断；本入口没有写云端。',
        nextStep: result.next_action || '继续人工只读核对。',
      }
    case 'already_retired':
      return {
        ...base, ok: true, code, tone: 'warning', changed: false, duplicate: true,
        blockingRetained: guardRetained,
        title: '该批次此前已退场（本次未重复写盘）',
        detail: '重复提交同一退场决策是幂等的：服务端没有再次写盘，防重复闸门仍按原状态生效。',
        nextStep: result.next_action || '继续人工只读核对云端。',
      }
    default:
      return {
        ...base, ok: true, code, tone: 'warning', changed,
        blockingRetained: guardRetained,
        title: `处置已受理（${result.status || '未知状态'}）`,
        detail: result.reason || '服务端已处理本次处置；本入口没有写云端。',
        nextStep: result.next_action || '只读核对状态确认效果；不要重复提交。',
      }
  }
}

/** 恢复状态计数里必须展示的新增项。 */
export function retiredGuardedCount(status: {
  counts?: Record<string, number>
  summary?: { retired_guarded_count?: number }
  retired_guarded_count?: number
} | null | undefined): number {
  if (!status) return 0
  const fromSummary = Number(status.summary?.retired_guarded_count ?? 0)
  const fromCounts = Number(status.counts?.retired_guarded ?? 0)
  const fromTop = Number(status.retired_guarded_count ?? 0)
  return Math.max(fromSummary, fromCounts, fromTop)
}
