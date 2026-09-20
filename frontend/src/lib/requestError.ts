/**
 * 统一请求错误分类。
 *
 * 前端所有 API 调用失败都归一到这里的短句 + 可执行下一步；
 * 页面不得再用中文关键词猜错误类型（例如 message.includes('超时')）。
 * 该模块是纯逻辑，供 bridge.ts 与 Node 测试直接复用。
 */

export type RequestErrorKind =
  | 'auth'
  | 'permission'
  | 'offline'
  | 'timeout'
  | 'server'
  | 'client'
  | 'unknown'

export interface RequestErrorOptions {
  status?: number
  retryable?: boolean
  cause?: unknown
}

export class RequestError extends Error {
  readonly kind: RequestErrorKind
  readonly status?: number
  readonly retryable: boolean
  override readonly cause?: unknown

  constructor(kind: RequestErrorKind, message: string, options: RequestErrorOptions = {}) {
    super(message)
    this.name = 'RequestError'
    this.kind = kind
    this.status = options.status
    this.retryable = options.retryable ?? (kind !== 'auth' && kind !== 'permission' && kind !== 'client')
    this.cause = options.cause
  }
}

export interface RequestErrorView {
  kind: RequestErrorKind
  title: string
  detail: string
  nextStep: string
  retryable: boolean
  /** 供“重试”按钮使用的动作短文案；不提供则不显示重试按钮。 */
  retryLabel?: string
}

function textOf(error: unknown): string {
  if (error instanceof Error) return error.message
  if (typeof error === 'string') return error
  try {
    return JSON.stringify(error)
  } catch {
    return String(error)
  }
}

function isAbortLike(error: unknown): boolean {
  if (!(error instanceof Error)) return false
  const name = error.name.toLowerCase()
  const text = error.message.toLowerCase()
  return name === 'aborterror' || name === 'timeouterror' || text.includes('timeout')
    || text.includes('timed out') || text.includes('超时')
}

function isOfflineLike(error: unknown): boolean {
  if (!(error instanceof Error)) return false
  const name = error.name.toLowerCase()
  const text = error.message.toLowerCase()
  return name === 'typeerror'
    || text.includes('failed to fetch')
    || text.includes('networkerror')
    || text.includes('network request failed')
    || text.includes('请求失败：无法连接')
}

function inferKind(error: unknown): RequestErrorKind {
  if (error instanceof RequestError) return error.kind
  if (isAbortLike(error)) return 'timeout'
  if (error && typeof error === 'object' && 'status' in error) {
    const status = Number((error as { status?: unknown }).status)
    if (Number.isFinite(status)) {
      if (status === 401) return 'auth'
      if (status === 403) return 'permission'
      if (status === 408 || status === 504 || status === 524) return 'timeout'
      if (status >= 500) return 'server'
      if (status >= 400) return 'client'
    }
  }
  if (isOfflineLike(error)) return 'offline'
  return 'unknown'
}

const VIEWS: Record<RequestErrorKind, Omit<RequestErrorView, 'kind'>> = {
  auth: {
    title: '登录状态已失效',
    detail: '服务端未接受当前登录令牌。',
    nextStep: '请用带有效 ?token= 的完整网址重新打开页面，再继续操作。',
    retryable: false,
  },
  permission: {
    title: '当前账号没有该权限',
    detail: '服务端拒绝了这个操作。',
    nextStep: '请联系管理员授权，或改用管理员账号操作。',
    retryable: false,
  },
  offline: {
    title: '网络已断开',
    detail: '页面暂时连不上运行任务的服务器。',
    nextStep: '检查手机网络或服务是否启动；恢复后页面会主动查询权威状态，不要重复提交。',
    retryable: true,
    retryLabel: '重新连接',
  },
  timeout: {
    title: '请求超时，结果未知',
    detail: '服务器未在等待时间内返回，但操作可能已经受理。',
    nextStep: '不要立即重跑任务；先查看日志或刷新状态，确认执行结果后再决定是否重试。',
    retryable: true,
    retryLabel: '刷新连接',
  },
  server: {
    title: '服务端暂时不可用',
    detail: '服务器处理请求时出错。',
    nextStep: '稍后重试；若任务正在执行，请先看日志确认状态，避免重复提交。',
    retryable: true,
    retryLabel: '重试',
  },
  client: {
    title: '请求参数无效',
    detail: '服务端拒绝了这次调用。',
    nextStep: '按字段提示修正后重试。',
    retryable: false,
  },
  unknown: {
    title: '操作未完成',
    detail: '发生了未识别的请求错误。',
    nextStep: '先查看运行日志；仍失败时再重试，避免重复提交正在执行的任务。',
    retryable: true,
    retryLabel: '重试',
  },
}

/** 把任意异常转换为统一错误视图。 */
export function classifyRequestError(error: unknown): RequestErrorView {
  const kind = inferKind(error)
  const fallback = VIEWS[kind]
  const raw = textOf(error).trim()
  // 服务端返回的业务短句可用于补充 detail，但不用来猜颜色或类型。
  const detail = raw && raw.length <= 120 && !raw.toUpperCase().includes('ABORT')
    ? raw
    : fallback.detail
  return { kind, ...fallback, detail }
}

/** 把未知异常归一为 RequestError，便于调用方保留 status / retryable。 */
export function toRequestError(error: unknown, status?: number): RequestError {
  if (error instanceof RequestError) {
    if (status === undefined || error.status !== undefined) return error
    return new RequestError(error.kind, error.message, { ...error, status })
  }
  const kind = inferKind(error)
  const view = classifyRequestError(error)
  return new RequestError(kind, view.detail || textOf(error), { status, cause: error })
}
