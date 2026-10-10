import assert from 'node:assert/strict'
import test from 'node:test'
import {
  parseSssSubmission,
  pickSssSubmission,
  sssFollowUpAction,
  sssPrimaryLabel,
  sssRunKind,
  sssRunKindFromSummary,
  sssSendGuard,
  sssSubmissionAllConfirmed,
  sssSubmissionHeadline,
  sssSubmissionNote,
  sssSubmissionNotes,
  sssSubmissionPending,
  sssSubmissionRows,
  SSS_RECONCILE_LABEL,
} from './sssSubmission.ts'
import type { OperationInfo } from './bridge.ts'

/**
 * 主样例（现场事实）：目标 99 单，提交前已有 26 站内确认，本轮提交 73 单 / 73 次 POST，
 * 25 次成功响应、48 次技术异常，对账后新增 25 单确认 → 最终确认 51、未确认 48。
 */
const SUBMISSION = {
  target_total: 99,
  preconfirmed: 26,
  submitted: 73,
  attempts: 73,
  success_responses: 25,
  technical_errors: 48,
  explicit_rejections: 0,
  auth_rejections: 0,
  balance_rejections: 0,
  not_sent: 0,
  newly_confirmed: 25,
  confirmed: 51,
  unconfirmed: 48,
  reconciled: true,
}

function operation(overrides: Partial<OperationInfo> = {}): OperationInfo {
  return {
    ok: true, active: false, operation_id: 'op-sss-1', mode: 'sss', status: 'uncertain',
    phase: 'finished', summary: {}, next_action: '', reason: '', started_at: 1, finished_at: 2,
    ...overrides,
  }
}

/** 带 submission 的权威 operation（默认：未确认 48 单）。 */
function withSubmission(submission: unknown = SUBMISSION,
                        overrides: Partial<OperationInfo> = {}): OperationInfo {
  return operation({
    summary: { status: 'unconfirmed', semantics: 'at-least-once+reconciliation', submission },
    ...overrides,
  })
}

type Parsed = NonNullable<ReturnType<typeof parseSssSubmission>>

function parsed(submission: unknown = SUBMISSION): Parsed {
  const result = parseSssSubmission(submission)
  assert.ok(result, '需要 submission')
  return result
}

function rowValue(summary: Parsed, key: string): string {
  const row = sssSubmissionRows(summary).find((item) => item.key === key)
  assert.ok(row, `缺行 ${key}`)
  return row.value
}

test('解析完整计数：逐项保留且自洽', () => {
  const view = parsed()
  assert.deepEqual(
    {
      targetTotal: view.targetTotal, preconfirmed: view.preconfirmed, submitted: view.submitted,
      attempts: view.attempts, successResponses: view.successResponses,
      technicalErrors: view.technicalErrors, explicitRejections: view.explicitRejections,
      authRejections: view.authRejections, balanceRejections: view.balanceRejections,
      notSent: view.notSent, newlyConfirmed: view.newlyConfirmed, confirmed: view.confirmed,
      unconfirmed: view.unconfirmed, reconciled: view.reconciled,
    },
    {
      targetTotal: 99, preconfirmed: 26, submitted: 73, attempts: 73, successResponses: 25,
      technicalErrors: 48, explicitRejections: 0, authRejections: 0, balanceRejections: 0,
      notSent: 0, newlyConfirmed: 25, confirmed: 51, unconfirmed: 48, reconciled: true,
    },
  )
  assert.equal(view.consistent, true)
  assert.deepEqual(view.issues, [])
})

test('没有 submission 字典时返回 null（不渲染统计卡片，也不按 0 计）', () => {
  assert.equal(parseSssSubmission(undefined), null)
  assert.equal(parseSssSubmission(null), null)
  assert.equal(parseSssSubmission('nope'), null)
  assert.equal(parseSssSubmission([]), null)
})

