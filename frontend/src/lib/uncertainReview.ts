/**
 * 闪时送「未解决的不确定记录」只读核对 + 管理员解除（纯逻辑层）。
 *
 * 背景：闪时送下单 POST 非幂等，一旦结果未知，本地 journal 记为活跃未决记录，
 * 之后每一轮运行都会在任何 POST 之前先只读对账；站内查不到就继续阻断
 * （status = blocked_uncertain）。前端必须：
 *
 * - 如实列出未决记录（手机号后端已掩码，这里只做兜底），绝不把阻断渲染成成功；
 * - station_absent（解除阻断）必须建立在一次新鲜且指纹一致的只读核对之上，
 *   否则服务端会以 review_required / review_stale / journal_changed 拒绝；
 * - station_present 只是把站内已存在的订单标记为已确认，同样需要勾选与审计，
 *   但不依赖核对快照（站内已能看到订单本身就是证据）；
 * - 提交前本地就拦住：无勾选 / note<4 / confirm 不一致；
 * - 失败一律保持阻断，且没有任何「直接重发 / 删除记录」的捷径。
 *
 * 纯逻辑模块，Node 测试可直接覆盖（uncertainReview.test.ts）。
 */
import type {
  SssReviewStartResult,
  SssUncertainClassification,
  SssUncertainCounts,
  SssUncertainDecision,
  SssUncertainRecord,
  SssUncertainResolvePayload,
  SssUncertainResolveResult,
  SssUncertainReview,
  SssUncertainReviewCounts,
  SssUncertainState,
} from './bridge.ts'

/** 解除入口永不发送 POST（契约保证 post_sent=false）；界面据此显示。 */
export const SSS_RESOLVE_SENDS_POST = false

/** 人工核对说明最小长度（与后端 note_required 一致）。 */
export const SSS_NOTE_MIN_LENGTH = 4

/** 没有核对快照时后端返回的形状；前端兜底用同一份，避免各写一套默认值。 */
export const EMPTY_SSS_REVIEW: SssUncertainReview = {
  available: false,
  checked_at: '',
  age_s: null,
  stale: false,
  journal_fingerprint: '',
  journal_matches: false,
  wide_window_days: 3,
  counts: {},
  classifications: {},
}

/** 决策文案（决策名严格照抄冻结契约）。 */
export interface SssDecisionSpec {
  decision: SssUncertainDecision
  label: string
  /** 这个决策实际会改什么。 */
  effect: string
  /** 用户可见的风险说明（必须展示）。 */
  risk: string
  /** 是否必须先有一次新鲜且指纹一致的只读核对。 */
  requiresFreshReview: boolean
  /** 决策后阻断是否保留。 */
  blockingRetained: boolean
  nextActionLabel: string
}

export const SSS_DECISION_SPECS: Record<SssUncertainDecision, SssDecisionSpec> = {
  station_absent: {
    decision: 'station_absent',
    label: '已确认站内无这些订单 → 解除阻断',
    effect: '把所选记录标记为 discarded（有充分证据未落单）；只写本地 journal 审计，不发任何 POST。',
    risk: '只有只读核对确认站内确实查不到这些订单、且核对之后本地记录没有被改动时才可解除；否则服务端会拒绝并继续保持阻断。',
    requiresFreshReview: true,
    blockingRetained: false,
    nextActionLabel: '解除后重新运行闪时送任务；未勾选的记录仍会阻断。',
  },
  station_present: {
    decision: 'station_present',
    label: '已在站内找到 → 标记为已确认',
    effect: '把所选记录标记为 resolved（站内订单已存在）；只写本地 journal 审计，不发任何 POST。',
    risk: '站内已存在对应订单，绝不能重发同一批；标记只是停止把它们当作「可能漏单」。',
    requiresFreshReview: false,
    blockingRetained: false,
    nextActionLabel: '不要重发这些订单；剩余未勾选记录仍会阻断。',
  },
  keep: {
    decision: 'keep',
    label: '保持阻断（不做任何改动）',
    effect: '保持现状：不改变任何记录状态、不发送任何 POST，阻断继续生效。',
    risk: '最保守的选项：阻断保持，需要继续人工只读核对。',
    requiresFreshReview: false,
    blockingRetained: true,
    nextActionLabel: '继续人工只读核对站内订单与本地记录。',
  },
}

