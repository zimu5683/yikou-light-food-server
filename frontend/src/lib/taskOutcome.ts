/**
 * task:done / task:error 结果的统一语义。
 *
 * 后端 `_task_outcome` 已提供 ok/success/real_order/stopped/partial/uncertain/blocked/
 * needs_review/result_status 等字段。前端不得只看 stopped/partial 就报成功：
 * uncertain/partial/failed/blocked 必须显示“待核对/未完成”，模拟/预检必须明确未下单。
 * 纯逻辑模块，Node 测试可直接覆盖。
 */
export type TaskOutcomeLevel = 'success' | 'info' | 'warning' | 'error'
export type TaskToastKind = 'success' | 'info' | 'warning' | 'error'

/** 结果对应的权威状态键，供 `setStatus` 复用（`blocked_concurrent` 不是失败）。 */
export type TaskOutcomeStatusKey =
  | 'success'
  | 'noop'
  | 'partial'
  | 'stopped'
  | 'dry_run'
  | 'preflight_ok'
  | 'no_orders'
  | 'insufficient_balance'
  | 'balance_unknown'
  | 'uncertain'
  | 'blocked_uncertain'
  | 'blocked_concurrent'
  | 'error'

export interface TaskOutcomeLike {
  message?: string
  status?: string
  result_status?: string
  ok?: boolean
  success?: boolean
  real_order?: boolean
  stopped?: boolean
  partial?: boolean
  uncertain?: boolean
  blocked?: boolean
  needs_review?: boolean
  next_action?: string
  reason?: string
}

export interface TaskOutcomeView {
  level: TaskOutcomeLevel
  toast: TaskToastKind
  title: string
  message: string
  needsReview: boolean
  isSuccess: boolean
  statusKey: TaskOutcomeStatusKey
}

function isPlainSuccess(payload: TaskOutcomeLike): boolean {
  // 仅当服务端明确 ok=true 且 success=true，且没有任何不确定/部分/停止/阻断标记时才叫成功。
  if (payload.ok !== true || payload.success !== true) return false
  if (payload.stopped || payload.partial || payload.uncertain || payload.blocked) return false
  const status = String(payload.status || payload.result_status || '').toLowerCase()
  return status === '' || status === 'success'
}

export function taskOutcomeView(payload: TaskOutcomeLike): TaskOutcomeView {
  const status = String(payload.result_status || payload.status || '').toLowerCase()
  const message = (payload.message || payload.next_action || payload.reason || '').trim()
  const withNext = (text: string): string => {
    if (!payload.next_action || text.includes(payload.next_action)) return text
    return `${text} ${payload.next_action}`
  }

  if (payload.uncertain || status === 'uncertain') {
    return {
      level: 'warning', toast: 'warning',
      title: '结果不确定 · 待核对',
      message: withNext(message || '任务结果不确定，需要人工核对；不要直接重试或重跑本批。'),
      needsReview: true, isSuccess: false, statusKey: 'uncertain',
    }
  }
  // R6-9：拿不到批次级跨进程锁时 runner 返回 blocked_concurrent，**未发送任何 POST**。
  // 必须排在 payload.blocked 之前，否则会被误判成“任务被阻断 · 待核对”。
  if (status === 'blocked_concurrent') {
    return {
      level: 'warning', toast: 'warning',
      title: '另一个任务正在运行',
      message: withNext(message || '本次没有发送任何下单请求；请等待后刷新，不要重复提交。'),
      needsReview: false, isSuccess: false, statusKey: 'blocked_concurrent',
    }
  }
  if (status === 'blocked_uncertain' || payload.blocked) {
    return {
      level: 'error', toast: 'error',
      title: '任务被阻断 · 待核对',
      message: withNext(message || '任务被阻断，需先只读核对站内订单与本地记录；未确认前不要重发。'),
      needsReview: true, isSuccess: false, statusKey: 'blocked_uncertain',
    }
  }
  if (payload.partial || status === 'partial') {
    return {
      level: 'warning', toast: 'warning',
      title: '部分完成 · 待核对',
      message: withNext(message || '任务部分完成，请只处理未完成项，不要整批重跑。'),
      needsReview: true, isSuccess: false, statusKey: 'partial',
    }
  }
  if (payload.stopped || status === 'stopped') {
    return {
      level: 'warning', toast: 'warning',
      title: '任务已停止 · 待核对',
      message: withNext(message || '任务已停止，请核对结果，不要整批重跑。'),
      needsReview: true, isSuccess: false, statusKey: 'stopped',
    }
  }
  if (status === 'insufficient_balance' || status === 'balance_unknown') {
    return {
      level: 'warning', toast: 'warning',
      title: status === 'insufficient_balance' ? '余额不足，本批未提交' : '余额未知，本批未提交',
      message: withNext(message || '本批未提交；请先只读核对后再决定是否运行。'),
      needsReview: true, isSuccess: false,
      statusKey: status === 'insufficient_balance' ? 'insufficient_balance' : 'balance_unknown',
    }
  }
  if (status === 'error' || status === 'failed' || status === 'blocked') {
    return {
      level: 'error', toast: 'error',
      title: '任务失败',
      message: withNext(message || '任务执行失败，请查看日志定位原因，不要直接重跑整批。'),
      needsReview: true, isSuccess: false, statusKey: 'error',
    }
  }
  if (status === 'dry_run') {
    return {
      level: 'info', toast: 'info',
      title: '模拟执行完成（未创建订单）',
      message: withNext(message || '未发送任何真实下单请求。'),
      needsReview: false, isSuccess: false, statusKey: 'dry_run',
    }
  }
  if (status === 'preflight_ok') {
    return {
      level: 'info', toast: 'info',
      title: '预检完成（未创建订单）',
      message: withNext(message || '只读检查完成，未提交新订单。'),
      needsReview: false, isSuccess: false, statusKey: 'preflight_ok',
    }
  }
  if (status === 'no_orders') {
    return {
      level: 'info', toast: 'info',
      title: '没有需要处理的订单',
      message: withNext(message || '无需操作。'),
      needsReview: false, isSuccess: false, statusKey: 'no_orders',
    }
  }
  if (status === 'noop') {
    return {
      level: 'info', toast: 'info',
      title: '已完成（无变化）',
      message: withNext(message || '任务执行完成，服务端未产生变更。'),
      needsReview: false, isSuccess: false, statusKey: 'noop',
    }
  }
  if (isPlainSuccess(payload)) {
    return {
      level: 'success', toast: 'success',
      title: payload.real_order === false ? '任务完成（非真实下单）' : '任务已完成',
      message: withNext(message || '任务已正常结束。'),
      needsReview: false, isSuccess: true, statusKey: 'success',
    }
  }
  return {
    level: 'warning', toast: 'warning',
    title: '任务未确认成功 · 待核对',
    message: withNext(message || '未收到明确的成功标记，请查看日志与 operation_status 后再决定下一步。'),
    needsReview: true, isSuccess: false, statusKey: 'error',
  }
}
