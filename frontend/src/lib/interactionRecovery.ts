/**
 * 待处理交互（decision / captcha / address_input）的断线、刷新、重连安全逻辑。
 *
 * 当前后端没有 pending interaction 只读接口，因此本模块遵循“无恢复接口”分支：
 * - 不伪造恢复成功；
 * - 不自动重复提交；
 * - 连接恢复后明确要求重新核对；
 * - 用户输入保留在内存中，提交失败可安全重试；
 * - sessionStorage 只保存 kind/id/operation_id/updated_at 这类安全元数据，
 *   不保存验证码图片、地址条目、客户姓名等业务内容。
 *
 * 纯逻辑模块，Node 测试可直接覆盖。
 */
import type { ConnectionState, RecoveryState } from './operationStatus.ts'
import type {
  AddressInputRequest,
  CaptchaRequest,
  DecisionChoice,
  DecisionRequest,
  PendingAddressItem,
  PendingInteractionItem,
} from './bridge.ts'

export type InteractionKind = 'decision' | 'captcha' | 'address_input'

export interface PendingInteractionMeta {
  kind: InteractionKind
  id: string
  operationId: string
  updatedAt: number
}

export interface StorageLike {
  getItem(key: string): string | null
  setItem(key: string, value: string): void
  removeItem(key: string): void
}

export const PENDING_INTERACTION_KEY = 'yikou.pending-interaction.v1'

export function interactionKindLabel(kind: InteractionKind): string {
  if (kind === 'captcha') return '验证码'
  if (kind === 'address_input') return '地址补录'
  return '决策'
}

export function readPendingInteractionMeta(
  storage: StorageLike | null | undefined,
): PendingInteractionMeta | null {
  if (!storage) return null
  try {
    const raw = storage.getItem(PENDING_INTERACTION_KEY)
    if (!raw) return null
    const data = JSON.parse(raw) as Partial<PendingInteractionMeta>
    if (data.kind !== 'decision' && data.kind !== 'captcha' && data.kind !== 'address_input') return null
    if (typeof data.id !== 'string' || !data.id) return null
    return {
      kind: data.kind,
      id: data.id,
      operationId: typeof data.operationId === 'string' ? data.operationId : '',
      updatedAt: typeof data.updatedAt === 'number' && Number.isFinite(data.updatedAt) ? data.updatedAt : 0,
    }
  } catch {
    return null
  }
}

export function writePendingInteractionMeta(
  storage: StorageLike | null | undefined,
  meta: PendingInteractionMeta | null,
): void {
  if (!storage) return
  try {
    if (!meta) {
      storage.removeItem(PENDING_INTERACTION_KEY)
      return
    }
    storage.setItem(PENDING_INTERACTION_KEY, JSON.stringify(meta))
  } catch {
    // 隐私模式/配额满：不影响本次会话，恢复能力退化为“内存态”。
  }
}

export function interactionMetaFromRequest(
  kind: InteractionKind,
  id: string,
  operationId: string,
  now = Date.now(),
): PendingInteractionMeta {
  return { kind, id, operationId, updatedAt: now }
}

export interface InteractionRecoveryView {
  visible: boolean
  title: string
  detail: string
  /** 永远 false：没有后端只读恢复接口时，不允许任何自动提交。 */
  autoSubmit: false
  canRetry: boolean
  canStop: boolean
  tone: 'warning' | 'info'
}