export const SSS_DECISION_ORDER: SssUncertainDecision[] = ['station_present', 'station_absent', 'keep']

export const SSS_RECORD_STATUS_LABELS: Record<string, string> = {
  inflight: '已发出待对账',
  unresolved: '结果不确定',
}

export function recordStatusLabel(status: string): string {
  const key = String(status || '')
  return SSS_RECORD_STATUS_LABELS[key] || ('未知状态 ' + (key || '（空）'))
}

export const SSS_CLASSIFICATION_LABELS: Record<string, string> = {
  station_missing: '站内未查到',
  station_found_other_day: '站内只查到其他日期',
  station_confirmed: '站内已确认存在',
  scan_failed: '扫描失败，未能判定',
}

export function classificationLabel(value: string): string {
  const key = String(value || '')
  if (!key) return '尚未核对'
  return SSS_CLASSIFICATION_LABELS[key] || ('未识别分类 ' + key)
}

/**
 * 手机号/账号掩码兜底：与后端 _mask_contact 同一规则。
 *
 * 后端已经掩码过（含 *），这里不能再掩一次，否则 138****0001 会变成 13*****0001；
 * 只有在后端漏掩（11 位纯数字）时才兜底，保证界面绝不出现完整号码。
 */
export function maskPhoneFallback(value: string): string {
  const text = String(value || '').trim()
  if (!text) return ''
  if (text.indexOf('*') >= 0) return text
  if (/^[0-9]{11}$/.test(text)) return text.slice(0, 3) + '****' + text.slice(-4)
  if (text.length <= 4) return '*'.repeat(text.length)
  return text.slice(0, 2) + '*'.repeat(Math.max(0, text.length - 6)) + text.slice(-4)
}

export interface SssUncertainRecordView {
  journalId: string
  identifier: string
  sheet: string
  batchId: string
  deliveryDate: string
  status: string
  statusLabel: string
  error: string
  createdAt: string
  name: string
  /** 掩码后的手机号；后端已掩码，这里只做兜底。 */
  phone: string
  deliveryTime: string
  doorNum: string
  account: string
  reason: string
  /** 上一次核对对该条的分类；没有快照/未分类时为空串。 */
  classification: string
  classificationLabel: string
  /** 是否仍是活跃记录（resolved/discarded 不再阻断）。 */
  active: boolean
}

export function uncertainRecordView(
  record: SssUncertainRecord | null | undefined,
  classifications: Record<string, string> = {},
): SssUncertainRecordView {
  const status = String((record && record.status) || '')
  const journalId = String((record && record.journal_id) || '')
  const classification = String(classifications[journalId] || '')
  return {
    journalId,
    identifier: String((record && record.identifier) || ''),
    sheet: String((record && record.sheet) || ''),
    batchId: String((record && record.batch_id) || ''),
    deliveryDate: String((record && record.delivery_date) || ''),
    status,
    statusLabel: recordStatusLabel(status),
    error: String((record && record.error) || ''),
    createdAt: String((record && record.created_at) || ''),
    name: String((record && record.name) || ''),
    phone: maskPhoneFallback((record && record.phone) || ''),
    deliveryTime: String((record && record.delivery_time) || ''),
    doorNum: String((record && record.door_num) || ''),
    account: maskPhoneFallback((record && record.account) || ''),
    reason: String((record && record.reason) || ''),
    classification,
    classificationLabel: classificationLabel(classification),
    active: status !== 'resolved' && status !== 'discarded',
  }
}