test('计数只接受有限非负整数：布尔/数组/对象/非数字串都不是 0', () => {
  const view = parsed({
    target_total: 99,
    submitted: false, attempts: [], success_responses: {}, technical_errors: 'abc',
    explicit_rejections: -3, not_sent: 1.5, confirmed: null, unconfirmed: undefined,
  })
  for (const field of ['submitted', 'attempts', 'successResponses', 'technicalErrors',
    'explicitRejections', 'notSent'] as const) {
    assert.equal(view[field], null, field)
  }
  assert.equal(view.targetTotal, 99)
  assert.equal(view.consistent, false)
  const issues = view.issues.join('；')
  // 报错用载荷里的原始字段名（便于对照服务端返回）。
  assert.match(issues, /submitted 不是数字/)
  assert.match(issues, /attempts 不是数字/)
  assert.match(issues, /success_responses 不是数字/)
  assert.match(issues, /technical_errors 不是数字/)
  assert.match(issues, /explicit_rejections 为负数/)
  assert.match(issues, /not_sent 不是整数/)
  // 缺失值不算矛盾（不是 0，也不报错）。
  assert.ok(!issues.includes('confirmed'))
  assert.ok(!issues.includes('unconfirmed'))
})

test('数字串按旧接口兼容读取，空串按缺失', () => {
  const view = parsed({ target_total: '99', submitted: ' 73 ', attempts: 73, unconfirmed: '' })
  assert.equal(view.targetTotal, 99)
  assert.equal(view.submitted, 73)
  assert.equal(view.unconfirmed, null)
  assert.equal(view.consistent, true)
})

test('进度类字段永远不是计数：progress 20 不冒充任何结果数字', () => {
  const view = parsed({ progress: 20, percent: 20, progress_percent: '20' })
  assert.equal(view.submitted, null)
  assert.equal(view.attempts, null)
  assert.equal(view.confirmed, null)
  const rows = sssSubmissionRows(view)
  assert.ok(rows.every((row) => !row.value.includes('20')), JSON.stringify(rows))
  assert.equal(sssSubmissionHeadline(view).key, 'unknown')
  assert.match(sssSubmissionHeadline(view).title, /待核对/)
})

test('契约关系矛盾不给成功口径：集合分区、集合增量、POST 次数上限', () => {
  const partition = parsed({ ...SUBMISSION, confirmed: 51, unconfirmed: 48, target_total: 98 })
  assert.equal(partition.consistent, false)
  assert.match(partition.issues.join('；'), /confirmed\+unconfirmed/)

  const increment = parsed({ ...SUBMISSION, preconfirmed: 26, newly_confirmed: 30, confirmed: 51 })
  assert.equal(increment.consistent, false)
  assert.match(increment.issues.join('；'), /preconfirmed\+newlyConfirmed/)

  const overPosts = parsed({ ...SUBMISSION, submitted: 99, attempts: 73 })
  assert.equal(overPosts.consistent, false)
  assert.match(overPosts.issues.join('；'), /submitted 大于 attempts/)

  const overClassified = parsed({ ...SUBMISSION, success_responses: 60, technical_errors: 40 })
  assert.equal(overClassified.consistent, false)
  assert.match(overClassified.issues.join('；'), /响应分类次数之和/)

  const overTarget = parsed({ ...SUBMISSION, submitted: 120, attempts: 120, not_sent: 0 })
  assert.equal(overTarget.consistent, false)
  assert.match(overTarget.issues.join('；'), /submitted 大于 target_total/)

  // 迟到确认允许 newlyConfirmed 超过 submitted，不当作矛盾。
  const lateConfirm = parsed({ ...SUBMISSION, newly_confirmed: 40, confirmed: 66, unconfirmed: 33 })
  assert.equal(lateConfirm.consistent, true)

  for (const view of [partition, increment, overPosts, overClassified, overTarget]) {
    assert.equal(sssSubmissionAllConfirmed(view), false)
    assert.notEqual(sssSubmissionHeadline(view).key, 'confirmed')
  }
})

test('对账未完成：确认类计数按未知（不保留旧 0），响应次数照常显示', () => {
  const stale = parsed({
    ...SUBMISSION, reconciled: false, confirmed: 0, unconfirmed: 0, newly_confirmed: 0,
  })
  assert.equal(stale.consistent, false)
  assert.equal(rowValue(stale, 'unconfirmed'), '待核对/未知')
  assert.equal(rowValue(stale, 'confirmed'), '待核对/未知')
  assert.equal(rowValue(stale, 'newlyConfirmed'), '待核对/未知')
  assert.equal(rowValue(stale, 'successResponses'), '25 次')
  assert.equal(rowValue(stale, 'technicalErrors'), '48 次')

  const nulled = parsed({
    ...SUBMISSION, reconciled: false, confirmed: null, unconfirmed: null, newly_confirmed: null,
  })
  assert.equal(sssSubmissionHeadline(nulled).key, 'pending')
  assert.notEqual(sssSubmissionHeadline(nulled).tone, 'success')
  assert.equal(sssSubmissionPending(nulled), true)
})

