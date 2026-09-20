/**
 * 云同步预览的严格前端契约与本地失效键。
 *
 * 时间只来自后端 created_at / expires_at（ISO 字符串），禁止前端自造时间。
 * 统计只来自结构化 summary/tables/blocked/warnings，禁止解析 text。
 * 本地失效键用于“任一输入/配置/源表/模式变化立即作废”，后端 upload 仍会做最终闸门。
 */
import type {
  WpsBlockedItem,
  WpsPreviewResult,
  WpsStructuredTable,
  WpsTableCounts,
} from './bridge.ts'

export interface PreviewCounts {
  update: number
  append: number
  unchanged: number
  skipped: number
  warned: number
  blocked: number
}

export interface PreviewHandle {
  preview: WpsPreviewResult
  /** 预览请求成功时冻结的表单/目标/模式指纹。 */
  localKey: string
}

export interface PreviewFreshness {
  fresh: boolean
  reason: string
  createdAtMs: number | null
  expiresAtMs: number | null
}

export function stableStringify(value: unknown): string {
  if (value === null || typeof value !== 'object') return JSON.stringify(value)
  if (Array.isArray(value)) return `[${value.map((item) => stableStringify(item)).join(',')}]`
  const record = value as Record<string, unknown>
  return `{${Object.keys(record).sort().map((key) =>
    `${JSON.stringify(key)}:${stableStringify(record[key])}`).join(',')}}`
}

export function previewLocalKey(parts: Record<string, unknown>): string {
  return stableStringify(parts)
}

function emptyCounts(): PreviewCounts {
  return { update: 0, append: 0, unchanged: 0, skipped: 0, warned: 0, blocked: 0 }
}

function addCounts(total: PreviewCounts, counts: Partial<WpsTableCounts> | undefined): void {
  if (!counts) return
  total.update += Number(counts.to_update ?? 0)
  total.append += Number(counts.to_append ?? 0)
  total.unchanged += Number(counts.unchanged ?? 0)
  total.skipped += Number(counts.skipped ?? 0)
  total.warned += Number(counts.warned ?? 0)
  total.blocked += Number(counts.blocked ?? 0)
}

/**
 * 统计口径：
 * - 优先按结构化 `tables[].counts` 求和；
 * - 没有 tables（失败返回/兼容）时才用 summary/stats；
 * - blocked 只来自 `blocked.length` 或结构化 tables[].counts.blocked，绝不解析 text。
 */
export function previewCounts(preview: WpsPreviewResult | null): PreviewCounts | null {
  if (!preview || !preview.ok) return null
  const total = emptyCounts()
  const tables: WpsStructuredTable[] = Array.isArray(preview.tables) ? preview.tables : []
  if (tables.length > 0) {
    for (const table of tables) addCounts(total, table.counts)
    const blockedFromArray = Array.isArray(preview.blocked) ? preview.blocked.length : 0
    total.blocked = Math.max(total.blocked, blockedFromArray)
    return total
  }
  const summary = preview.summary ?? preview.stats
  if (!summary) return null
  total.update = Number(summary.to_update ?? 0)
  total.append = Number(summary.to_append ?? 0)
  total.unchanged = Number(summary.unchanged ?? 0)
  total.skipped = Number(summary.skipped ?? 0)
  total.warned = Number(summary.warned ?? 0)
  total.blocked = Number(summary.blocked ?? (Array.isArray(preview.blocked) ? preview.blocked.length : 0))
  return total
}

export function blockedItems(preview: WpsPreviewResult | null): WpsBlockedItem[] {
  return Array.isArray(preview?.blocked) ? preview.blocked.filter((item) => item?.sheet) : []
}

export function previewWarnings(preview: WpsPreviewResult | null): string[] {
  return Array.isArray(preview?.warnings) ? preview.warnings.filter(Boolean) : []
}

export function parseIsoMs(value: string | undefined): number | null {
  if (!value) return null
  const ms = Date.parse(value)
  return Number.isFinite(ms) ? ms : null
}

