/**
 * 闪时送本轮提交统计（`operation.summary.submission`）的解析与展示口径。
 *
 * 字段来自 runner 的真实调用与集合（见字段契约）：未知一律用 `待核对/未知`，不按 0 显示；
 * 响应次数是历史、站内确认是最终状态，两者不能互相顶替；`created` 的语义是对账确认数，
 * 不能当成本轮新建单数。纯逻辑模块，Node 测试直接覆盖。
 */
import type { OperationInfo } from './bridge.ts'
import type { ConnectionState, RecoveryState } from './operationStatus.ts'
import { UNKNOWN_TEXT } from './resultCounts.ts'

/** 主动作：`normal` = 正常开始；`reconcile` = 先对账、只补仍缺失项。 */
export type SssFollowUpMode = 'normal' | 'reconcile'

/** 补单态主动作文案。 */
export const SSS_RECONCILE_LABEL = '核对并补单'

export interface SssSubmissionSummary {
  targetTotal: number | null
  preconfirmed: number | null
  submitted: number | null
  attempts: number | null
  successResponses: number | null
  technicalErrors: number | null
  explicitRejections: number | null
  authRejections: number | null
  balanceRejections: number | null
  notSent: number | null
  newlyConfirmed: number | null
  confirmed: number | null
  unconfirmed: number | null
  reconciled: boolean | null
  /** 计数之间的关系是否自洽；`false` 时不得给成功口径。 */
  consistent: boolean
  /** 矛盾点（只含字段名与关系，不含账号等数据）。 */
  issues: string[]
}

export interface SssRow {
  key:
    | 'preconfirmed'
    | 'submitted'
    | 'attempts'
    | 'successResponses'
    | 'technicalErrors'
    | 'explicitRejections'
    | 'notSent'
    | 'newlyConfirmed'
    | 'confirmed'
    | 'unconfirmed'
  label: string
  value: string
  tone: 'neutral' | 'success' | 'warning' | 'danger'
}

export type SssHeadlineKey =
  | 'dry-run'
  | 'preflight'
  | 'guarded'
  | 'no-orders'
  | 'running'
  | 'pending'
  | 'unconfirmed'
  | 'confirmed'
  | 'unknown'

export interface SssHeadline {
  key: SssHeadlineKey
  tone: 'neutral' | 'info' | 'success' | 'warning'
  title: string
  detail: string
}

/** 这次运行是不是“真实下单”：预检/模拟/余额闸门/无单都不提供补单。 */
export type SssRunKind = 'live' | 'dry_run' | 'preflight' | 'guarded' | 'no_orders' | 'unknown'

export type SssFollowUpReason =
  | 'eligible'
  | 'offline'
  | 'recovery-pending'
  | 'busy'
  | 'mode-not-live'
  | 'no-finished-run'
  | 'not-real-run'
  | 'blocked'
  | 'all-confirmed'
  | 'nothing-unconfirmed'

export interface SssFollowUpView {
  /** true = 主动作改为「核对并补单」。 */
  reconcile: boolean
  label: string
  reason: SssFollowUpReason
  /** 不能补单时给界面的中文理由（可展示，不含真实账号）。 */
  reasonText: string
  operationId: string
  submission: SssSubmissionSummary | null
}

function asRecord(value: unknown): Record<string, unknown> | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null
  return value as Record<string, unknown>
}

/**
 * 计数只接受有限非负整数；`boolean`/数组/对象/其它类型一律按未知（不能伪装成 0）。
 * `string` 仅兼容旧接口里写成数字串（"73"）的情况。
 */
function toCount(value: unknown, field: string, issues: string[]): number | null {
  if (value === null || value === undefined) return null
  if (typeof value === 'string' && value.trim() === '') return null
  const numeric = typeof value === 'number' ? value
    : (typeof value === 'string' ? Number(value.trim()) : Number.NaN)
  if (!Number.isFinite(numeric)) {
    issues.push(`${field} 不是数字`)
    return null
  }
  if (!Number.isInteger(numeric)) {
    issues.push(`${field} 不是整数`)
    return null
  }
  if (numeric < 0) {
    issues.push(`${field} 为负数`)
    return null
  }
  return numeric
}

