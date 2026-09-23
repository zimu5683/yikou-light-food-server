import assert from 'node:assert/strict'
import test from 'node:test'
import { legacyStatusFromOperation, operationModeLabel, operationViewFromAuthority, operationViewFromStatus } from './operationStatus.ts'
import type { OperationInfo } from './bridge.ts'

function operation(overrides: Partial<OperationInfo> = {}): OperationInfo {
  return {
    ok: true, active: false, operation_id: 'op-1', mode: 'order', status: 'success',
    phase: '', summary: {}, next_action: '', reason: '', started_at: 1, finished_at: 2,
    ...overrides,
  }
}

test('活动 operation 显示执行中；订单可停止，云上传不可停止', () => {
  const order = operationViewFromAuthority(operation({ active: true, status: 'running', mode: 'order' }), 'connected')
  assert.equal(order.key, 'running')
  assert.equal(order.busy, true)
  assert.equal(order.canStop, true)
  const upload = operationViewFromAuthority(operation({ active: true, status: 'running', mode: 'wps_upload' }), 'connected')
  assert.equal(upload.busy, true)
  assert.equal(upload.canStop, false)
})

test('partial 进入待核对，不显示为整批失败', () => {
  const view = operationViewFromAuthority(operation({ status: 'partial', mode: 'order', next_action: '只补未完成项' }), 'connected')
  assert.equal(view.key, 'partial')
  assert.equal(view.needsReview, true)
  assert.match(view.label, /待核对/)
  assert.equal(view.tone, 'warning')
})

test('断线只提示连接中断，保留上次权威状态且提示不要重跑', () => {
  const view = operationViewFromAuthority(operation({ active: true, status: 'running' }), 'disconnected')
  assert.equal(view.key, 'disconnected')
  assert.equal(view.tone, 'warning')
  assert.match(view.detail, /不要重跑/)
  assert.notEqual(view.key, 'error')
})

test('operation_conflict/rejected 显示为已拒绝与下一步', () => {
  const view = operationViewFromAuthority(operation({ active: false, status: 'rejected', reason: 'operation_conflict' }), 'connected')
  assert.equal(view.key, 'rejected')
  assert.equal(view.needsReview, true)
  assert.match(view.detail, /operation_conflict/)
})

test('旧事件通道的 error 状态仍可显示为失败', () => {
  const view = operationViewFromStatus('error', false, 'connected')
  assert.equal(view.key, 'error')
  assert.equal(view.needsReview, true)
  assert.equal(operationModeLabel('wps_upload'), '云文档上传')
})

test('P0 新增结果：uncertain/partial/failed/blocked 不显示成功', () => {
  const uncertain = operationViewFromAuthority(operation({ status: 'uncertain', active: false }), 'connected')
  assert.equal(uncertain.key, 'uncertain')
  assert.equal(uncertain.needsReview, true)
  assert.notEqual(uncertain.tone, 'success')
  assert.doesNotMatch(uncertain.label, /已完成/)

  const partial = operationViewFromAuthority(operation({ status: 'partial', active: false }), 'connected')
  assert.equal(partial.needsReview, true)
  assert.notEqual(partial.tone, 'success')

  const failed = operationViewFromAuthority(operation({ status: 'failed', active: false }), 'connected')
  assert.equal(failed.key, 'error')
  assert.equal(failed.tone, 'danger')

  const blocked = operationViewFromAuthority(operation({ status: 'blocked_uncertain', active: false }), 'connected')
  assert.equal(blocked.key, 'blocked_uncertain')
  assert.equal(blocked.needsReview, true)
})

test('blocked_uncertain 文案始终指向未决记录面板，且不被服务端 next_action 覆盖', () => {
  const view = operationViewFromAuthority(operation({
    status: 'blocked_uncertain', active: false, mode: 'sss',
    next_action: '先只读核对站内订单与本地记录；未确认前不要重跑或补发',
  }), 'connected')
  assert.equal(view.key, 'blocked_uncertain')
  assert.equal(view.needsReview, true)
  assert.notEqual(view.tone, 'success')
  assert.match(view.detail, /未决记录/)
  assert.match(view.detail, /只读核对站内订单/)
  // 服务端 next_action 只作为补充，不能把面板入口挤掉。
  assert.match(view.detail, /服务端说明/)
})

test('模拟/预检明确未下单；legacyStatusFromOperation 覆盖新状态', () => {
  const dry = operationViewFromAuthority(operation({ status: 'dry_run', active: false }), 'connected')
  assert.equal(dry.key, 'dry_run')
  assert.match(dry.label, /未创建订单/)

  const preflight = operationViewFromAuthority(operation({ status: 'preflight_ok', active: false }), 'connected')
  assert.equal(preflight.key, 'preflight_ok')
  assert.match(preflight.label, /未创建订单/)

  assert.equal(legacyStatusFromOperation(operation({ status: 'uncertain' })), 'uncertain')
  assert.equal(legacyStatusFromOperation(operation({ status: 'failed' })), 'error')
  assert.equal(legacyStatusFromOperation(operation({ status: 'noop' })), 'noop')
  assert.equal(legacyStatusFromOperation(operation({ status: 'dry_run' })), 'dry_run')
  assert.equal(legacyStatusFromOperation(operation({ active: true, status: 'running', mode: 'wps_upload' })), 'updating')
})

test('R6-9：blocked_concurrent 权威视图与 legacy 回退都显示“另一个任务正在运行”', () => {
  const authority = operationViewFromAuthority(
    operation({ status: 'blocked_concurrent', active: false, mode: 'sss', next_action: '等待锁释放后重试' }),
    'connected',
  )
  assert.equal(authority.key, 'blocked_concurrent')
  assert.equal(authority.tone, 'warning')
  assert.equal(authority.needsReview, false)
  assert.match(authority.label, /另一个任务正在运行/)

  const legacy = operationViewFromStatus('blocked_concurrent', false, 'connected')
  assert.equal(legacy.key, 'blocked_concurrent')
  assert.equal(legacy.needsReview, false)
  assert.notEqual(legacy.key, 'idle')
  assert.match(legacy.label, /另一个任务正在运行/)

  assert.equal(legacyStatusFromOperation(operation({ status: 'blocked_concurrent' })), 'blocked_concurrent')
})

test('R6 §9：新增只读入口 mode 有中文名', () => {
  assert.equal(operationModeLabel('wps_preview'), '云文档预览')
  assert.equal(operationModeLabel('wps_check_copies'), '云文档副本核对')
  assert.equal(operationModeLabel('sss_day_orders'), '云端当天名单读取')
  assert.equal(operationModeLabel('wps_recovery_resolve'), '旧任务恢复/退场')
  // 未登记的 mode 仍回退原文，不显示 undefined。
  assert.equal(operationModeLabel('brand_new_mode'), 'brand_new_mode')
})
