/**
 * 操作状态视图。
 *
 * 权威来源是后端 `operation_status()`（bridge_ready 也镜像同一结构）：
 * active/status/phase/mode/summary/next_action/reason。
 * `status` 事件和 worker_alive 只用于旧事件通道与线程兜底，不能覆盖权威操作记录。
 *
 * 后端 P0 后新增 noop/dry_run/preflight_ok/no_orders/insufficient_balance/
 * balance_unknown/uncertain/blocked_uncertain/recovered/not_started 等结果；
 * uncertain/partial/failed/blocked 绝不能显示成功。
 */
import type { OperationInfo, StatusState } from './bridge.ts'

export type ConnectionState = 'connecting' | 'connected' | 'disconnected'
export type RecoveryState = 'idle' | 'checking' | 'recovered' | 'unavailable'
export type OperationTone = 'neutral' | 'progress' | 'success' | 'warning' | 'danger' | 'info'

export interface OperationView {
  key:
    | 'connecting'
    | 'disconnected'
    | 'checking'
    | 'idle'
    | 'running'
    | 'stopping'
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
    | 'rejected'
    | 'recovered'
    | 'not_started'
    | 'updating'
  label: string
  detail: string
  tone: OperationTone
  busy: boolean
  needsReview: boolean
  canStop: boolean
  modeLabel: string
  nextAction: string
  operationId: string
  active: boolean
  finishedAt: number | null
}

type FallbackView = Pick<OperationView, 'key' | 'label' | 'detail' | 'tone' | 'busy' | 'needsReview' | 'canStop'>

const MODE_LABELS: Record<string, string> = {
  '': '空闲',
  order: '订单处理',
  sss: '闪时送下单',
  wps_upload: '云文档上传',
  wps_authorize: 'WPS 授权',
  wps_logout: 'WPS 退出授权',
  check_update: '检查更新',
  install_update: '安装更新',
  // R6 §9：只读入口也占互斥槽位，用于「谁在跑」的提示。
  wps_preview: '云文档预览',
  wps_check_copies: '云文档副本核对',
  sss_day_orders: '云端当天名单读取',
  wps_recovery_resolve: '旧任务恢复/退场',
}

export const STATUS_FALLBACK: Record<StatusState, FallbackView> = {
  ready: { key: 'idle', label: '就绪', detail: '没有正在执行的任务。', tone: 'neutral', busy: false, needsReview: false, canStop: false },
  running: { key: 'running', label: '执行中', detail: '任务正在服务端执行，日志会持续更新。', tone: 'progress', busy: true, needsReview: false, canStop: true },
  stopping: { key: 'stopping', label: '正在停止', detail: '已请求停止，正在等待当前操作安全结束。', tone: 'warning', busy: true, needsReview: false, canStop: false },
  success: { key: 'success', label: '已完成', detail: '任务已正常结束，可查看结果与日志。', tone: 'success', busy: false, needsReview: false, canStop: false },
  noop: { key: 'noop', label: '已完成（无变化）', detail: '任务执行完成，服务端未产生变更。', tone: 'success', busy: false, needsReview: false, canStop: false },
  partial: { key: 'partial', label: '部分完成 · 待核对', detail: '有未完成或结果不确定的项目。请先核对日志，只补处理未完成项，不要整批重跑。', tone: 'warning', busy: false, needsReview: true, canStop: false },
  stopped: { key: 'stopped', label: '已停止', detail: '任务已停止，可能仍有未处理项目。请核对日志后再单独补处理。', tone: 'warning', busy: false, needsReview: true, canStop: false },
  dry_run: { key: 'dry_run', label: '模拟完成（未创建订单）', detail: '未发送真实下单请求，请核对预览/日志。', tone: 'info', busy: false, needsReview: false, canStop: false },
  preflight_ok: { key: 'preflight_ok', label: '预检完成（未创建订单）', detail: '只读检查完成，未提交新订单。', tone: 'info', busy: false, needsReview: false, canStop: false },
  no_orders: { key: 'no_orders', label: '没有需要处理的订单', detail: '无需操作。', tone: 'info', busy: false, needsReview: false, canStop: false },
  insufficient_balance: { key: 'insufficient_balance', label: '余额不足，本批未提交', detail: '任务已安全停止，本批未提交；请先只读核对。', tone: 'warning', busy: false, needsReview: true, canStop: false },
  balance_unknown: { key: 'balance_unknown', label: '余额未知，本批未提交', detail: '任务已安全停止，本批未提交；请先确认余额并只读核对。', tone: 'warning', busy: false, needsReview: true, canStop: false },
  uncertain: { key: 'uncertain', label: '结果不确定 · 待核对', detail: '无法确认执行结果；请先只读核对云端与日志，不要直接重试或重跑本批。', tone: 'warning', busy: false, needsReview: true, canStop: false },
  blocked_uncertain: { key: 'blocked_uncertain', label: '任务被阻断 · 待核对', detail: '任务被阻断，未确认前不要重发；请在闪时送结果区「未决记录」面板查看未决记录并只读核对站内订单。', tone: 'danger', busy: false, needsReview: true, canStop: false },
  blocked_concurrent: { key: 'blocked_concurrent', label: '另一个任务正在运行', detail: '跨进程锁不可用或另一进程正在处理同一批次，本次没有发送任何请求；请等待后刷新，不要重复提交。', tone: 'warning', busy: false, needsReview: false, canStop: false },
  recovered: { key: 'recovered', label: '已恢复 · 待核对', detail: '服务端报告已恢复；请核对实际结果，不要直接重跑本批。', tone: 'warning', busy: false, needsReview: true, canStop: false },
  not_started: { key: 'not_started', label: '未开始', detail: '该操作未真正开始；请核对配置后决定是否重新执行。', tone: 'warning', busy: false, needsReview: true, canStop: false },
  rejected: { key: 'rejected', label: '已拒绝', detail: '操作被服务端拒绝；请按提示处理后重试。', tone: 'warning', busy: false, needsReview: true, canStop: false },
  error: { key: 'error', label: '失败', detail: '任务执行失败。请查看日志定位原因，不要直接重跑整批。', tone: 'danger', busy: false, needsReview: true, canStop: false },
  updating: { key: 'updating', label: '处理中', detail: '正在执行应用更新，请等待结果，不要重复点击。', tone: 'progress', busy: true, needsReview: false, canStop: false },
}