function toBool(value: unknown): boolean | null {
  if (typeof value === 'boolean') return value
  if (value === 1 || value === '1' || value === 'true') return true
  if (value === 0 || value === '0' || value === 'false') return false
  return null
}

/**
 * 解析 `summary.submission`。
 *
 * 返回 `null` = 没有 submission 字典（旧后端/旧运行）→ 界面不渲染统计卡片。
 * 字典存在时逐字段解析，并检查契约里的字段关系（集合分区、集合增量、POST 次数上限）。
 */
export function parseSssSubmission(value: unknown): SssSubmissionSummary | null {
  const raw = asRecord(value)
  if (!raw) return null
  const issues: string[] = []
  const pick = (field: string, ...aliases: string[]): number | null => {
    for (const key of [field, ...aliases]) {
      if (key in raw) return toCount(raw[key], key, issues)
    }
    return null
  }
  const targetTotal = pick('targetTotal', 'target_total')
  const preconfirmed = pick('preconfirmed')
  const submitted = pick('submitted')
  const attempts = pick('attempts')
  const successResponses = pick('successResponses', 'success_responses')
  const technicalErrors = pick('technicalErrors', 'technical_errors')
  const explicitRejections = pick('explicitRejections', 'explicit_rejections')
  const authRejections = pick('authRejections', 'auth_rejections')
  const balanceRejections = pick('balanceRejections', 'balance_rejections')
  const notSent = pick('notSent', 'not_sent')
  const newlyConfirmed = pick('newlyConfirmed', 'newly_confirmed')
  const confirmed = pick('confirmed')
  const unconfirmed = pick('unconfirmed')
  const reconciled = 'reconciled' in raw ? toBool(raw.reconciled) : null

  if (reconciled === false) {
    if (unconfirmed === 0) issues.push('reconciled=false 但 unconfirmed=0')
    if (confirmed !== null) issues.push('reconciled=false 但有最终确认数')
    if (newlyConfirmed !== null) issues.push('reconciled=false 但有新增确认数')
  }
  // 每个目标任务的 POST 只有一次“首次调用”，重登/人工重试计入 attempts。
  if (submitted !== null && attempts !== null && submitted > attempts) {
    issues.push('submitted 大于 attempts')
  }
  // 最终确认集合是目标集合的子集；未确认集合与之互补。
  const withinTarget = (field: string, num: number | null): void => {
    if (num !== null && targetTotal !== null && num > targetTotal) {
      issues.push(`${field} 大于 target_total`)
    }
  }
  withinTarget('submitted', submitted)
  withinTarget('confirmed', confirmed)
  withinTarget('preconfirmed', preconfirmed)
  withinTarget('notSent', notSent)
  if (submitted !== null && notSent !== null && targetTotal !== null
    && submitted + notSent > targetTotal) {
    issues.push('submitted+not_sent 大于 target_total')
  }
  if (confirmed !== null && unconfirmed !== null && targetTotal !== null
    && confirmed + unconfirmed !== targetTotal) {
    issues.push('confirmed+unconfirmed 与 target_total 不符')
  }
  // 新增确认 = 最终确认集合 − 初始已确认集合（对账差异，不要求 <= submitted）。
  if (preconfirmed !== null && confirmed !== null && preconfirmed > confirmed) {
    issues.push('preconfirmed 大于 confirmed')
  }
  if (newlyConfirmed !== null && confirmed !== null && newlyConfirmed > confirmed) {
    issues.push('newlyConfirmed 大于 confirmed')
  }
  if (preconfirmed !== null && newlyConfirmed !== null && confirmed !== null
    && preconfirmed + newlyConfirmed !== confirmed) {
    issues.push('preconfirmed+newlyConfirmed 与 confirmed 不符')
  }
  // 响应分类是 POST 调用结果的子集。
  for (const [field, num] of [
    ['successResponses', successResponses], ['technicalErrors', technicalErrors],
    ['explicitRejections', explicitRejections], ['authRejections', authRejections],
    ['balanceRejections', balanceRejections],
  ] as Array<[string, number | null]>) {
    if (num !== null && attempts !== null && num > attempts) issues.push(`${field} 大于 attempts`)
  }
  const classified = [successResponses, technicalErrors, explicitRejections]
  if (classified.every((item) => item !== null) && attempts !== null
    && (successResponses ?? 0) + (technicalErrors ?? 0) + (explicitRejections ?? 0) > attempts) {
    issues.push('响应分类次数之和大于 attempts')
  }

  return {
    targetTotal,
    preconfirmed,
    submitted,
    attempts,
    successResponses,
    technicalErrors,
    explicitRejections,
    authRejections,
    balanceRejections,
    notSent,
    newlyConfirmed,
    confirmed,
    unconfirmed,
    reconciled,
    consistent: issues.length === 0,
    issues,
  }
}