export function interactionRecoveryView(input: {
  hasLocalRequest: boolean
  hasPersistedMeta: boolean
  connection: ConnectionState
  recovery: RecoveryState
  operationActive: boolean
  hasServerReadApi?: boolean
}): InteractionRecoveryView {
  const hasPending = input.hasLocalRequest || input.hasPersistedMeta
  if (!hasPending) {
    return { visible: false, title: '', detail: '', autoSubmit: false, canRetry: false, canStop: false, tone: 'info' }
  }
  const common = {
    autoSubmit: false as const,
    canStop: input.operationActive,
    tone: 'warning' as const,
  }
  const apiAvailable = input.hasServerReadApi === true

  if (apiAvailable && input.connection === 'connected' && input.recovery === 'idle') {
    if (!input.hasLocalRequest && input.hasPersistedMeta) {
      return {
        ...common,
        visible: true,
        title: '服务端没有可恢复的该交互，需要重新核对',
        detail: '只读 pending_interactions 已查询，但未返回该项；它可能已解决、已过期、已取消或无权限。恢复不等于自动提交，本页不会重放请求。',
        canRetry: false,
      }
    }
    // 本地弹窗已恢复：弹窗自身显示待确认状态，不额外占用顶部空间。
    return { visible: false, title: '', detail: '', autoSubmit: false, canRetry: true, canStop: input.operationActive, tone: 'info' }
  }

  if (input.connection === 'connecting') {
    return {
      ...common,
      visible: true,
      title: '正在连接服务端，交互输入已保留',
      detail: apiAvailable
        ? '连接完成后会只读查询 pending_interactions 重新核对；查询不会自动提交。'
        : '连接完成后需要重新核对服务端是否仍在等待；本页不会自动提交。',
      canRetry: false,
    }
  }

  if (input.connection === 'disconnected') {
    return {
      ...common,
      visible: true,
      title: '连接已断开，交互输入已保留',
      detail: apiAvailable
        ? '恢复连接后会只读查询 pending_interactions；重新核对前不会自动重复提交。'
        : '后端待处理交互只读接口不可用。恢复连接后需要重新核对，系统不会自动重复提交。',
      canRetry: false,
    }
  }

  if (input.recovery === 'checking') {
    return {
      ...common,
      visible: true,
      title: '正在重新核对服务端状态',
      detail: apiAvailable
        ? '正在只读查询 pending_interactions；恢复的是弹窗内容，不代表已提交，也不会自动重试。'
        : '连接已恢复，但后端待处理交互只读接口不可用；核对完成也不会自动提交。',
      canRetry: false,
    }
  }

  if (input.recovery === 'recovered' || input.recovery === 'unavailable') {
    return {
      ...common,
      visible: true,
      title: input.recovery === 'recovered' ? '已重新核对服务端交互' : '服务端恢复接口不可用，需要重新核对',
      detail: apiAvailable
        ? '只读恢复只重建弹窗与输入提示，不等于自动提交；提交失败会保留输入，结果不确定请先查询。'
        : '无法从服务端确认该交互是否仍在等待；请查看日志与权威 operation_status 后再决定。你的输入已保留，系统不会自动重复提交。',
      canRetry: input.hasLocalRequest,
    }
  }

  if (input.hasPersistedMeta && !input.hasLocalRequest) {
    return {
      ...common,
      visible: true,
      title: '检测到未完成的交互，需要重新核对',
      detail: apiAvailable
        ? '只读恢复没有找到仍在等待的该项；可能已解决、 expired、取消或无权限。本地未保存客户数据，也不会自动重试。'
        : '页面刷新后无法自动恢复弹窗：本地仅保存了类型/ID/operation_id，未保存客户数据。请查看日志或等待任务超时，系统不会自动重试；必要时可安全停止任务。',
      canRetry: false,
    }
  }

  // connected + recovery idle + 本地弹窗仍存在：弹窗自身就展示待确认信息，不额外挡住界面。
  return { visible: false, title: '', detail: '', autoSubmit: false, canRetry: true, canStop: input.operationActive, tone: 'info' }
}

export interface SubmissionOutcome {
  clearRequest: boolean
  keepInput: boolean
  autoSubmit: false
  tone: 'resolved' | 'retryable' | 'ended'
  message: string
}

/** 统一 resolve_* 的提交结果语义：只有 ok=true 才算已解决。 */
export function submissionOutcome(input: {
  ok: boolean
  thrown?: boolean
  message?: string
}): SubmissionOutcome {
  if (input.ok) {
    return { clearRequest: true, keepInput: false, autoSubmit: false, tone: 'resolved', message: '' }
  }
  if (input.thrown) {
    const prefix = (input.message || '网络未确认').replace(/[。；;\s]+$/, '')
    return {
      clearRequest: false,
      keepInput: true,
      autoSubmit: false,
      tone: 'retryable',
      message: `${prefix}；输入已保留，恢复连接后请重新核对再提交；系统不会自动重试。`,
    }
  }
  return {
    clearRequest: true,
    keepInput: false,
    autoSubmit: false,
    tone: 'ended',
    message: input.message || '服务端已不再等待该交互（可能已完成或已过期）；请查看日志确认。',
  }
}