export interface SssUncertainReviewView {
  available: boolean
  stale: boolean
  journalMatches: boolean
  checkedAt: string
  ageS: number | null
  journalFingerprint: string
  wideWindowDays: number
  counts: SssUncertainReviewCounts
  classifications: Record<string, SssUncertainClassification | string>
  /** 是否满足 station_absent 的「新鲜且指纹一致」要求。 */
  usableForAbsent: boolean
  /** 不满足时的机器码（review_required / review_stale / journal_changed）。 */
  unavailableCode: string
  unavailableReason: string
}

const REVIEW_COUNT_KEYS: Array<keyof SssUncertainReviewCounts> = [
  'station_missing', 'station_found_other_day', 'station_confirmed', 'scan_failed',
]

export function uncertainReviewView(review: SssUncertainReview | null | undefined): SssUncertainReviewView {
  const source = review || EMPTY_SSS_REVIEW
  const counts: SssUncertainReviewCounts = {
    station_missing: 0,
    station_found_other_day: 0,
    station_confirmed: 0,
    scan_failed: 0,
  }
  for (const key of REVIEW_COUNT_KEYS) {
    counts[key] = Number((source.counts && source.counts[key]) ?? 0) || 0
  }
  const classifications: Record<string, SssUncertainClassification | string> = {}
  const rawClassifications = source.classifications || {}
  for (const key of Object.keys(rawClassifications)) {
    classifications[key] = rawClassifications[key]
  }
  const available = source.available === true
  const stale = source.stale === true
  const journalMatches = source.journal_matches === true
  let unavailableCode = ''
  let unavailableReason = ''
  if (!available) {
    unavailableCode = 'review_required'
    unavailableReason = '还没有只读核对快照：解除阻断前必须先做一次「只读核对站内订单」。'
  } else if (stale) {
    unavailableCode = 'review_stale'
    unavailableReason = '上次只读核对结果已过期：请重新核对站内订单后再解除。'
  } else if (!journalMatches) {
    unavailableCode = 'journal_changed'
    unavailableReason = '本地未决记录在核对后发生变化：请重新核对，确认站内状态后再解除。'
  }
  return {
    available,
    stale,
    journalMatches,
    checkedAt: String(source.checked_at || ''),
    ageS: typeof source.age_s === 'number' ? source.age_s : null,
    journalFingerprint: String(source.journal_fingerprint || ''),
    wideWindowDays: Number(source.wide_window_days ?? 3) || 0,
    counts,
    classifications,
    usableForAbsent: available && !stale && journalMatches,
    unavailableCode,
    unavailableReason,
  }
}

export interface SssUncertainStateView {
  ok: boolean
  /** ok=false 时的机器码（journal_unreadable 等）。 */
  code: string
  reason: string
  nextAction: string
  readOnly: boolean
  origin: string
  account: string
  journal: string
  batchKey: string
  deliveryDate: string
  counts: SssUncertainCounts
  records: SssUncertainRecordView[]
  review: SssUncertainReviewView
  /** 仍有活跃未决记录（阻断未解除）。 */
  blocking: boolean
}

export function uncertainStateView(state: SssUncertainState | null | undefined): SssUncertainStateView {
  const review = uncertainReviewView(state && state.review)
  const records = ((state && state.records) || []).map((record) => uncertainRecordView(record, review.classifications))
  const counts: SssUncertainCounts = {
    active: Number((state && state.counts && state.counts.active) ?? 0) || 0,
    inflight: Number((state && state.counts && state.counts.inflight) ?? 0) || 0,
    unresolved: Number((state && state.counts && state.counts.unresolved) ?? 0) || 0,
    resolved: Number((state && state.counts && state.counts.resolved) ?? 0) || 0,
    discarded: Number((state && state.counts && state.counts.discarded) ?? 0) || 0,
  }
  const ok = Boolean(state && state.ok === true)
  return {
    ok,
    code: String((state && state.code) || ''),
    reason: String((state && state.reason) || ''),
    nextAction: String((state && state.next_action) || ''),
    readOnly: !state || state.read_only !== false,
    origin: String((state && state.origin) || ''),
    account: String((state && state.account) || ''),
    journal: String((state && state.journal) || ''),
    batchKey: String((state && state.batch_key) || ''),
    deliveryDate: String((state && state.delivery_date) || ''),
    counts,
    records,
    review,
    // ok=false（journal 损坏/不可读）绝不能当成「没有未决记录」：保持阻断。
    blocking: ok ? (counts.active > 0 || records.some((row) => row.active)) : true,
  }
}