function countText(value: number | null, unit: string): string {
  return value === null ? UNKNOWN_TEXT : `${value} ${unit}`
}

function pairText(left: number | null, right: number | null, unit: string): string {
  if (left === null || right === null) return UNKNOWN_TEXT
  return `${left} / ${right} ${unit}`
}

/**
 * 逐项计数。对账未完成（`reconciled !== true`）时确认类计数的最终值不可信，
 * 按未知展示（不保留旧 0）；响应次数是历史，照常展示。
 */
export function sssSubmissionRows(summary: SssSubmissionSummary): SssRow[] {
  const finalKnown = summary.reconciled === true
  return [
    {
      key: 'preconfirmed',
      label: '提交前已有站内确认',
      value: countText(summary.preconfirmed, '单'),
      tone: 'neutral',
    },
    {
      key: 'submitted',
      label: '本轮实际提交（不同任务）',
      value: countText(summary.submitted, '单'),
      tone: 'neutral',
    },
    {
      key: 'attempts',
      label: '本轮 POST 调用次数（含重登/人工重试）',
      value: countText(summary.attempts, '次'),
      tone: 'neutral',
    },
    {
      key: 'successResponses',
      label: '响应成功',
      value: countText(summary.successResponses, '次'),
      tone: (summary.successResponses ?? 0) > 0 ? 'success' : 'neutral',
    },
    {
      key: 'technicalErrors',
      label: '技术异常 · 结果未知（未自动重发）',
      value: countText(summary.technicalErrors, '次'),
      tone: (summary.technicalErrors ?? 0) > 0 ? 'warning' : 'neutral',
    },
    {
      key: 'explicitRejections',
      label: '明确拒绝',
      value: countText(summary.explicitRejections, '次'),
      tone: (summary.explicitRejections ?? 0) > 0 ? 'warning' : 'neutral',
    },
    {
      key: 'notSent',
      label: '未发送（始终没有 POST）',
      value: countText(summary.notSent, '单'),
      tone: (summary.notSent ?? 0) > 0 ? 'warning' : 'neutral',
    },
    {
      key: 'newlyConfirmed',
      label: '本轮新增站内确认（对账差异）',
      value: countText(finalKnown ? summary.newlyConfirmed : null, '单'),
      tone: 'neutral',
    },
    {
      key: 'confirmed',
      label: '最终站内确认 / 目标',
      value: pairText(finalKnown ? summary.confirmed : null, summary.targetTotal, '单'),
      tone: 'neutral',
    },
    {
      key: 'unconfirmed',
      label: '未确认',
      value: countText(finalKnown ? summary.unconfirmed : null, '单'),
      tone: finalKnown && (summary.unconfirmed ?? 0) > 0 ? 'warning' : 'neutral',
    },
  ]
}

/** 确认类计数是否可信（对账完成、目标与确认都已知且相等）。 */
export function sssSubmissionAllConfirmed(summary: SssSubmissionSummary): boolean {
  if (!summary.consistent) return false
  if (summary.reconciled !== true) return false
  if (summary.unconfirmed !== 0) return false
  if (summary.confirmed === null || summary.targetTotal === null) return false
  return summary.confirmed === summary.targetTotal
}

/** 还存在未确认风险（未知也算风险，不按 0 处理）。 */
export function sssSubmissionPending(summary: SssSubmissionSummary): boolean {
  return summary.reconciled !== true || summary.unconfirmed !== 0 || !summary.consistent
}