export function operationModeLabel(mode: string): string {
  return MODE_LABELS[mode] || mode || '其他操作'
}

function cleanDetail(value: string | undefined, fallback: string): string {
  const text = (value || '').trim()
  return text || fallback
}

export function legacyStatusFromOperation(operation: OperationInfo | null | undefined): StatusState {
  if (!operation) return 'ready'
  const status = String(operation.status || '').toLowerCase()
  if (operation.active) {
    if (status === 'stopping' || operation.phase === 'stopping') return 'stopping'
    if (operation.mode === 'wps_upload' || operation.mode === 'wps_authorize'
      || operation.mode === 'wps_logout' || operation.mode === 'check_update'
      || operation.mode === 'install_update') return 'updating'
    return 'running'
  }
  const map: Record<string, StatusState> = {
    idle: 'ready', '': 'ready', not_found: 'ready',
    running: 'running', stopping: 'stopping',
    success: 'success', noop: 'noop',
    partial: 'partial', stopped: 'stopped',
    dry_run: 'dry_run', preflight_ok: 'preflight_ok', no_orders: 'no_orders',
    insufficient_balance: 'insufficient_balance', balance_unknown: 'balance_unknown',
    uncertain: 'uncertain', blocked_uncertain: 'blocked_uncertain',
    blocked: 'blocked_uncertain',
    blocked_concurrent: 'blocked_concurrent',
    recovered: 'recovered', not_started: 'not_started',
    rejected: 'rejected',
    failed: 'error', error: 'error',
  }
  return map[status] ?? 'error'
}

/** 权威 operation_status → UI 视图；connection 是本地通道状态，优先显示。 */
export function operationViewFromAuthority(
  operation: OperationInfo | null | undefined,
  connection: ConnectionState,
  recovery: RecoveryState = 'idle',
): OperationView {
  if (connection === 'connecting') return connectionView('connecting')
  if (connection === 'disconnected') {
    const mode = operationModeLabel(operation?.mode || '')
    return {
      key: 'disconnected', label: '连接中断', tone: 'warning',
      detail: `已与服务器断开，恢复后会自动查询 operation_status；${mode !== '空闲' ? `上次操作：${mode}` : ''}。不要重跑。`,
      busy: false, needsReview: true, canStop: false,
      modeLabel: mode, nextAction: '恢复连接后查询权威状态',
      operationId: operation?.operation_id || '', active: false,
      finishedAt: operation?.finished_at ?? null,
    }
  }
  if (recovery === 'checking') return connectionView('checking')
  if (recovery === 'unavailable') return connectionView('recovery_unavailable')
  // 只要服务端给了非 idle 的权威状态就必须如实渲染：不能因为 operation_id 为空
  // 就回退成「就绪」（否则 blocked_concurrent/uncertain 会被伪装成没有任务）。
  if (operation && (operation.operation_id || isMeaningfulStatus(operation.status))) {
    return viewOfOperation(operation)
  }
  return { ...STATUS_FALLBACK.ready, modeLabel: '空闲', nextAction: '', operationId: '', active: false, finishedAt: null }
}