export interface SssAbsentEvidence {
  allowed: boolean
  code: string
  reason: string
}

/**
 * station_absent 的逐条证据检查（与后端写入口的校验保持一致，避免必然被拒的提交）。
 *
 * 后端要求：快照新鲜且指纹一致、所选记录都被判成 station_missing；若某条在宽窗内
 * 命中（落单但送达日被平台改过），必须改用 station_present，不能解除阻断。
 */
export function absentEvidence(recordIds: string[], review: SssUncertainReviewView): SssAbsentEvidence {
  if (!review.usableForAbsent) {
    return {
      allowed: false,
      code: review.unavailableCode || 'review_required',
      reason: review.unavailableReason || '必须先完成一次只读核对。',
    }
  }
  const ids = (recordIds || []).filter((id) => String(id || '').trim())
  const foundOtherDay = ids.filter((id) => review.classifications[id] === 'station_found_other_day')
  if (foundOtherDay.length > 0) {
    return {
      allowed: false,
      code: 'station_state_changed',
      reason: '只读核对在站内宽窗内找到了这些订单（送达日不同）：请先人工核对；若确认已落单，请改用「已在站内找到」。',
    }
  }
  const notMissing = ids.filter((id) => review.classifications[id] !== 'station_missing')
  if (notMissing.length > 0) {
    return {
      allowed: false,
      code: 'review_required',
      reason: '本次只读核对没有覆盖全部所选记录，或这些记录不是「站内未查到」：请重新核对后再解除。',
    }
  }
  return { allowed: true, code: '', reason: '' }
}

export interface SssResolveGateInput {
  isAdmin: boolean
  decision: SssUncertainDecision | ''
  recordIds: string[]
  note: string
  confirm: string
  review: SssUncertainReviewView
  /** 提交在途或服务端已有互斥操作：禁用重复点击。 */
  busy?: boolean
}

export interface SssResolveGate {
  allowed: boolean
  /** 不允许时的机器码，与后端 code 同名，便于文案统一。 */
  code: string
  /** 中文原因；allowed=true 时为空串。 */
  reason: string
}

/**
 * 解除按钮/提交的本地门禁。
 *
 * 顺序固定：权限 → 决策 → 勾选 → 核对快照 → note → confirm。
 * 前端只是提前拦住明显错误，最终仍以服务端校验为准。
 */
export function resolveGate(input: SssResolveGateInput): SssResolveGate {
  if (!input.isAdmin) {
    return { allowed: false, code: 'forbidden', reason: '仅管理员可用：该入口只有管理员能打开。' }
  }
  if (input.busy) {
    return { allowed: false, code: 'operation_conflict', reason: '已有提交或任务在途，请等待结束后再试；不要重复提交。' }
  }
  if (!input.decision) {
    return { allowed: false, code: 'decision_required', reason: '请先选择处置动作。' }
  }
  const recordIds = Array.isArray(input.recordIds)
    ? input.recordIds.filter((id) => String(id || '').trim())
    : []
  if (recordIds.length === 0) {
    return { allowed: false, code: 'invalid_record_ids', reason: '请至少勾选一条未决记录。' }
  }
  // 防御性校验：非法 decision（例如旧前端/手工调用传进来的未知串）必须返回禁用，
  // 而不是在查表时抛 TypeError（Verifier D1）。
  const spec = Object.prototype.hasOwnProperty.call(SSS_DECISION_SPECS, input.decision)
    ? SSS_DECISION_SPECS[input.decision]
    : null
  if (!spec) {
    return { allowed: false, code: 'invalid_payload', reason: '处置动作不受支持，请重新选择。' }
  }
  if (spec.requiresFreshReview) {
    const evidence = absentEvidence(recordIds, input.review)
    if (!evidence.allowed) return evidence
  }
  const note = String(input.note || '').trim()
  if (note.length < SSS_NOTE_MIN_LENGTH) {
    return { allowed: false, code: 'note_required', reason: '必须填写至少 4 个字符的人工核对说明（会写入本地审计）。' }
  }
  if (String(input.confirm || '') !== input.decision) {
    return {
      allowed: false,
      code: 'confirmation_required',
      reason: '确认串必须与 decision 完全一致：请逐字输入 ' + input.decision + '。',
    }
  }
  return { allowed: true, code: '', reason: '' }
}