/** 同 id 防重复提交：返回 false 表示已有提交在途。 */
export function beginInteractionSubmit(pending: Set<string>, key: string): boolean {
  if (pending.has(key)) return false
  pending.add(key)
  return true
}

export function endInteractionSubmit(pending: Set<string>, key: string): void {
  pending.delete(key)
}


export function isDecisionKind(kind: string): boolean {
  return kind === 'order_retry' || kind === 'sss_retry'
    || kind === 'save_retry' || kind === 'close_confirm' || kind === 'decision'
}

export function isCaptchaKind(kind: string): boolean {
  return kind === 'captcha'
}

export function isAddressKind(kind: string): boolean {
  return kind === 'address_input'
}

/** 依 interaction_id 去重，并按 created_at 稳定排序；不修改服务端语义。 */
export function dedupePendingInteractions(items: PendingInteractionItem[]): PendingInteractionItem[] {
  const seen = new Set<string>()
  const result: PendingInteractionItem[] = []
  for (const item of items) {
    const id = String(item?.interaction_id || '').trim()
    if (!id || seen.has(id)) continue
    seen.add(id)
    result.push(item)
  }
  return result.sort((a, b) => Number(a.created_at || 0) - Number(b.created_at || 0))
}

function asString(value: unknown): string {
  return typeof value === 'string' ? value : ''
}

function toChoices(value: unknown): DecisionChoice[] {
  if (!Array.isArray(value)) return []
  const choices: DecisionChoice[] = []
  for (const raw of value) {
    if (!raw || typeof raw !== 'object') continue
    const item = raw as Record<string, unknown>
    const choiceValue = asString(item.value)
    const label = asString(item.label)
    if (!choiceValue || !label) continue
    const style = item.style === 'primary' || item.style === 'neutral' || item.style === 'danger'
      ? item.style
      : 'neutral'
    choices.push({ value: choiceValue, label, style })
  }
  return choices
}

function toAddressItems(value: unknown): PendingAddressItem[] {
  if (!Array.isArray(value)) return []
  const items: PendingAddressItem[] = []
  for (const raw of value) {
    if (!raw || typeof raw !== 'object') continue
    const item = raw as Record<string, unknown>
    const rawAddress = asString(item.raw_address)
    if (!rawAddress) continue
    const orderNumbers = Array.isArray(item.order_numbers)
      ? item.order_numbers.map((v) => String(v)).filter(Boolean)
      : []
    items.push({
      raw_address: rawAddress,
      order_numbers: orderNumbers,
      campus: asString(item.campus),
      confidence: asString(item.confidence),
      reason: asString(item.reason),
      suggested_point: asString(item.suggested_point),
    })
  }
  return items
}

export function decisionFromPendingItem(item: PendingInteractionItem): DecisionRequest | null {
  if (!isDecisionKind(String(item.kind))) return null
  const request = item.request || {}
  const title = asString(request.title)
  const message = asString(request.message)
  const choices = toChoices(request.choices)
  if (!title || !message || choices.length === 0) return null
  return { id: item.interaction_id, kind: item.kind as DecisionRequest['kind'], title, message, choices }
}

export function captchaFromPendingItem(item: PendingInteractionItem): CaptchaRequest | null {
  if (!isCaptchaKind(String(item.kind))) return null
  const image = asString((item.request || {}).image)
  if (!image) return null
  return { id: item.interaction_id, image }
}

export function addressFromPendingItem(item: PendingInteractionItem): AddressInputRequest | null {
  if (!isAddressKind(String(item.kind))) return null
  const request = item.request || {}
  const items = toAddressItems(request.items)
  if (items.length === 0) return null
  return {
    id: item.interaction_id,
    title: asString(request.title) || '地址待确认',
    message: asString(request.message),
    items,
  }
}