function isMeaningfulStatus(status: string | undefined): boolean {
  const value = String(status || '').toLowerCase()
  return value !== '' && value !== 'idle' && value !== 'not_found'
}

export function operationViewFromStatus(
  status: StatusState,
  workerAlive: boolean,
  connection: ConnectionState,
): OperationView {
  if (connection === 'connecting') return connectionView('connecting')
  if (connection === 'disconnected') return operationViewFromAuthority(null, connection)
  const base = STATUS_FALLBACK[status] ?? STATUS_FALLBACK.ready
  const busy = workerAlive || base.busy
  return {
    ...base,
    busy,
    modeLabel: '任务',
    nextAction: '',
    operationId: '',
    active: busy,
    finishedAt: null,
  }
}

function connectionView(key: 'connecting' | 'checking' | 'recovery_unavailable'): OperationView {
  if (key === 'connecting') {
    return {
      key: 'connecting', label: '连接中', detail: '正在连接运行任务的服务器…',
      tone: 'info', busy: false, needsReview: false, canStop: false,
      modeLabel: '', nextAction: '', operationId: '', active: false, finishedAt: null,
    }
  }
  if (key === 'checking') {
    return {
      key: 'checking', label: '正在核对', detail: '连接已恢复，正在向服务端确认操作状态，请不要重复操作。',
      tone: 'info', busy: true, needsReview: false, canStop: false,
      modeLabel: '', nextAction: '', operationId: '', active: false, finishedAt: null,
    }
  }
  return {
    key: 'disconnected', label: '状态未确认', tone: 'warning',
    detail: '连接恢复但无法取得 operation_status。请查看日志确认服务端状态，不要重跑。',
    busy: false, needsReview: true, canStop: false,
    modeLabel: '', nextAction: '查看日志或稍后重新核对', operationId: '', active: false, finishedAt: null,
  }
}

function resultView(
  modeLabel: string,
  key: OperationView['key'],
  label: string,
  detail: string,
  tone: OperationTone,
  needsReview: boolean,
  nextAction: string,
  reason: string,
  common: Pick<OperationView, 'operationId' | 'finishedAt'>,
  canStop = false,
): OperationView {
  return {
    key, label, detail: cleanDetail(nextAction || reason, detail), tone,
    busy: false, needsReview, canStop, active: false,
    modeLabel, nextAction, ...common,
  }
}