/**
 * 卡片脚注：说明口径，并把「成功但暂不可见」与「内部异常」分开讲清楚。
 *
 * 非正式运行（模拟/预检/余额闸门/无单）只说明“没有提交任何请求”，不提示补单。
 */
export function sssSubmissionNotes(
  summary: SssSubmissionSummary,
  options: { kind?: SssRunKind } = {},
): string[] {
  const kind = options.kind || 'live'
  if (kind !== 'live') {
    return [
      '这次运行没有提交任何下单请求（模拟 / 预检 / 余额闸门 / 无单），站内不会因此新增订单。',
      '确认为未知时不按 0 计；下面的计数只描述本次组装或只读核对的规模。',
    ]
  }
  const notes: string[] = []
  if ((summary.successResponses ?? 0) > 0 && sssSubmissionPending(summary)) {
    notes.push(
      `共收到 ${summary.successResponses} 次成功响应；仍有未确认项时，无法从这个次数判断`
      + '其中多少只是尚未显示在站内列表（平台列表有延迟）：优先等待并只读复查，'
      + '不要因为看不到就自动重发。',
    )
  }
  if ((summary.technicalErrors ?? 0) > 0) {
    notes.push(
      `技术异常（${summary.technicalErrors} 次）是内部错误导致的“结果未知”，`
      + '不代表订单确定创建失败；以站内列表为准。',
    )
  }
  const auth = summary.authRejections
  const balance = summary.balanceRejections
  if (auth !== null || balance !== null) {
    notes.push(
      `明确拒绝明细：登录失效 ${auth === null ? UNKNOWN_TEXT : `${auth} 次`}`
      + ` · 余额不足 ${balance === null ? UNKNOWN_TEXT : `${balance} 次`}。`,
    )
  }
  if (summary.newlyConfirmed !== null) {
    notes.push('「本轮新增站内确认」= 最终确认集合 − 初始已确认集合，是对账差异，不表示本轮新建了这么多单。')
  }
  if (sssSubmissionPending(summary)) {
    notes.push('「核对并补单」不是只读操作：先做站内对账，仍缺失的订单会真实提交（可能产生费用）。')
  }
  notes.push('计数以站内订单列表对账为准：响应失败不代表未创建；平台列表有延迟时仍可能出现重复。')
  if (!summary.consistent) {
    notes.push(`服务端计数不自洽（${summary.issues.join('；')}），请按未知处理并只读核对。`)
  }
  return notes
}

/** 收尾提示里的一句话：区分「成功但暂不可见」与「内部异常」（不自动重发）。 */
export function sssSubmissionNote(summary: SssSubmissionSummary): string {
  const parts: string[] = []
  if ((summary.successResponses ?? 0) > 0 && sssSubmissionPending(summary)) {
    parts.push(
      `共收到 ${summary.successResponses} 次成功响应；仍有未确认项时，无法判断其中多少只是`
      + '尚未显示在站内列表（平台列表有延迟）：优先等待并只读复查，不要因为看不到就自动重发。',
    )
  }
  if ((summary.technicalErrors ?? 0) > 0) {
    const head = parts.length > 0 ? `另有 ${summary.technicalErrors} 次` : `${summary.technicalErrors} 次`
    parts.push(`${head}技术异常属内部错误、结果未知（未自动重发），不代表确定创建失败；以站内对账为准。`)
  }
  if (parts.length > 0) return parts.join(' ')
  if (summary.unconfirmed === null) {
    return '本站未返回未确认数量：按待核对处理，不按 0 计。'
  }
  return ''
}

/**
 * 卡片标题/语气。
 *
 * `kind` 非 live（模拟/预检/余额闸门/无单）时只说明未提交，不提示补单；
 * 运行中的计数只是暂定；`reconciled !== true`、缺字段、计数矛盾一律待核对。
 */