test('全部站内确认要求真正对账完成、目标与确认计数已知相等', () => {
  const done = parsed({
    target_total: 26, preconfirmed: 26, submitted: 0, attempts: 0, success_responses: 0,
    technical_errors: 0, explicit_rejections: 0, auth_rejections: 0, balance_rejections: 0,
    not_sent: 0, newly_confirmed: 0, confirmed: 26, unconfirmed: 0, reconciled: true,
  })
  assert.equal(sssSubmissionAllConfirmed(done), true)
  assert.equal(sssSubmissionPending(done), false)
  const headline = sssSubmissionHeadline(done)
  assert.equal(headline.key, 'confirmed')
  assert.equal(headline.tone, 'success')
  assert.match(headline.title, /无需补单/)

  // reconciled 未知 / 缺失 / 目标未知 / 确认数不足 / 计数矛盾 → 一律不算“全部确认”。
  for (const view of [
    parsed({ ...done, reconciled: null }),
    parsed({ ...done, reconciled: undefined }),
    parsed({ confirmed: 26, unconfirmed: 0, reconciled: true }),
    parsed({ ...done, confirmed: 25, unconfirmed: 1 }),
    parsed({ ...done, confirmed: 27 }),
  ]) {
    assert.equal(sssSubmissionAllConfirmed(view), false, JSON.stringify(view))
    assert.notEqual(sssSubmissionHeadline(view).key, 'confirmed')
  }
})

test('行文案：提交/尝试/成功/异常/拒绝/未发送/新增确认/未确认逐项对应', () => {
  const view = parsed()
  assert.deepEqual(sssSubmissionRows(view).map((row) => [row.label, row.value]), [
    ['提交前已有站内确认', '26 单'],
    ['本轮实际提交（不同任务）', '73 单'],
    ['本轮 POST 调用次数（含重登/人工重试）', '73 次'],
    ['响应成功', '25 次'],
    ['技术异常 · 结果未知（未自动重发）', '48 次'],
    ['明确拒绝', '0 次'],
    ['未发送（始终没有 POST）', '0 单'],
    ['本轮新增站内确认（对账差异）', '25 单'],
    ['最终站内确认 / 目标', '51 / 99 单'],
    ['未确认', '48 单'],
  ])
  assert.equal(sssSubmissionHeadline(view).key, 'unconfirmed')
  assert.match(sssSubmissionHeadline(view).title, /仍有 48 项未确认/)
})

test('提示：成功响应是聚合次数，不能判断其中多少尚未可见；补单不是只读操作', () => {
  const view = parsed()
  const notes = sssSubmissionNotes(view).join('\n')
  assert.match(notes, /共收到 25 次成功响应/)
  assert.match(notes, /无法从这个次数判断\s*其中多少只是尚未显示在站内列表/)
  assert.match(notes, /优先等待并只读复查/)
  assert.match(notes, /技术异常（48 次）/)
  assert.match(notes, /不代表订单确定创建失败/)
  assert.match(notes, /「核对并补单」不是只读操作：先做站内对账，仍缺失的订单会真实提交/)
  assert.match(notes, /不表示本轮新建了这么多单/)
  assert.match(notes, /平台列表有延迟时仍可能出现重复/)
  assert.ok(!notes.includes('可再运行一次补单）；'), notes)

  const note = sssSubmissionNote(view)
  assert.match(note, /共收到 25 次成功响应/)
  assert.match(note, /优先等待并只读复查，不要因为看不到就自动重发/)
  assert.ok(!note.includes('已完成'))
})

