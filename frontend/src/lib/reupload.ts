/**
 * 「核对并补传」的纯展示逻辑（不碰网络，Node 单测可直接覆盖）。
 *
 * 所有判断只读后端结构化字段（status / missing / counts），不解析文本、不自造时间。
 */
import type {
  WpsReuploadCheckResult,
  WpsReuploadCounts,
  WpsReuploadOrder,
  WpsReuploadOrderStatus,
  WpsReuploadSheetStatus,
  WpsReuploadTable,
  WpsReuploadUploadResult,
} from './bridge.ts'
import { parseIsoMs } from './preview.ts'

export type ReuploadTone = 'info' | 'success' | 'warning' | 'danger' | 'neutral'

const ORDER_STATUS_TEXT: Record<WpsReuploadOrderStatus, string> = {
  present: '已在云端',
  missing_row: '缺整行',
  missing_meals: '总餐次不足',
  missing_mark: '缺日期格',
  missing_fields: '缺行内字段',
  manual: '无法判定',
}

export function reuploadOrderStatusText(status: string): string {
  return ORDER_STATUS_TEXT[status as WpsReuploadOrderStatus] ?? status ?? '未知'
}

/** 「缺什么」的人类可读文本；空数组 = 不缺。 */
export function reuploadOrderMissingText(order: WpsReuploadOrder): string {
  const parts: string[] = []
  const missing = Array.isArray(order?.missing) ? order.missing : []
  if (missing.includes('row')) parts.push('缺整行')
  if (missing.includes('meals')) {
    const meals = Number(order?.missing_meals ?? 0)
    parts.push(meals > 0 ? `少 ${meals} 餐` : '总餐次不足')
  }
  if (missing.includes('mark')) parts.push('缺日期格')
  if (missing.includes('fields')) parts.push('缺类型/餐种/公式')
  if (parts.length === 0) return '不缺'
  return parts.join('、')
}

export function reuploadSheetTone(status: WpsReuploadSheetStatus | string): ReuploadTone {
  switch (status) {
    case 'complete':
      return 'success'
    case 'ready':
      return 'info'
    case 'partial':
      return 'warning'
    case 'manual':
      return 'warning'
    case 'blocked':
      return 'danger'
    default:
      return 'neutral'
  }
}

export function reuploadSheetStatusText(status: WpsReuploadSheetStatus | string): string {
  switch (status) {
    case 'complete':
      return '已完整'
    case 'ready':
      return '可补传'
    case 'partial':
      return '部分可补'
    case 'manual':
      return '无法判定'
    case 'blocked':
      return '不可补传'
    default:
      return status || '未知'
  }
}

/** 该表是否有可勾选补传的订单。 */
export function reuploadSelectable(table: WpsReuploadTable | undefined | null): boolean {
  if (!table) return false
  if (table.status !== 'ready' && table.status !== 'partial') return false
  return Number(table.counts?.to_resend ?? 0) > 0
}

/** 默认勾选：所有可补传的表。 */
export function reuploadInitialSelection(check: WpsReuploadCheckResult | null): string[] {
  const tables = Array.isArray(check?.tables) ? check.tables : []
  return tables.filter((table) => reuploadSelectable(table)).map((table) => table.sheet)
}

/** 选中表的待补传订单数（按表内计数求和，不重新解析订单）。 */
export function reuploadSelectedCount(
  check: WpsReuploadCheckResult | null,
  selected: string[],
): number {
  const wanted = new Set(Array.isArray(selected) ? selected : [])
  const tables = Array.isArray(check?.tables) ? check.tables : []
  return tables
    .filter((table) => wanted.has(table.sheet) && reuploadSelectable(table))
    .reduce((sum, table) => sum + Number(table.counts?.to_resend ?? 0), 0)
}

/** 汇总行：需补传 N 单（M 表）· 已在云端 · 无法判定。 */
export function reuploadSummaryText(check: WpsReuploadCheckResult | null): string {
  const summary = check?.summary
  if (!summary) return '尚未核对'
  const parts = [
    `需补传 ${Number(summary.to_resend ?? 0)} 单`,
    `已在云端 ${Number(summary.present ?? 0)} 单`,
  ]
  if (Number(summary.manual ?? 0) > 0) parts.push(`无法判定 ${Number(summary.manual ?? 0)} 单`)
  const writeable = Number(summary.writeable_sheets ?? 0)
  if (writeable > 0) parts.push(`${writeable} 张表可补传`)
  return parts.join(' · ')
}