export function sssSubmissionHeadline(
  summary: SssSubmissionSummary,
  options: { running?: boolean; kind?: SssRunKind } = {},
): SssHeadline {
  const kind = options.kind || 'live'
  if (kind === 'dry_run') {
    return {
      key: 'dry-run',
      tone: 'info',
      title: '本次为模拟执行，未发送任何下单请求',
      detail: '模拟只组装并预览报文，站内不会新增订单；下面的计数不代表本轮创建。',
    }
  }
  if (kind === 'preflight') {
    return {
      key: 'preflight',
      tone: 'info',
      title: '本次仅预检（只读），未提交新订单',
      detail: '预检只读核对站内列表；确认为未知时不按 0 计。',
    }
  }
  if (kind === 'guarded') {
    return {
      key: 'guarded',
      tone: 'warning',
      title: '余额闸门：本批未提交任何 POST',
      detail: '任务在提交前已安全停止；请确认余额并只读核对后再运行。',
    }
  }
  if (kind === 'no_orders') {
    return {
      key: 'no-orders',
      tone: 'info',
      title: '本次没有需要下单的名单项，未提交订单',
      detail: '名单为空或都被过滤；无需补单。',
    }
  }
  if (options.running) {
    return {
      key: 'running',
      tone: 'info',
      title: '本轮提交统计（运行中 · 暂定）',
      detail: '运行中的计数还会变化，结束时以站内对账为准；不要按当前数字判断是否补单。',
    }
  }
  if (summary.reconciled === false) {
    return {
      key: 'pending',
      tone: 'warning',
      title: '站内对账未完成 · 待核对',
      detail: '最终确认数不可用（未知，不按 0 计）；可先「核对并补单」：对账后再只补仍缺失项。',
    }
  }
  if (summary.unconfirmed === null) {
    return {
      key: 'unknown',
      tone: 'warning',
      title: '未确认项待核对',
      detail: '服务端未返回未确认数量，不能按 0 处理；请以站内列表核对，或先对账再决定是否补单。',
    }
  }
  if (summary.unconfirmed > 0) {
    return {
      key: 'unconfirmed',
      tone: 'warning',
      title: `仍有 ${summary.unconfirmed} 项未确认`,
      detail: '以站内订单列表为准；可「核对并补单」：先对账、只补仍缺失项。响应失败不代表未创建。',
    }
  }
  if (sssSubmissionAllConfirmed(summary)) {
    return {
      key: 'confirmed',
      tone: 'success',
      title: `已全部站内确认（${summary.confirmed}/${summary.targetTotal}），无需补单`,
      detail: '本轮没有仍待补的订单；如需处理新名单，正常开始即可。',
    }
  }
  return {
    key: 'unknown',
    tone: 'warning',
    title: summary.consistent ? '确认数待核对' : '计数不自洽 · 待核对',
    detail: '最终确认数与目标不一致或不可信；请只读核对后再决定是否补单。',
  }
}

/**
 * 这次运行的类型。Bridge 的 `operation.status` 已归一化，所以同时读
 * `summary.status`（runner 原始状态）与 `summary.result` 里的 dry_run/semantics。
 */
export function sssRunKindFromSummary(summary: unknown, operationStatus = ''): SssRunKind {
  const record = asRecord(summary) || {}
  const result = asRecord(record.result) || {}
  const runner = String(record.status || '').trim().toLowerCase()
  const normalized = String(operationStatus || '').trim().toLowerCase()
  const semantics = String(record.semantics || result.semantics || '').trim().toLowerCase()
  if (runner === 'no_orders' || normalized === 'no_orders') return 'no_orders'
  if (semantics === 'preflight-only' || runner.startsWith('preflight') || normalized === 'preflight_ok') {
    return 'preflight'
  }
  if (semantics === 'pre-submit-balance-guard') return 'guarded'
  if (runner === 'dry_run' || normalized === 'dry_run'
    || record.dry_run === true || result.dry_run === true) {
    return 'dry_run'
  }
  if (runner === 'insufficient_balance' || runner === 'balance_unknown'
    || normalized === 'insufficient_balance' || normalized === 'balance_unknown') {
    return 'guarded'
  }
  return 'live'
}

