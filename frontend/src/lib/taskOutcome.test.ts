import assert from 'node:assert/strict'
import test from 'node:test'
import { taskOutcomeView } from './taskOutcome.ts'

test('uncertain / partial / failed / blocked 都不显示成功', () => {
  for (const payload of [
    { status: 'uncertain', ok: false, success: false, uncertain: true },
    { status: 'partial', ok: false, success: false, partial: true },
    { status: 'error', ok: false, success: false, needs_review: true },
    { status: 'failed', ok: false, success: false },
    { status: 'blocked_uncertain', ok: false, success: false, blocked: true },
  ]) {
    const view = taskOutcomeView({ message: '服务端结果', ...payload } as never)
    assert.equal(view.isSuccess, false, JSON.stringify(payload))
    assert.equal(view.toast === 'success', false)
    assert.equal(view.needsReview, true)
  }
})

test('只有明确 ok=true success=true 且无风险标记才算成功', () => {
  const success = taskOutcomeView({
    status: 'success', result_status: 'success', ok: true, success: true,
    real_order: true, stopped: false, partial: false, uncertain: false, blocked: false,
    message: '闪时送下单完成：已创建 3 单',
  })
  assert.equal(success.isSuccess, true)
  assert.equal(success.toast, 'success')

  const dirty = taskOutcomeView({
    status: 'success', result_status: 'success', ok: true, success: true,
    uncertain: true, message: '口径不清的完成',
  })
  assert.equal(dirty.isSuccess, false)
  assert.match(dirty.title, /不确定|待核对/)
})

test('模拟/预检/无单/余额不足有明确非成功的文案', () => {
  assert.equal(taskOutcomeView({ status: 'dry_run', real_order: false }).isSuccess, false)
  assert.equal(taskOutcomeView({ status: 'dry_run', real_order: false }).title, '模拟执行完成（未创建订单）')
  assert.equal(taskOutcomeView({ status: 'preflight_ok', real_order: false }).title, '预检完成（未创建订单）')
  assert.equal(taskOutcomeView({ status: 'no_orders' }).toast, 'info')
  assert.match(taskOutcomeView({ status: 'insufficient_balance' }).title, /余额不足/)
  assert.equal(taskOutcomeView({ status: 'insufficient_balance' }).isSuccess, false)
})

test('结果不确定时 next_action 会带进提示', () => {
  const view = taskOutcomeView({
    status: 'uncertain', ok: false, success: false, uncertain: true,
    message: '站内对账失败', next_action: '可再运行一次补单（已存在的订单不会重复提交）',
  })
  assert.equal(view.isSuccess, false)
  assert.equal(view.needsReview, true)
  assert.match(view.message, /可再运行一次补单/)
})

test('R6-9：blocked_concurrent 不落入阻断/失败/对账分支，也不需要核对', () => {
  // 后端 task:error 的真实形状：blocked=true 但本次未发送任何 POST。
  const view = taskOutcomeView({
    status: 'blocked_concurrent',
    result_status: 'blocked_concurrent',
    ok: false, success: false, real_order: false,
    stopped: true, partial: false, blocked: true, uncertain: false, needs_review: false,
    message: '另一个任务正在运行，请等待后刷新（本次未发送任何下单请求）',
    next_action: '另一进程正在处理同一批次或跨进程锁不可用；未发送任何 POST，请等待锁释放后重试',
  })
  assert.equal(view.level, 'warning')
  assert.equal(view.toast, 'warning')
  assert.equal(view.title, '另一个任务正在运行')
  assert.equal(view.needsReview, false)
  assert.equal(view.isSuccess, false)
  assert.doesNotMatch(view.title, /阻断|失败|不确定/)
  assert.match(view.message, /请等待后刷新/)
})

test('R6-9：blocked_concurrent 优先于 payload.blocked，不被误判成“任务被阻断”', () => {
  const view = taskOutcomeView({ status: 'blocked_concurrent', blocked: true, ok: false })
  assert.equal(view.title, '另一个任务正在运行')
  assert.equal(view.level, 'warning')
})