test('模拟/预检/余额闸门：只说明未提交，不提示补单，缺确认仍未知', () => {
  const dry = parsed({ ...SUBMISSION, submitted: 0, attempts: 0, success_responses: 0, technical_errors: 0, reconciled: false })
  const headline = sssSubmissionHeadline(dry, { kind: 'dry_run' })
  assert.equal(headline.key, 'dry-run')
  assert.match(headline.title, /未发送任何下单请求/)
  assert.notEqual(headline.tone, 'success')
  const notes = sssSubmissionNotes(dry, { kind: 'dry_run' }).join('\n')
  assert.match(notes, /没有提交任何下单请求/)
  assert.ok(!notes.includes('核对并补单'))

  assert.equal(sssSubmissionHeadline(dry, { kind: 'preflight' }).key, 'preflight')
  assert.match(sssSubmissionHeadline(dry, { kind: 'preflight' }).title, /仅预检/)
  const guarded = sssSubmissionHeadline(dry, { kind: 'guarded' })
  assert.equal(guarded.key, 'guarded')
  assert.match(guarded.title, /未提交任何 POST/)
  const noOrders = sssSubmissionHeadline(dry, { kind: 'no_orders' })
  assert.equal(noOrders.key, 'no-orders')
  assert.match(noOrders.title, /未提交订单/)
  for (const view of [headline, sssSubmissionHeadline(dry, { kind: 'preflight' }), guarded, noOrders]) {
    assert.doesNotMatch(view.title, /核对并补单|补单/)
  }

  // 运行中的计数只是暂定。
  const running = sssSubmissionHeadline(parsed(), { running: true })
  assert.equal(running.key, 'running')
  assert.match(running.title, /暂定/)
})

test('运行类型：读 runner 的 summary.status / summary.result，而不是只看归一化状态', () => {
  assert.equal(sssRunKindFromSummary({ status: 'dry_run' }), 'dry_run')
  assert.equal(sssRunKindFromSummary({ status: 'confirmed', result: { dry_run: true } }), 'dry_run')
  assert.equal(sssRunKindFromSummary({ status: 'preflight_uncertain' }), 'preflight')
  assert.equal(sssRunKindFromSummary({}, 'preflight_ok'), 'preflight')
  assert.equal(sssRunKindFromSummary({ result: { semantics: 'preflight-only' } }, 'uncertain'), 'preflight')
  assert.equal(sssRunKindFromSummary({ status: 'insufficient_balance' }), 'guarded')
  assert.equal(sssRunKindFromSummary({ result: { semantics: 'pre-submit-balance-guard' } }, 'failed'), 'guarded')
  assert.equal(sssRunKindFromSummary({ status: 'no_orders' }), 'no_orders')
  assert.equal(sssRunKindFromSummary({ status: 'unconfirmed' }), 'live')
  assert.equal(sssRunKind(null), 'unknown')

  // Bridge 的 operation.status 已归一化：模拟/预检不能被当成真实运行。
  assert.equal(sssRunKind(operation({ status: 'dry_run', summary: {} })), 'dry_run')
  assert.equal(sssRunKind(operation({
    status: 'uncertain', summary: { status: 'preflight_uncertain', result: { semantics: 'preflight-only' } },
  })), 'preflight')
})

test('只取最近一次闪时送运行的统计：顶层权威优先，其它模式不参与', () => {
  const wps = operation({ operation_id: 'op-wps', mode: 'wps_upload', status: 'success', summary: { status: 'success' } })
  const sss = withSubmission(SUBMISSION, { operation_id: 'op-sss' })
  assert.equal(pickSssSubmission({ operation: wps, operations: [wps, sss] }).operationId, 'op-sss')
  assert.equal(pickSssSubmission({ operation: sss, operations: [wps] }).operationId, 'op-sss')
  assert.equal(pickSssSubmission({ operation: wps, operations: [wps] }).operation, null)
  const dayOrders = operation({ operation_id: 'op-day', mode: 'sss_day_orders', status: 'success' })
  assert.equal(pickSssSubmission({ operation: dayOrders, operations: [dayOrders] }).operation, null)
})

test('顶层运行中的 sss 权威优先：不拿上一轮已结束的统计冒充本轮', () => {
  const running = operation({
    operation_id: 'op-new', status: 'running', active: true,
    started_at: '2026-09-20T10:00:00', finished_at: null,
    summary: { status: 'running', progress: 20 },
  })
  const previous = withSubmission(SUBMISSION, {
    operation_id: 'op-old', status: 'partial', started_at: '2026-09-20T09:00:00',
    finished_at: '2026-09-20T09:05:00',
  })
  const picked = pickSssSubmission({ operation: running, operations: [running, previous] })
  assert.equal(picked.operationId, 'op-new')
  assert.equal(picked.running, true)
  assert.equal(picked.submission, null)
})