export function previewFreshness(
  handle: PreviewHandle | null,
  currentLocalKey: string,
  now = Date.now(),
): PreviewFreshness {
  if (!handle) return { fresh: false, reason: '尚未生成预览', createdAtMs: null, expiresAtMs: null }
  const { preview, localKey } = handle
  const createdAtMs = parseIsoMs(preview.created_at)
  const expiresAtMs = parseIsoMs(preview.expires_at)
  if (!preview.ok || !preview.preview_id) {
    return { fresh: false, reason: preview.reason || '预览未成功', createdAtMs, expiresAtMs }
  }
  if (createdAtMs === null || expiresAtMs === null) {
    return { fresh: false, reason: '预览缺少服务端时间字段，请重新预览', createdAtMs, expiresAtMs }
  }
  if (localKey !== currentLocalKey) {
    return { fresh: false, reason: '配置或输入已变化，请重新预览', createdAtMs, expiresAtMs }
  }
  if (!(now < expiresAtMs)) {
    return { fresh: false, reason: '预览已过期，请重新预览', createdAtMs, expiresAtMs }
  }
  return { fresh: true, reason: '', createdAtMs, expiresAtMs }
}

export interface UploadGateInput {
  hasValidToken: boolean
  wpsEnabled: boolean
  cliFound: boolean
  authenticated: boolean
  busy: boolean
  handle: PreviewHandle | null
  currentLocalKey: string
  now?: number
}

/** 前端上传闸门：无 token、未授权、预览缺失/过期/变化时一律禁用。 */
export function uploadGate(input: UploadGateInput): { ok: boolean; reason: string } {
  if (!input.hasValidToken) return { ok: false, reason: '登录令牌无效：请用带有效 token 的完整网址重新打开' }
  if (!input.wpsEnabled) return { ok: false, reason: '云文档同步未启用' }
  if (!input.cliFound) return { ok: false, reason: '未找到云同步组件，请检查高级设置中的组件路径' }
  if (!input.authenticated) return { ok: false, reason: '尚未授权云文档，请先完成授权' }
  if (input.busy) return { ok: false, reason: '已有操作正在执行，请等待结束或查询操作状态' }
  if (!input.handle?.preview.preview_id) return { ok: false, reason: '缺少 preview_id，请先预览' }
  const fresh = previewFreshness(input.handle, input.currentLocalKey, input.now)
  if (!fresh.fresh) return { ok: false, reason: fresh.reason }
  return { ok: true, reason: '' }
}

const RELOAD_CODES = new Set([
  'missing_preview',
  'preview_not_found',
  'preview_expired',
  'preview_consumed',
  'preview_changed',
  'preview_invalidated',
])

export interface UploadNextAction {
  code: string
  label: string
  detail: string
  tone: 'warning' | 'danger' | 'info' | 'neutral'
}

/** 根据后端上传失败的机器码给“重新预览/重新核对”，不把拒绝渲染成失败重跑。 */
export function uploadNextAction(result: {
  code?: string
  status?: string
  reason?: string
  next_action?: string
} | null | undefined): UploadNextAction {
  const code = String(result?.code || '')
  if (RELOAD_CODES.has(code)) {
    return {
      code,
      label: '重新预览',
      detail: result?.next_action || result?.reason || '请重新生成预览后再上传',
      tone: 'warning',
    }
  }
  if (code === 'operation_conflict' || result?.status === 'rejected') {
    return {
      code: code || 'operation_conflict',
      label: '查看操作状态',
      detail: result?.next_action || result?.reason || '等待当前互斥操作结束后再决定。',
      tone: 'warning',
    }
  }
  if (result?.status === 'uncertain') {
    return {
      code: code || 'uncertain',
      label: '重新核对',
      detail: result?.next_action || result?.reason || '结果不确定，请先核对云端与日志，不要重复提交。',
      tone: 'warning',
    }
  }
  if (result?.status === 'partial') {
    return {
      code: code || 'partial',
      label: '重新核对',
      detail: result?.next_action || result?.reason || '部分表未确认，请核对日志与副本后在云端确认。',
      tone: 'warning',
    }
  }
  if (result?.status === 'recovered' || result?.status === 'not_started') {
    return {
      code: code || String(result?.status || ''),
      label: '重新预览',
      detail: result?.next_action || result?.reason || '需重新只读预览后再决定是否上传。',
      tone: 'warning',
    }
  }
  if (result?.status === 'blocked') {
    return {
      code: code || 'blocked',
      label: '重新核对',
      detail: result?.next_action || result?.reason || '存在阻断：先核对/修复本地日志与账本，不要重复上传。',
      tone: 'danger',
    }
  }
  return {
    code: code || 'failed',
    label: '重新预览',
    detail: result?.next_action || result?.reason || '本次未完成，请重新预览后再决定。',
    tone: 'danger',
  }
}