export function sssRunKind(operation: OperationInfo | null | undefined): SssRunKind {
  if (!operation) return 'unknown'
  return sssRunKindFromSummary(operation.summary, String(operation.status || ''))
}

function timestampOf(value: unknown): number | null {
  if (typeof value === 'number' && Number.isFinite(value)) return value
  if (typeof value === 'string' && value.trim() !== '') {
    const parsed = Date.parse(value)
    if (Number.isFinite(parsed)) return parsed
    const numeric = Number(value)
    if (Number.isFinite(numeric)) return numeric
  }
  return null
}

function operationSubmission(operation: OperationInfo | null | undefined): SssSubmissionSummary | null {
  const summary = asRecord(operation?.summary)
  if (!summary) return null
  return parseSssSubmission(summary.submission)
}

export interface SssSubmissionSource {
  /** operation_status() 顶层权威操作。 */
  operation?: OperationInfo | null
  /** operation_status().operations（顺序不保证新→旧）。 */
  operations?: OperationInfo[] | null
}

export interface SssSubmissionPick {
  operation: OperationInfo | null
  submission: SssSubmissionSummary | null
  /** 运行中的 operation：卡片计数只能算暂定。 */
  running: boolean
  operationId: string
  kind: SssRunKind
}

function pickOf(operation: OperationInfo | null): SssSubmissionPick {
  if (!operation) {
    return { operation: null, submission: null, running: false, operationId: '', kind: 'unknown' }
  }
  return {
    operation,
    submission: operationSubmission(operation),
    running: Boolean(operation.active) || String(operation.status || '').toLowerCase() === 'running',
    operationId: operation.operation_id || '',
    kind: sssRunKind(operation),
  }
}

/**
 * 取本轮统计对应的闪时送（mode=sss）操作。
 *
 * 顶层权威 operation 是 sss 时以它为准（它已是“活动 > 最新完成”），没有统计就暂不展示，
 * 不用上一轮的数字冒充本轮；顶层是其它模式时才回退到 operations 里**按时间最新**的 sss
 * （列表顺序不做假设）。其它模式的陈旧状态不改卡片也不改主动作。
 */
export function pickSssSubmission(source: SssSubmissionSource): SssSubmissionPick {
  const authority = source.operation && String(source.operation.mode || '') === 'sss'
    ? source.operation
    : null
  if (authority) return pickOf(authority)
  const candidates = (source.operations || [])
    .map((item, index) => ({
      item,
      index,
      ts: timestampOf(item?.finished_at) ?? timestampOf(item?.started_at),
    }))
    .filter((entry) => entry.item && String(entry.item.mode || '') === 'sss')
  if (candidates.length === 0) return pickOf(null)
  candidates.sort((left, right) => {
    if (left.ts !== null && right.ts !== null && left.ts !== right.ts) return right.ts - left.ts
    if (left.ts === null && right.ts !== null) return 1
    if (left.ts !== null && right.ts === null) return -1
    return left.index - right.index
  })
  return pickOf(candidates[0].item)
}

export interface SssFollowUpInput extends SssSubmissionSource {
  connection: ConnectionState
  recovery?: RecoveryState
  /** 是否有互斥操作在跑（`operationActive`）。 */
  operationActive: boolean
  /** 当前表单的执行方式。 */
  executionMode: 'dry_run' | 'preflight' | 'live'
}

function normal(reason: SssFollowUpReason, reasonText: string,
                pick: SssSubmissionPick): SssFollowUpView {
  return {
    reconcile: false,
    label: '',
    reason,
    reasonText,
    operationId: pick.operationId,
    submission: pick.submission,
  }
}

/**
 * 「核对并补单」主动作判定：连接正常、没有互斥任务、当前是正式下单，
 * 且最近一次**真实**闪时送运行还有未确认项时才提供。
 */