function viewOfOperation(operation: OperationInfo): OperationView {
  const modeLabel = operationModeLabel(operation.mode)
  const nextAction = (operation.next_action || '').trim()
  const reason = (operation.reason || '').trim()
  const status = String(operation.status || '').toLowerCase()
  const common = { operationId: operation.operation_id, finishedAt: operation.finished_at }

  if (operation.active || status === 'running') {
    if (status === 'stopping' || operation.phase === 'stopping') {
      return {
        key: 'stopping', label: '正在停止', tone: 'warning',
        detail: cleanDetail(nextAction, `正在停止${modeLabel}，等待安全收尾。`),
        busy: true, needsReview: false, canStop: false, active: true, modeLabel, nextAction, ...common,
      }
    }
    return {
      key: 'running', label: `${modeLabel}执行中`, tone: 'progress',
      detail: cleanDetail(nextAction, '任务正在服务端执行，请不要重复提交。'),
      busy: true, needsReview: false,
      canStop: operation.mode === 'order' || operation.mode === 'sss',
      active: true, modeLabel, nextAction, ...common,
    }
  }

  switch (status) {
    case 'idle':
      return resultView(modeLabel, 'idle', '就绪', '没有正在执行的任务。', 'neutral', false, nextAction, reason, common)
    case 'success':
      return resultView(modeLabel, 'success', `${modeLabel}已完成`, '任务已正常结束，可查看结果与日志。', 'success', false, nextAction, reason, common)
    case 'noop':
      return resultView(modeLabel, 'noop', `${modeLabel}已完成（无变化）`, '服务端未产生变更。', 'success', false, nextAction, reason, common)
    case 'partial':
      return resultView(modeLabel, 'partial', `${modeLabel}部分完成 · 待核对`, '结果存在未完成或不确定项。请先核对日志，只补处理未完成项，不要整批重跑。', 'warning', true, nextAction, reason, common)
    case 'stopped':
      return resultView(modeLabel, 'stopped', `${modeLabel}已停止`, '操作已停止，可能有未完成项，请核对日志后单独补处理。', 'warning', true, nextAction, reason, common)
    case 'dry_run':
      return resultView(modeLabel, 'dry_run', `${modeLabel}模拟完成（未创建订单）`, '未发送真实请求，不把模拟当正式成功。', 'info', false, nextAction, reason, common)
    case 'preflight_ok':
      return resultView(modeLabel, 'preflight_ok', `${modeLabel}预检完成（未创建订单）`, '只读检查完成，未提交新订单。', 'info', false, nextAction, reason, common)
    case 'no_orders':
      return resultView(modeLabel, 'no_orders', '没有需要处理的订单', '无需操作。', 'info', false, nextAction, reason, common)
    case 'insufficient_balance':
      return resultView(modeLabel, 'insufficient_balance', '余额不足，本批未提交', '任务已安全停止，本批未提交；请先只读核对。', 'warning', true, nextAction, reason, common)
    case 'balance_unknown':
      return resultView(modeLabel, 'balance_unknown', '余额未知，本批未提交', '任务已安全停止，本批未提交；请先只读核对。', 'warning', true, nextAction, reason, common)
    case 'uncertain':
      return resultView(modeLabel, 'uncertain', `${modeLabel}结果不确定 · 待核对`, '无法确认执行结果；请先只读核对云端与日志，不要直接重试或重跑本批。', 'warning', true, nextAction, reason, common)
    case 'blocked':
    case 'blocked_uncertain': {
      // 阻断文案必须始终指向未决记录面板（服务端 next_action 只作为补充说明），
      // 否则用户只看到“先只读核对”却找不到记录与解除入口。
      const panelHint = '请在闪时送结果区「未决记录」面板查看未决记录并只读核对站内订单；未确认前不要重发。'
      const extra = (nextAction || reason).trim()
      return {
        key: 'blocked_uncertain',
        label: `${modeLabel}被阻断 · 待核对`,
        detail: `${panelHint}${extra ? ` 服务端说明：${extra}` : ''}`,
        tone: 'danger', busy: false, needsReview: true, canStop: false, active: false,
        modeLabel, nextAction, ...common,
      }
    }
    case 'blocked_concurrent': {
      // 本次没有发送任何请求，不需要对账；级别是 warning 不是 danger。
      // 统一并发文案必须始终出现（服务端的 next_action 只作为补充说明）。
      const concurrency = '另一个任务正在运行，请等待后刷新。'
      const extra = (nextAction || reason).trim()
      return {
        key: 'blocked_concurrent',
        label: '另一个任务正在运行',
        detail: `${concurrency}${extra ? ` 服务端说明：${extra}` : ''}本次没有发送任何请求，不需要对账。`,
        tone: 'warning', busy: false, needsReview: false, canStop: false, active: false,
        modeLabel, nextAction, ...common,
      }
    }
    case 'recovered':
      return resultView(modeLabel, 'recovered', `${modeLabel}已恢复 · 待核对`, '服务端报告已恢复；请核对实际结果，不要直接重跑本批。', 'warning', true, nextAction, reason, common)
    case 'not_started':
      return resultView(modeLabel, 'not_started', `${modeLabel}未开始`, '该操作未真正开始；请核对配置后决定是否重新执行。', 'warning', true, nextAction, reason, common)
    case 'rejected':
      return resultView(modeLabel, 'rejected', `${modeLabel}已拒绝`, '操作被服务端拒绝；请按提示处理后重试。', 'warning', true, nextAction, reason, common)
    case 'failed':
    case 'error':
    default:
      return resultView(modeLabel, 'error', `${modeLabel}失败`, '操作失败。请查看日志定位原因，不要直接重跑整批。', 'danger', true, nextAction, reason, common)
    case 'not_found':
      return resultView(modeLabel, 'error', '操作不存在', '服务端找不到该 operation_id，可能已被清理；请刷新状态。', 'warning', true, nextAction, reason, common)
  }
}

export const OPERATION_TONES: Record<OperationTone, string> = {
  neutral: 'text-muted-foreground',
  progress: 'text-primary',
  success: 'text-success',
  warning: 'text-warning',
  danger: 'text-destructive',
  info: 'text-primary',
}