/** 是否允许提交补传。 */
export function reuploadCanSubmit(
  check: WpsReuploadCheckResult | null,
  selected: string[],
  now = Date.now(),
): boolean {
  if (!check?.ok) return false
  if (!check.check_id) return false
  if (!reuploadTokenFresh(check, now).fresh) return false
  return reuploadSelectedCount(check, selected) > 0
}

export interface ReuploadTokenFreshness {
  fresh: boolean
  reason: string
  expiresAtMs: number | null
}

/** 核对令牌有效期（只用服务端 expires_at，不自造时间）。 */
export function reuploadTokenFresh(
  check: WpsReuploadCheckResult | null,
  now = Date.now(),
): ReuploadTokenFreshness {
  const expiresAtMs = parseIsoMs(check?.expires_at)
  if (!check?.check_id) {
    return { fresh: false, reason: '尚未核对', expiresAtMs }
  }
  if (expiresAtMs === null) {
    return { fresh: false, reason: '核对结果缺少服务端过期时间，请重新核对', expiresAtMs }
  }
  if (now >= expiresAtMs) {
    return { fresh: false, reason: '核对结果已过期，请重新核对', expiresAtMs }
  }
  return { fresh: true, reason: '', expiresAtMs }
}

export interface ReuploadHeadline {
  title: string
  tone: ReuploadTone
  nextStep: string
  /** 是否可当作「已完成」。只有 ok 且逐表可核实成功才为 true。 */
  impliesComplete: boolean
}

/** 补传结果的标题/语气/下一步；未知结果绝不渲染成「已完成」。 */
export function reuploadHeadline(result: WpsReuploadUploadResult | null): ReuploadHeadline {
  if (!result) return { title: '尚未补传', tone: 'neutral', nextStep: '', impliesComplete: false }
  const status = String(result.status || '')
  if (status === 'noop') {
    return { title: '无需补传', tone: 'neutral', nextStep: '云端已与上次上传的期望一致', impliesComplete: true }
  }
  if (result.ok && (status === 'ok' || status === 'success')) {
    return { title: '补传完成', tone: 'success', nextStep: '已逐格回读核对；再核对一次可确认全部已在云端', impliesComplete: true }
  }
  if (status === 'partial') {
    return { title: '补传部分完成', tone: 'warning', nextStep: '查看下面未完成的表；可重新核对后只补剩下的', impliesComplete: false }
  }
  if (status === 'uncertain') {
    return { title: '补传结果不确定', tone: 'danger', nextStep: '不要直接重试；先看每张表的说明并核对云端', impliesComplete: false }
  }
  if (status === 'blocked') {
    return { title: '补传被阻断', tone: 'danger', nextStep: '按说明处理后重新核对', impliesComplete: false }
  }
  return { title: '补传未完成', tone: result.ok ? 'warning' : 'danger', nextStep: '查看原因后重新核对', impliesComplete: false }
}

/** 逐表「这次实际写入」的可读状态。 */
export function reuploadWriteStatusText(table: WpsReuploadTable): string {
  const write = table.write
  if (!write) return '未执行'
  const status = String(write.status || '')
  if (status === 'ok' || status === 'verified') return '已写入并回读通过'
  if (status === 'noop') return '无需写入'
  if (status === 'failed') return write.reason || '写入失败（未写入）'
  if (status === 'uncertain') return write.reason || '写入结果不确定'
  if (status === 'skipped') return '已跳过'
  if (status === 'blocked' || status === 'stale_batch') return write.reason || '被阻断'
  return write.reason || status || '未执行'
}

export function reuploadOrderLine(order: WpsReuploadOrder): string {
  const parts: string[] = []
  if (order.order_no) parts.push(order.order_no)
  if (order.name) parts.push(order.name)
  return parts.join(' ')
}

export function reuploadCountsZero(): WpsReuploadCounts {
  return { total: 0, to_resend: 0, present: 0, manual: 0 }
}