export type SssResolveBuildResult =
  | { ok: true; payload: SssUncertainResolvePayload }
  | { ok: false; code: string; message: string }

/** 本地校验并构造请求体；不可能产出契约之外的决策或字段。 */
export function buildUncertainResolvePayload(input: SssResolveGateInput): SssResolveBuildResult {
  const gate = resolveGate(input)
  if (!gate.allowed) return { ok: false, code: gate.code, message: gate.reason }
  const decision = input.decision as SssUncertainDecision
  return {
    ok: true,
    payload: {
      decision,
      confirm: decision,
      note: String(input.note || '').trim(),
      record_ids: input.recordIds.map((id) => String(id)),
    },
  }
}

export interface SssResolveMessage {
  title: string
  detail: string
  nextStep: string
  tone: 'warning' | 'danger'
}

/** 契约里的 code → 中文提示（标题 + 发生了什么 + 下一步）。 */
export const SSS_RESOLVE_FAILURE_TEXT: Record<string, SssResolveMessage> = {
  forbidden: {
    title: '仅管理员可用',
    detail: '该解除入口仅管理员可用（服务端 403 admin_only）；本次没有改任何文件。',
    nextStep: '请联系管理员处理；普通用户不需要也不应该能解除阻断。',
    tone: 'warning',
  },
  invalid_payload: {
    title: '请求格式不正确',
    detail: '服务端拒绝了本次请求体；本次没有改任何文件。',
    nextStep: '点「刷新未决记录（只读）」后重新勾选并提交。',
    tone: 'warning',
  },
  invalid_record_ids: {
    title: '请至少选择一条记录',
    detail: 'record_ids 为空或格式不正确；本次没有改任何文件。',
    nextStep: '勾选要处置的未决记录后重新提交。',
    tone: 'warning',
  },
  unknown_record_ids: {
    title: '选择的记录已不存在或不属于本批次',
    detail: '服务端找不到这些 journal_id（可能已被自动对账清理）；本次没有改任何文件。',
    nextStep: '点「刷新未决记录（只读）」重新勾选，再决定下一步。',
    tone: 'warning',
  },
  confirmation_required: {
    title: '确认串必须与 decision 完全一致',
    detail: 'confirm 与所选 decision 不一致；本次没有改任何文件。',
    nextStep: '重新逐字输入决策名后再提交。',
    tone: 'warning',
  },
  note_required: {
    title: '必须填写人工核对说明',
    detail: 'note 为空或少于 4 个字符；本次没有改任何文件。',
    nextStep: '补充至少 4 个字符的人工核对说明（会写入本地审计）后重新提交。',
    tone: 'warning',
  },
  review_required: {
    title: '必须先做一次只读核对',
    detail: '没有可用的核对快照，不能解除阻断；本次没有改任何文件。',
    nextStep: '先点「只读核对站内订单」，等核对完成后再解除。',
    tone: 'warning',
  },
  review_stale: {
    title: '只读核对结果已过期',
    detail: '快照超过有效期，不能据此解除阻断；本次没有改任何文件。',
    nextStep: '重新点「只读核对站内订单」，用新的结果再解除。',
    tone: 'warning',
  },
  journal_changed: {
    title: '本地记录在核对后发生变化',
    detail: 'journal 指纹与快照不一致，不能据此解除阻断；本次没有改任何文件。',
    nextStep: '重新只读核对后再解除；不要删除本地记录文件。',
    tone: 'warning',
  },
  station_state_changed: {
    title: '核对后站内状态变化',
    detail: '有记录已在站内查到（可能已被其他进程或人工处理）；本次没有改任何文件。',
    nextStep: '重新只读核对，确认站内实际状态后再决定下一步。',
    tone: 'warning',
  },
  operation_conflict: {
    title: '已有任务在运行',
    detail: '互斥操作占用中，本次解除请求被拒绝，没有改任何文件。',
    nextStep: '等待当前任务结束后重试；不要重复提交。',
    tone: 'warning',
  },
  journal_write_failed: {
    title: '本地记录写入失败',
    detail: '审计记录没有落盘，本次解除不生效；没有改任何文件。',
    nextStep: '先修复本地磁盘/权限问题，再重新只读核对；确认前不要重复提交。',
    tone: 'danger',
  },
  journal_unreadable: {
    title: '本地记录不可读',
    detail: '服务端读不到本地 journal，无法安全处置；没有改任何文件。',
    nextStep: '联系管理员修复本地记录后重试；不要删除记录文件，也不要直接重跑。',
    tone: 'danger',
  },
}