test('列表不保证新→旧：按 finished_at/started_at 取时间最新的 sss', () => {
  const olderLive = withSubmission(SUBMISSION, {
    operation_id: 'op-live', status: 'uncertain',
    started_at: '2026-09-20T09:00:00', finished_at: '2026-09-20T09:05:00',
  })
  const newerDry = operation({
    operation_id: 'op-dry', status: 'dry_run',
    started_at: '2026-09-20T11:00:00', finished_at: '2026-09-20T11:00:30',
    summary: { status: 'dry_run', submission: { ...SUBMISSION, submitted: 0, attempts: 0, success_responses: 0, technical_errors: 0, reconciled: false } },
  })
  const picked = pickSssSubmission({ operations: [olderLive, newerDry] })
  assert.equal(picked.operationId, 'op-dry')
  assert.equal(picked.kind, 'dry_run')
  assert.equal(sssFollowUpAction({
    operations: [olderLive, newerDry], connection: 'connected', recovery: 'idle',
    operationActive: false, executionMode: 'live',
  }).reason, 'not-real-run')

  // 时间戳缺失时保持传入顺序（不猜）。
  const noTime = withSubmission(SUBMISSION, { operation_id: 'op-first', started_at: null, finished_at: null })
  const noTime2 = withSubmission(SUBMISSION, { operation_id: 'op-second', started_at: null, finished_at: null })
  assert.equal(pickSssSubmission({ operations: [noTime, noTime2] }).operationId, 'op-first')
})

// ---------- 主动作 ----------

function followUp(overrides: Partial<Parameters<typeof sssFollowUpAction>[0]> = {}) {
  return sssFollowUpAction({
    operation: withSubmission(),
    operations: [],
    connection: 'connected',
    recovery: 'idle',
    operationActive: false,
    executionMode: 'live',
    ...overrides,
  })
}

test('正式运行存在未确认项：主动作改为「核对并补单」', () => {
  const view = followUp()
  assert.equal(view.reconcile, true)
  assert.equal(view.reason, 'eligible')
  assert.equal(view.label, SSS_RECONCILE_LABEL)
  assert.equal(view.operationId, 'op-sss-1')
})

test('对账失败/未确认未知/计数矛盾都提供补单，而不是按 0 处理', () => {
  const failed = followUp({
    operation: withSubmission({ ...SUBMISSION, confirmed: null, unconfirmed: null, newly_confirmed: null, reconciled: false }),
  })
  assert.equal(failed.reconcile, true)
  const inconsistent = followUp({
    operation: withSubmission({ ...SUBMISSION, target_total: 98 }),
  })
  assert.equal(inconsistent.reconcile, true)
})

test('已全部站内确认：保持正常开始，不提供补单', () => {
  const view = followUp({
    operation: withSubmission({
      target_total: 26, preconfirmed: 26, submitted: 0, attempts: 0, success_responses: 0,
      technical_errors: 0, explicit_rejections: 0, auth_rejections: 0, balance_rejections: 0,
      not_sent: 0, newly_confirmed: 0, confirmed: 26, unconfirmed: 0, reconciled: true,
    }, { status: 'success' }),
  })
  assert.equal(view.reconcile, false)
  assert.equal(view.reason, 'all-confirmed')
})

test('预检/模拟/余额闸门不提供补单（即使状态是 uncertain）', () => {
  const preflight = followUp({
    operation: operation({
      status: 'uncertain',
      summary: { status: 'preflight_uncertain', result: { semantics: 'preflight-only' }, submission: SUBMISSION },
    }),
  })
  assert.equal(preflight.reconcile, false)
  assert.equal(preflight.reason, 'not-real-run')

  assert.equal(followUp({ operation: operation({ status: 'dry_run', summary: { status: 'dry_run', submission: SUBMISSION } }) }).reconcile, false)
  assert.equal(followUp({ operation: operation({ status: 'balance_unknown', summary: { submission: SUBMISSION } }) }).reconcile, false)
  assert.equal(followUp({ executionMode: 'dry_run' }).reason, 'mode-not-live')
  assert.equal(followUp({ executionMode: 'preflight' }).reason, 'mode-not-live')
})