export function sssFollowUpAction(input: SssFollowUpInput): SssFollowUpView {
  const pick = pickSssSubmission(input)
  if (input.recovery === 'checking' || input.recovery === 'unavailable') {
    return normal('recovery-pending', '正在向服务端核对权威状态，确认后再决定是否补单。', pick)
  }
  if (input.connection !== 'connected') {
    return normal('offline', '连接中断，恢复并确认权威状态前不提供补单。', pick)
  }
  if (input.operationActive) {
    return normal('busy', '有任务正在运行，等待结束后再核对补单。', pick)
  }
  if (input.executionMode !== 'live') {
    return normal('mode-not-live', '仅正式下单提供核对并补单；模拟/预检不会创建订单。', pick)
  }
  if (!pick.operation) {
    return normal('no-finished-run', '还没有已结束的闪时送运行，正常开始即可。', pick)
  }
  if (pick.running) {
    return normal('no-finished-run', '闪时送任务仍在运行，结束后再核对补单。', pick)
  }
  const status = String(pick.operation.status || '').toLowerCase()
  if (status === 'blocked_uncertain' || status === 'blocked') {
    // 阻断态按运行日志处理：没有“再运行一次”的入口（3.6.19 起没有人工解除闸门）。
    return normal('blocked', '任务被阻断：请按运行日志里的提示处理后再运行。', pick)
  }
  if (status === 'blocked_concurrent' || status === 'no_orders'
    || status === 'not_started' || status === 'rejected') {
    return normal('not-real-run', '这次运行没有提交订单（并发阻断/无单/未开始/被拒绝）。', pick)
  }
  if (pick.kind !== 'live') {
    return normal('not-real-run', '这次运行不是正式下单（模拟/预检/余额闸门），不提供补单。', pick)
  }
  const submission = pick.submission
  const risky = submission ? sssSubmissionPending(submission)
    // 旧后端没有 submission：只在“可能已提交但未确认”的状态上提供，成功/无变化不提供。
    : status === 'uncertain' || status === 'partial' || status === 'stopped' || status === 'recovered'
  if (!risky) {
    if (submission && sssSubmissionAllConfirmed(submission)) {
      return normal('all-confirmed', '本轮已全部站内确认，无需补单。', pick)
    }
    return normal('nothing-unconfirmed', '没有未确认项，正常开始即可。', pick)
  }
  return {
    reconcile: true,
    label: SSS_RECONCILE_LABEL,
    reason: 'eligible',
    reasonText: '这次正式运行存在未确认项：先按当前账号与名单做站内对账，只补仍缺失的订单。',
    operationId: pick.operationId,
    submission,
  }
}

export interface SssSendGuard {
  /** true = 当前状态不允许发起真实下单（断线/正在核对/已有任务）。 */
  blocked: boolean
  /** 简短原因（可直接拼进按钮文案与弹窗告警）。 */
  reason: string
}

/** 真实下单的统一前置闸门：断线/恢复未确认/有互斥任务时一律不发请求。 */
export function sssSendGuard(input: {
  connection: ConnectionState
  recovery?: RecoveryState
  operationActive: boolean
}): SssSendGuard {
  if (input.operationActive) return { blocked: true, reason: '已有操作进行中' }
  if (input.recovery === 'checking' || input.recovery === 'unavailable') {
    return { blocked: true, reason: '正在核对权威状态' }
  }
  if (input.connection === 'connecting') return { blocked: true, reason: '正在连接' }
  if (input.connection !== 'connected') return { blocked: true, reason: '连接中断' }
  return { blocked: false, reason: '' }
}

/** 主动作按钮文字（名单预览优先，其次是发送闸门，再是补单/正常开始）。 */
export function sssPrimaryLabel(input: {
  operationActive: boolean
  needsDayPreview: boolean
  dayLoading: boolean
  executionMode: 'dry_run' | 'preflight' | 'live'
  followUp: SssFollowUpView
  sendBlocked?: boolean
  sendBlockReason?: string
}): string {
  if (input.operationActive) return '已有操作进行中'
  if (input.needsDayPreview) return input.dayLoading ? '正在读取云端名单…' : '先读取云端名单'
  if (input.sendBlocked) return `${input.sendBlockReason || '连接未确认'}，暂不能提交`
  if (input.followUp.reconcile) return input.followUp.label || SSS_RECONCILE_LABEL
  if (input.executionMode === 'live') return '开始正式下单'
  if (input.executionMode === 'preflight') return '开始预检'
  return '开始模拟执行'
}
