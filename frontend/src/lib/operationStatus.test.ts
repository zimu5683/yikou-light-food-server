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

test('blocked_uncertain 固定提示按日志处理，且不被服务端 next_action 顶掉', () => {
  const view = operationViewFromAuthority(operation({
    status: 'blocked_uncertain', active: false, mode: 'sss',
    next_action: '示例：服务端补充说明',
  }), 'connected')
  assert.equal(view.key, 'blocked_uncertain')
  assert.equal(view.needsReview, true)
  assert.notEqual(view.tone, 'success')
  assert.match(view.detail, /按运行日志/)
  assert.match(view.detail, /未确认前不要重发/)
  // 服务端 next_action 只作为补充，不能把固定提示挤掉。
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

test('闪时送 success 但仍有未确认项：不冒充最终完成（进度/成功都不是最终）', () => {
  const view = operationViewFromAuthority(operation({
    status: 'success', mode: 'sss', active: false,
    summary: {
      submission: {
        target_total: 99, preconfirmed: 26, submitted: 20, attempts: 20,
        success_responses: 16, technical_errors: 4, explicit_rejections: 0,
        auth_rejections: 0, balance_rejections: 0, not_sent: 0,
        newly_confirmed: 16, confirmed: 42, unconfirmed: 57, reconciled: true,
        progress: 20,
      },
    },
  }), 'connected')
  assert.equal(view.key, 'success')
  assert.match(view.label, /已完成/)
  assert.match(view.label, /未确认/)
  assert.equal(view.tone, 'warning')
  assert.equal(view.needsReview, true)
  // 不能把进度 20 说成最终结果。
  assert.doesNotMatch(view.label, /20/)

  // 未确认数未知（对账失败）同样不冒充最终完成。
  const pending = operationViewFromAuthority(operation({
    status: 'success', mode: 'sss', active: false,
    summary: { status: 'failed', submission: { confirmed: null, unconfirmed: null, reconciled: false } },
  }), 'connected')
  assert.equal(pending.tone, 'warning')
  assert.match(pending.label, /待核对/)

  // 计数矛盾（集合分区对不上）也不能给成功口径。
  const inconsistent = operationViewFromAuthority(operation({
    status: 'success', mode: 'sss', active: false,
    summary: { status: 'success', submission: { target_total: 98, confirmed: 51, unconfirmed: 48, reconciled: true } },
  }), 'connected')
  assert.equal(inconsistent.tone, 'warning')
  assert.match(inconsistent.label, /待核对/)

  // 模拟/预检不是“未确认下单”，不套用未确认文案。
  const dry = operationViewFromAuthority(operation({
    status: 'dry_run', mode: 'sss', active: false,
    summary: { status: 'dry_run', result: { dry_run: true }, submission: { target_total: 26, unconfirmed: 26 } },
  }), 'connected')
  assert.equal(dry.key, 'dry_run')
  assert.match(dry.label, /未创建订单/)

  // 全部站内确认：保持正常的成功口径。
  const done = operationViewFromAuthority(operation({
    status: 'success', mode: 'sss', active: false,
    summary: {
      submission: {
        target_total: 26, preconfirmed: 26, submitted: 0, attempts: 0,
        success_responses: 0, technical_errors: 0, not_sent: 0,
        newly_confirmed: 0, confirmed: 26, unconfirmed: 0, reconciled: true,
      },
    },
  }), 'connected')
  assert.equal(done.tone, 'success')
  assert.equal(done.needsReview, false)

  // 其它模式（无 submission）不受影响。
  const order = operationViewFromAuthority(operation({ status: 'success', mode: 'order' }), 'connected')
  assert.equal(order.tone, 'success')
  assert.equal(order.label, '订单处理已完成')
})