test('连接/恢复/互斥任务异常时不提供补单', () => {
  assert.equal(followUp({ connection: 'disconnected' }).reason, 'offline')
  assert.equal(followUp({ connection: 'connecting' }).reason, 'offline')
  assert.equal(followUp({ recovery: 'checking' }).reason, 'recovery-pending')
  assert.equal(followUp({ recovery: 'unavailable' }).reason, 'recovery-pending')
  assert.equal(followUp({ operationActive: true }).reason, 'busy')
  for (const view of [followUp({ connection: 'connecting' }), followUp({ operationActive: true })]) {
    assert.equal(view.reconcile, false)
    assert.equal(view.label, '')
  }
})

test('运行中/阻断/其它模式陈旧状态不改按钮', () => {
  assert.equal(followUp({
    operation: operation({ active: true, status: 'running', summary: { submission: SUBMISSION } }),
    operationActive: true,
  }).reconcile, false)
  assert.equal(followUp({ operation: operation({ mode: 'sss_day_orders', status: 'success' }) }).reason, 'no-finished-run')
  assert.equal(followUp({ operation: operation({ status: 'blocked_uncertain' }) }).reason, 'blocked')
  assert.equal(followUp({ operation: operation({ status: 'blocked_concurrent' }) }).reason, 'not-real-run')
  const wps = operation({ operation_id: 'op-wps', mode: 'wps_upload', status: 'success' })
  assert.equal(followUp({ operation: wps, operations: [wps] }).reason, 'no-finished-run')
})

test('旧后端（无 submission）：只在可能已提交但未确认的状态上提供', () => {
  for (const status of ['uncertain', 'partial', 'stopped', 'recovered']) {
    assert.equal(followUp({ operation: operation({ status }) }).reconcile, true, status)
  }
  for (const status of ['success', 'noop', 'no_orders']) {
    assert.equal(followUp({ operation: operation({ status }) }).reconcile, false, status)
  }
})

test('发送闸门：断线/正在核对/已有任务实际阻止提交', () => {
  assert.deepEqual(sssSendGuard({ connection: 'connected', recovery: 'idle', operationActive: false }),
    { blocked: false, reason: '' })
  assert.equal(sssSendGuard({ connection: 'disconnected', recovery: 'idle', operationActive: false }).blocked, true)
  assert.equal(sssSendGuard({ connection: 'connecting', recovery: 'idle', operationActive: false }).blocked, true)
  assert.equal(sssSendGuard({ connection: 'connected', recovery: 'checking', operationActive: false }).blocked, true)
  assert.equal(sssSendGuard({ connection: 'connected', recovery: 'unavailable', operationActive: false }).blocked, true)
  assert.equal(sssSendGuard({ connection: 'connected', recovery: 'idle', operationActive: true }).reason, '已有操作进行中')
})

test('主动作按钮文字：名单预览与闸门优先，补单其次', () => {
  const eligible = followUp()
  const base = {
    operationActive: false, needsDayPreview: false, dayLoading: false,
    executionMode: 'live' as const, followUp: eligible,
  }
  assert.equal(sssPrimaryLabel({ ...base, needsDayPreview: true }), '先读取云端名单')
  assert.equal(sssPrimaryLabel({ ...base, needsDayPreview: true, dayLoading: true }), '正在读取云端名单…')
  assert.equal(sssPrimaryLabel({ ...base, operationActive: true }), '已有操作进行中')
  assert.equal(sssPrimaryLabel({ ...base, sendBlocked: true, sendBlockReason: '连接中断' }), '连接中断，暂不能提交')
  assert.equal(sssPrimaryLabel(base), SSS_RECONCILE_LABEL)
  const normal = followUp({ operation: null })
  assert.equal(sssPrimaryLabel({ ...base, followUp: normal }), '开始正式下单')
  assert.equal(sssPrimaryLabel({ ...base, followUp: normal, executionMode: 'preflight' }), '开始预检')
  assert.equal(sssPrimaryLabel({ ...base, followUp: normal, executionMode: 'dry_run' }), '开始模拟执行')
})