export function resolveFailureText(
  code: string,
  fallback?: { reason?: string; next_action?: string } | null,
): SssResolveMessage {
  const mapped = SSS_RESOLVE_FAILURE_TEXT[String(code || '')]
  if (mapped) return mapped
  return {
    title: '解除未完成（阻断保持）',
    detail: (fallback && fallback.reason) || '服务端拒绝或未能完成本次处置；本次没有改任何文件。',
    nextStep: (fallback && fallback.next_action) || '先只读核对站内订单与本地记录，再决定下一步；不要重跑或补发。',
    tone: 'danger',
  }
}

export interface SssResolveOutcomeView {
  ok: boolean
  tone: 'success' | 'warning' | 'danger' | 'info'
  title: string
  detail: string
  nextStep: string
  /** 处置后阻断是否仍然存在。 */
  blockingRetained: boolean
  changed: boolean
  postSent: boolean
  cloudWrite: boolean
  code: string
  decision: string
  affected: number
  remaining: number
}

/** 响应 → 用户可见结论。失败一律「保持阻断」，且永不声称发送过 POST。 */
export function uncertainResolveView(result: SssUncertainResolveResult | null | undefined): SssResolveOutcomeView {
  const base: SssResolveOutcomeView = {
    ok: false,
    tone: 'warning',
    title: '解除未完成（阻断保持）',
    detail: '没有拿到明确结果。',
    nextStep: '先点「刷新未决记录（只读）」核对本地状态，再决定下一步；不要重跑或补发。',
    blockingRetained: true,
    changed: false,
    postSent: false,
    cloudWrite: false,
    code: '',
    decision: '',
    affected: 0,
    remaining: -1,
  }
  if (!result) return base

  const code = String(result.code || '')
  const decision = String(result.decision || '')
  const affected = Number(result.affected ?? 0) || 0
  const remaining = typeof result.remaining === 'number' ? result.remaining : -1
  const postSent = result.post_sent === true
  const cloudWrite = result.cloud_write === true
  const changed = result.changed === true
  const common = { ...base, code, decision, affected, remaining, postSent, cloudWrite, changed }

  if (result.ok !== true) {
    const mapped = resolveFailureText(code || String(result.status || ''), result)
    return {
      ...common,
      ok: false,
      tone: mapped.tone,
      title: mapped.title,
      detail: mapped.detail,
      nextStep: mapped.nextStep,
      blockingRetained: true,
    }
  }

  const status = String(result.status || '')
  if (status === 'kept') {
    // keep 在后端是纯 no-op（changed=false，不写审计）；必须单独呈现为「保持阻断」，
    // 不能落进下面的幂等分支说成“可能已处置过”。
    return {
      ...common,
      ok: true,
      tone: 'warning',
      title: '已保持阻断（本次未改动任何记录）',
      detail: changed
        ? '服务端确认保持阻断并写入本地审计；没有发送任何 POST，也没有写云端。'
        : '服务端确认保持阻断：没有改动任何记录、没有发送任何 POST、也没有写云端。',
      nextStep: result.next_action || '继续人工只读核对站内订单与本地记录。',
      blockingRetained: true,
    }
  }
  if (!changed) {
    // changed=false 是幂等重复提交：没有改任何文件，阻断是否解除以只读刷新为准。
    return {
      ...common,
      ok: true,
      tone: 'warning',
      title: '本次没有改变任何记录（幂等/可能已处置过）',
      detail: '服务端返回 changed=false：记录状态没有变化，阻断是否解除以「刷新未决记录」的只读结果为准。',
      nextStep: result.next_action || '点「刷新未决记录（只读）」确认当前状态。',
      blockingRetained: true,
    }
  }

  const stillBlocking = remaining > 0
  switch (status) {
    case 'discarded':
      return {
        ...common,
        ok: true,
        tone: stillBlocking ? 'warning' : 'success',
        title: stillBlocking
          ? '已按「站内无这些订单」处置 ' + affected + ' 条，仍有未决记录（阻断保持）'
          : '已解除阻断：站内确认无这 ' + affected + ' 条订单',
        detail: '所选记录已按 station_absent 写入本地审计（discarded）；本入口没有发送任何 POST，也没有写云端。',
        nextStep: stillBlocking
          ? '请继续核对剩余记录；全部处置前不要重跑或补发。'
          : (result.next_action || '可以重新运行闪时送任务；未勾选的记录仍会阻断。'),
        blockingRetained: stillBlocking,
      }
    case 'resolved':
      return {
        ...common,
        ok: true,
        tone: stillBlocking ? 'warning' : 'success',
        title: stillBlocking
          ? '已标记 ' + affected + ' 条为站内已确认，仍有未决记录（阻断保持）'
          : '已标记为站内已确认：' + affected + ' 条',
        detail: '所选记录已按 station_present 写入本地审计（resolved）；本入口没有发送任何 POST，也没有写云端。',
        nextStep: stillBlocking
          ? '请继续核对剩余记录；全部处置前不要重跑或补发。'
          : (result.next_action || '不要重发这些订单；确认站内订单无误后再运行。'),
        blockingRetained: stillBlocking,
      }
    default:
      return {
        ...common,
        ok: true,
        tone: 'warning',
        title: '处置已受理（' + (status || '未知状态') + '）',
        detail: result.reason || '服务端已处理本次处置；本入口没有发送任何 POST，也没有写云端。',
        nextStep: result.next_action || '点「刷新未决记录（只读）」确认效果；不要重跑或补发。',
        blockingRetained: stillBlocking,
      }
  }
}