test('F5：task:error 缺少 result_status 时也不能把非失败状态硬编码成失败', () => {
  // useApp 的回退规则：result_status || status || 'error'。
  const pick = (payload: { result_status?: string; status?: string }) =>
    String(payload.result_status || payload.status || 'error')
  assert.equal(pick({ status: 'blocked_concurrent' }), 'blocked_concurrent')
  assert.equal(pick({ result_status: 'blocked_concurrent' }), 'blocked_concurrent')
  assert.equal(pick({ result_status: 'blocked_concurrent', status: 'error' }), 'blocked_concurrent')
  assert.equal(pick({ status: 'uncertain' }), 'uncertain')
  // 两者都缺失时才回退 error。
  assert.equal(pick({}), 'error')

  // 只有 status（无 result_status）的 blocked_concurrent 仍然不进失败分支。
  const onlyStatus = taskOutcomeView({ status: pick({ status: 'blocked_concurrent' }), blocked: true, ok: false })
  assert.equal(onlyStatus.level, 'warning')
  assert.equal(onlyStatus.needsReview, false)
  assert.equal(onlyStatus.title, '另一个任务正在运行')

  // 真实缺状态时才显示任务失败。
  const noStatus = taskOutcomeView({ status: pick({}), ok: false })
  assert.equal(noStatus.title, '任务失败')
})

test('闪时送本轮统计：成功响应是聚合次数；技术异常不叫确定失败；都不自动重发', () => {
  const summary = {
    submission: {
      target_total: 99, preconfirmed: 26, submitted: 73, attempts: 73,
      success_responses: 25, technical_errors: 48, explicit_rejections: 0,
      auth_rejections: 0, balance_rejections: 0, not_sent: 0,
      newly_confirmed: 25, confirmed: 51, unconfirmed: 48, reconciled: true,
    },
  }
  const view = taskOutcomeView({
    status: 'uncertain', result_status: 'uncertain', ok: false, success: false,
    uncertain: true, needs_review: true,
    message: '闪时送任务未确认：确认 51/99',
    summary,
  } as never)
  assert.equal(view.isSuccess, false)
  assert.equal(view.needsReview, true)
  assert.match(view.message, /共收到 25 次成功响应/)
  assert.match(view.message, /无法判断其中多少只是/)
  assert.match(view.message, /不要因为看不到就自动重发/)
  assert.match(view.message, /技术异常/)
  assert.ok(!view.message.includes('已完成'))
  assert.ok(!view.message.includes('可能暂未显示'))

  // ok=true/success=true 但站内仍有未确认：不能弹绿色成功。
  const dirtySuccess = taskOutcomeView({
    status: 'success', result_status: 'success', ok: true, success: true, real_order: true,
    message: '闪时送下单完成：确认 51/99',
    summary,
  } as never)
  assert.equal(dirtySuccess.isSuccess, false)
  assert.equal(dirtySuccess.toast, 'warning')
  assert.match(dirtySuccess.title, /未确认|待核对/)
  assert.ok(!dirtySuccess.message.includes('任务已完成'))

  // 全部对账确认（没有未确认项）时不追加任何吓人的提示，绿色成功保留。
  const clean = taskOutcomeView({
    status: 'success', result_status: 'success', ok: true, success: true, real_order: true,
    message: '闪时送下单完成：确认 26/26',
    summary: {
      submission: {
        target_total: 26, preconfirmed: 26, submitted: 0, attempts: 0,
        success_responses: 0, technical_errors: 0, not_sent: 0,
        newly_confirmed: 0, confirmed: 26, unconfirmed: 0, reconciled: true,
      },
    },
  } as never)
  assert.equal(clean.isSuccess, true)
  assert.equal(clean.toast, 'success')
  assert.ok(!clean.message.includes('技术异常'))
  assert.ok(!clean.message.includes('成功响应'))

  // 模拟执行即使带 submission（未确认=全部）也不是“未确认下单”。
  const dry = taskOutcomeView({
    status: 'dry_run', result_status: 'dry_run', ok: false, success: false, real_order: false,
    message: '干跑完成（未真实下单）',
    summary: {
      status: 'dry_run', result: { dry_run: true },
      submission: { target_total: 26, submitted: 0, attempts: 0, unconfirmed: 26, reconciled: false },
    },
  } as never)
  assert.equal(dry.title, '模拟执行完成（未创建订单）')
  assert.equal(dry.isSuccess, false)

  // 没有 submission 的旧后端结果：文案完全不变。
  const legacy = taskOutcomeView({ status: 'uncertain', uncertain: true, message: '站内对账失败' } as never)
  assert.equal(legacy.message, '站内对账失败')
})