export interface SssReviewStartGate {
  allowed: boolean
  reason: string
}

/** 「只读核对站内订单」按钮门禁：需要密码，且不能与其它互斥操作并发。 */
export function reviewStartGate(input: {
  isAdmin: boolean
  password: string
  busy?: boolean
  operationActive?: boolean
}): SssReviewStartGate {
  if (!input.isAdmin) return { allowed: false, reason: '只读核对入口仅管理员可用；请联系管理员。' }
  if (input.busy) return { allowed: false, reason: '正在启动只读核对，请勿重复点击。' }
  if (input.operationActive) return { allowed: false, reason: '已有任务在运行，请等待结束后再核对。' }
  if (!String(input.password || '').trim()) {
    return { allowed: false, reason: '请先在闪时送表单填写登录密码，再启动只读核对。' }
  }
  return { allowed: true, reason: '' }
}

export interface SssReviewStartView {
  ok: boolean
  tone: 'info' | 'warning' | 'danger'
  message: string
  operationId: string
  /** validation_failed 时逐字段的中文提示。 */
  fieldMessages: string[]
}

export function reviewStartView(result: SssReviewStartResult | null | undefined): SssReviewStartView {
  if (!result) {
    return {
      ok: false,
      tone: 'danger',
      operationId: '',
      fieldMessages: [],
      message: '没有拿到核对启动结果；请刷新状态后重试。',
    }
  }
  const fields = result.fields || {}
  const fieldMessages = Object.keys(fields).map((key) => {
    const entry = fields[key]
    return (entry && entry.message) || ('字段 ' + key + ' 校验未通过')
  })
  if (result.ok === true) {
    return {
      ok: true,
      tone: 'info',
      operationId: String(result.operation_id || ''),
      fieldMessages,
      message: (result.summary && result.summary.message)
        || '已启动只读核对（零 POST）；结束后会自动刷新未决记录。',
    }
  }
  const reason = String(result.reason || '')
  let message = result.next_action || reason || '只读核对启动失败。'
  if (reason === 'validation_failed') message = '表单校验未通过，请修正后重试。'
  else if (reason === 'operation_conflict') message = '已有任务在运行，请等待结束后重试。'
  return {
    ok: false,
    tone: reason === 'validation_failed' ? 'warning' : 'danger',
    operationId: '',
    fieldMessages,
    message,
  }
}

export interface UncertainOperationLike {
  mode?: string
  status?: string
  summary?: Record<string, unknown> | null
}

function asRecord(source: unknown): Record<string, unknown> | null {
  return source && typeof source === 'object' ? (source as Record<string, unknown>) : null
}

function nestedNumber(source: unknown, key: string): number {
  const record = asRecord(source)
  if (!record) return 0
  const parsed = Number(record[key] ?? 0)
  return Number.isFinite(parsed) ? parsed : 0
}

function isSssMode(mode: string): boolean {
  return mode === 'sss' || mode.indexOf('sss_') === 0
}

/**
 * 在 operation 摘要里找 review.counts.active。
 *
 * 只读核对 worker 的 payload 形如
 * summary.result.review.counts.active；而 operation_status 会把整份 payload 再包一层，
 * 所以按 result 逐层下钻（最多 3 层），不依赖某一种嵌套深度。
 */
function reviewActiveCount(source: unknown, depth = 0): number {
  if (depth > 5) return 0
  const record = asRecord(source)
  if (!record) return 0
  const review = asRecord(record.review)
  const active = nestedNumber(asRecord(review && review.counts), 'active')
  if (active > 0) return active
  const fromResult = reviewActiveCount(record.result, depth + 1)
  if (fromResult > 0) return fromResult
  return reviewActiveCount(record.summary, depth + 1)
}

function operationIndicatesUncertain(operation: UncertainOperationLike | null | undefined): boolean {
  if (!operation) return false
  if (!isSssMode(String(operation.mode || ''))) return false
  if (String(operation.status || '').toLowerCase() === 'blocked_uncertain') return true
  const summary = asRecord(operation.summary)
  if (!summary) return false
  if (nestedNumber(summary, 'uncertain_count') > 0) return true
  if (nestedNumber(summary, 'uncertain_records') > 0) return true
  const result = asRecord(summary.result)
  if (result) {
    if (nestedNumber(result, 'uncertain_count') > 0) return true
    if (nestedNumber(result, 'uncertain_records') > 0) return true
    if (nestedNumber(asRecord(result.summary), 'uncertain_count') > 0) return true
  }
  return reviewActiveCount(summary) > 0
}

/**
 * 是否在闪时送结果区挂载未决记录面板。
 *
 * 只用权威 operation 快照做判断，**不发任何请求**（真正的记录在展开面板时才读）；
 * 同时看历史 operations：启动只读核对 worker 后当前 operation 变成 running，
 * 面板不能因此消失。
 */
export function shouldShowUncertainPanel(
  view: { key?: string } | null | undefined,
  operation: UncertainOperationLike | null | undefined,
  operations?: UncertainOperationLike[] | null,
): boolean {
  const viewKey = String((view && view.key) || '')
  const mode = String((operation && operation.mode) || '')
  if (viewKey === 'blocked_uncertain' && (mode === '' || isSssMode(mode))) return true
  if (operationIndicatesUncertain(operation)) return true
  const history = operations || []
  for (const item of history.slice(0, 5)) {
    if (operationIndicatesUncertain(item)) return true
  }
  return false
}
