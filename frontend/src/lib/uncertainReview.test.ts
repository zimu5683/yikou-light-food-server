import assert from 'node:assert/strict'
import test from 'node:test'
import {
  EMPTY_SSS_REVIEW,
  SSS_CLASSIFICATION_LABELS,
  SSS_DECISION_SPECS,
  SSS_NOTE_MIN_LENGTH,
  SSS_RECORD_STATUS_LABELS,
  SSS_RESOLVE_FAILURE_TEXT,
  SSS_RESOLVE_SENDS_POST,
  absentEvidence,
  buildUncertainResolvePayload,
  maskPhoneFallback,
  resolveFailureText,
  resolveGate,
  reviewStartGate,
  reviewStartView,
  shouldShowUncertainPanel,
  uncertainRecordView,
  uncertainResolveView,
  uncertainReviewView,
  uncertainStateView,
} from './uncertainReview.ts'
import type {
  SssUncertainRecord,
  SssUncertainResolveResult,
  SssUncertainReview,
  SssUncertainState,
} from './bridge.ts'

/** 新鲜且指纹一致的核对快照：station_absent 的前提。 */
const OK_REVIEW = uncertainReviewView({
  available: true,
  checked_at: '2026-09-22T07:41:10',
  age_s: 42.1,
  stale: false,
  journal_fingerprint: 'ab12cd34ef56',
  journal_matches: true,
  wide_window_days: 3,
  counts: { station_missing: 62, station_found_other_day: 0, station_confirmed: 0, scan_failed: 0 },
  classifications: { j1: 'station_missing', j2: 'station_missing' },
})

function gate(overrides: Partial<Parameters<typeof resolveGate>[0]> = {}) {
  return resolveGate({
    isAdmin: true,
    decision: 'station_absent',
    recordIds: ['j1'],
    note: '已人工核对站内订单',
    confirm: 'station_absent',
    review: OK_REVIEW,
    busy: false,
    ...overrides,
  })
}

function record(overrides: Partial<SssUncertainRecord> = {}): SssUncertainRecord {
  return {
    journal_id: 'j1',
    identifier: 'id-1',
    sheet: '午餐',
    batch_id: 'abc123def456',
    delivery_date: '2026-09-22',
    status: 'inflight',
    error: '提交前置记录：POST 即将发出，等待响应/对账',
    created_at: '2026-09-22T07:12:03',
    batch_started_at: 1758505923.4,
    name: '张三',
    phone: '138****0001',
    delivery_time: '2026-09-22 11:00:00',
    door_num: 'A101',
    account: '187****7837',
    reason: '',
    ...overrides,
  }
}

function resolveResult(overrides: Partial<SssUncertainResolveResult> = {}): SssUncertainResolveResult {
  return {
    ok: true,
    status: 'discarded',
    code: '',
    reason: '',
    next_action: '可以重新运行',
    read_only: false,
    cloud_write: false,
    post_sent: false,
    changed: true,
    decision: 'station_absent',
    record_ids: ['j1'],
    affected: 1,
    remaining: 0,
    ...overrides,
  }
}

const FAILURE_CODES = [
  'forbidden', 'invalid_payload', 'invalid_record_ids', 'unknown_record_ids',
  'confirmation_required', 'note_required', 'review_required', 'review_stale',
  'journal_changed', 'station_state_changed', 'operation_conflict',
  'journal_write_failed', 'journal_unreadable',
]

test('未决记录阻断：失败结果永远不显示成功，且阻断保持', () => {
  for (const code of FAILURE_CODES) {
    const view = uncertainResolveView(resolveResult({ ok: false, status: 'rejected', code, changed: false }))
    assert.equal(view.ok, false, code)
    assert.notEqual(view.tone, 'success', code)
    assert.equal(view.blockingRetained, true, code)
    assert.equal(view.postSent, false, code)
    assert.equal(view.cloudWrite, false, code)
    assert.doesNotMatch(view.title, /成功|已完成/, code)
  }
  const nothing = uncertainResolveView(null)
  assert.equal(nothing.ok, false)
  assert.equal(nothing.blockingRetained, true)
})

test('未决记录状态视图：有活跃记录或读取失败时都保持阻断', () => {
  const state: SssUncertainState = {
    ok: true,
    read_only: true,
    contract_version: 1,
    next_action: '先只读核对站内订单；确认后由管理员解除阻断',
    counts: { active: 62, inflight: 62, unresolved: 0, resolved: 0, discarded: 0 },
    records: [record()],
    review: {
      available: true,
      checked_at: '2026-09-22T07:41:10',
      age_s: 42.1,
      stale: false,
      journal_fingerprint: 'ab12cd34ef56',
      journal_matches: true,
      wide_window_days: 3,
      counts: { station_missing: 62 },
      classifications: { j1: 'station_missing' },
    },
  }
  const view = uncertainStateView(state)
  assert.equal(view.ok, true)
  assert.equal(view.blocking, true)
  assert.equal(view.records.length, 1)
  assert.equal(view.records[0].classification, 'station_missing')
  assert.equal(view.records[0].classificationLabel, '站内未查到')
  assert.equal(view.counts.active, 62)

  const failed = uncertainStateView({
    ok: false,
    status: 'failed',
    code: 'journal_unreadable',
    reason: '本地记录不可读',
    next_action: '修复后重试',
    read_only: true,
    contract_version: 1,
  })
  assert.equal(failed.ok, false)
  assert.equal(failed.blocking, true)
  assert.equal(failed.records.length, 0)
})

test('解除门禁：无勾选 / note<4 / confirm 不一致都必须禁用', () => {
  const none = gate({ recordIds: [] })
  assert.equal(none.allowed, false)
  assert.equal(none.code, 'invalid_record_ids')
  assert.equal(gate({ recordIds: ['   '] }).code, 'invalid_record_ids')

  const shortNote = gate({ note: '短' })
  assert.equal(shortNote.allowed, false)
  assert.equal(shortNote.code, 'note_required')
  assert.equal(gate({ note: '啊'.repeat(SSS_NOTE_MIN_LENGTH) }).allowed, true)

  const badConfirm = gate({ confirm: 'Station_absent' })
  assert.equal(badConfirm.allowed, false)
  assert.equal(badConfirm.code, 'confirmation_required')
  assert.match(badConfirm.reason, /station_absent/)

  const notAdmin = gate({ isAdmin: false })
  assert.equal(notAdmin.code, 'forbidden')

  const busy = gate({ busy: true })
  assert.equal(busy.code, 'operation_conflict')
})

test('station_absent 必须 review.available && !stale && journal_matches', () => {
  const unavailable = gate({ review: uncertainReviewView(null) })
  assert.equal(unavailable.allowed, false)
  assert.equal(unavailable.code, 'review_required')
  assert.match(unavailable.reason, /只读核对/)

  const base: SssUncertainReview = {
    available: true,
    checked_at: '2026-09-22T07:41:10',
    age_s: 42.1,
    stale: false,
    journal_fingerprint: 'ab12cd34ef56',
    journal_matches: true,
    wide_window_days: 3,
    counts: {},
    classifications: {},
  }
  const stale = gate({ review: uncertainReviewView({ ...base, stale: true }) })
  assert.equal(stale.allowed, false)
  assert.equal(stale.code, 'review_stale')

  const changed = gate({ review: uncertainReviewView({ ...base, journal_matches: false }) })
  assert.equal(changed.allowed, false)
  assert.equal(changed.code, 'journal_changed')

  const missingSnapshot = gate({ review: uncertainReviewView(EMPTY_SSS_REVIEW) })
  assert.equal(missingSnapshot.allowed, false)
  assert.equal(missingSnapshot.code, 'review_required')

  // 宽窗内命中（落单但送达日被改过）必须改用 station_present，不能解除阻断。
  const otherDay = gate({
    review: uncertainReviewView({ ...base, classifications: { j1: 'station_found_other_day' } }),
  })
  assert.equal(otherDay.allowed, false)
  assert.equal(otherDay.code, 'station_state_changed')
  assert.match(otherDay.reason, /已在站内找到/)

  // 所选记录没有被核对覆盖（或不是「站内未查到」）也不能解除。
  const uncovered = gate({
    review: uncertainReviewView({ ...base, classifications: { j1: 'scan_failed' } }),
  })
  assert.equal(uncovered.allowed, false)
  assert.equal(uncovered.code, 'review_required')

  assert.equal(gate({ review: OK_REVIEW }).allowed, true)
  assert.equal(absentEvidence(['j1', 'j2'], OK_REVIEW).allowed, true)
  assert.equal(absentEvidence([], OK_REVIEW).allowed, true)
})

test('station_present / keep 不需要核对快照', () => {
  const noSnapshot = uncertainReviewView(null)
  assert.equal(noSnapshot.usableForAbsent, false)
  assert.equal(SSS_DECISION_SPECS.station_present.requiresFreshReview, false)
  assert.equal(SSS_DECISION_SPECS.keep.requiresFreshReview, false)
  assert.equal(SSS_DECISION_SPECS.station_absent.requiresFreshReview, true)

  const present = gate({ decision: 'station_present', confirm: 'station_present', review: noSnapshot })
  assert.equal(present.allowed, true)
  const keep = gate({ decision: 'keep', confirm: 'keep', review: noSnapshot })
  assert.equal(keep.allowed, true)
})

test('契约里的每个 code 都有中文提示，且不出现成功/捷径字样', () => {
  assert.deepEqual(Object.keys(SSS_RESOLVE_FAILURE_TEXT).sort(), [...FAILURE_CODES].sort())
  for (const code of FAILURE_CODES) {
    const text = SSS_RESOLVE_FAILURE_TEXT[code]
    assert.ok(text.title.length > 0, code)
    assert.ok(text.detail.length > 0, code)
    assert.ok(text.nextStep.length > 0, code)
    const joined = text.title + text.detail + text.nextStep
    assert.doesNotMatch(joined, /成功|已完成/, code)
    assert.doesNotMatch(joined, /删除记录文件，然后|直接重传|强制清除/, code)
    const viaHelper = resolveFailureText(code)
    assert.deepEqual(viaHelper, text, code)
  }
  // 未知 code 走保守兜底：保持阻断，绝不声称成功。
  const unknown = resolveFailureText('brand_new_code', { reason: '服务端新原因', next_action: '人工核对' })
  assert.equal(unknown.tone, 'danger')
  assert.match(unknown.title, /阻断保持/)
  assert.match(unknown.detail, /服务端新原因/)
  assert.equal(resolveFailureText('brand_new_code').detail.includes('没有改任何文件'), true)
})

test('决策文案：解除/确认/保留三种语义互不混淆', () => {
  assert.equal(SSS_RESOLVE_SENDS_POST, false)
  assert.equal(SSS_DECISION_SPECS.station_absent.blockingRetained, false)
  assert.match(SSS_DECISION_SPECS.station_absent.label, /解除阻断/)
  assert.match(SSS_DECISION_SPECS.station_absent.effect, /不发任何 POST/)
  assert.match(SSS_DECISION_SPECS.station_absent.risk, /阻断/)
  assert.match(SSS_DECISION_SPECS.station_present.label, /标记为已确认/)
  assert.match(SSS_DECISION_SPECS.station_present.risk, /绝不能重发/)
  assert.equal(SSS_DECISION_SPECS.keep.blockingRetained, true)
  assert.match(SSS_DECISION_SPECS.keep.label, /保持阻断/)
})

test('手机号掩码兜底：后端已掩码时不二次掩码，漏掩时补上', () => {
  assert.equal(maskPhoneFallback('13800000001'), '138****0001')
  assert.equal(maskPhoneFallback('138****0001'), '138****0001')
  assert.equal(maskPhoneFallback(''), '')
  assert.equal(maskPhoneFallback('1234'), '****')
  assert.equal(maskPhoneFallback('123456789012'), '12******9012')
})

test('记录行视图：字段齐备、状态与分类有中文标签', () => {
  const row = uncertainRecordView(record(), { j1: 'station_missing' })
  assert.equal(row.journalId, 'j1')
  assert.equal(row.name, '张三')
  assert.equal(row.phone, '138****0001')
  assert.equal(row.deliveryTime, '2026-09-22 11:00:00')
  assert.equal(row.sheet, '午餐')
  assert.equal(row.statusLabel, SSS_RECORD_STATUS_LABELS.inflight)
  assert.equal(row.error.length > 0, true)
  assert.equal(row.createdAt, '2026-09-22T07:12:03')
  assert.equal(row.classificationLabel, SSS_CLASSIFICATION_LABELS.station_missing)
  assert.equal(row.active, true)

  const unmasked = uncertainRecordView(record({ phone: '13800000001' }))
  assert.equal(unmasked.phone, '138****0001')
  const unknown = uncertainRecordView(record({ status: 'weird' }))
  assert.match(unknown.statusLabel, /未知状态/)
  assert.equal(uncertainRecordView(null).active, true)
  assert.equal(uncertainRecordView(record({ status: 'resolved' })).active, false)
})

test('请求体只包含契约字段，confirm 与 decision 逐字一致', () => {
  const built = buildUncertainResolvePayload({
    isAdmin: true,
    decision: 'station_absent',
    recordIds: ['j1', 'j2'],
    note: '  已人工核对站内订单  ',
    confirm: 'station_absent',
    review: OK_REVIEW,
    busy: false,
  })
  assert.equal(built.ok, true)
  if (built.ok) {
    assert.deepEqual(Object.keys(built.payload).sort(), ['confirm', 'decision', 'note', 'record_ids'])
    assert.equal(built.payload.confirm, built.payload.decision)
    assert.equal(built.payload.note, '已人工核对站内订单')
    assert.deepEqual(built.payload.record_ids, ['j1', 'j2'])
  }
  const rejected = buildUncertainResolvePayload({
    isAdmin: true,
    decision: 'station_absent',
    recordIds: [],
    note: '已人工核对站内订单',
    confirm: 'station_absent',
    review: OK_REVIEW,
  })
  assert.equal(rejected.ok, false)
  if (!rejected.ok) assert.equal(rejected.code, 'invalid_record_ids')
})

test('解除结果视图：remaining>0 或 keep 都不得声称阻断已解除', () => {
  const full = uncertainResolveView(resolveResult())
  assert.equal(full.ok, true)
  assert.equal(full.tone, 'success')
  assert.equal(full.blockingRetained, false)
  assert.equal(full.postSent, false)
  assert.equal(full.cloudWrite, false)
  assert.match(full.detail, /没有发送任何 POST/)
  assert.match(full.title, /解除阻断/)

  const partial = uncertainResolveView(resolveResult({ affected: 30, remaining: 32 }))
  assert.equal(partial.tone, 'warning')
  assert.equal(partial.blockingRetained, true)
  assert.match(partial.title, /阻断保持/)

  // 后端 keep 是纯 no-op：changed=false、affected/remaining=0、不写审计。
  const kept = uncertainResolveView(resolveResult({
    status: 'kept', affected: 0, remaining: 0, changed: false, decision: 'keep', record_ids: [], audit: {},
  }))
  assert.equal(kept.tone, 'warning')
  assert.equal(kept.blockingRetained, true)
  assert.match(kept.title, /保持阻断/)
  assert.match(kept.detail, /没有改动任何记录/)

  const resolved = uncertainResolveView(resolveResult({ status: 'resolved', decision: 'station_present' }))
  assert.equal(resolved.tone, 'success')
  assert.equal(resolved.blockingRetained, false)

  const idempotent = uncertainResolveView(resolveResult({ status: 'discarded', changed: false }))
  assert.equal(idempotent.ok, true)
  assert.equal(idempotent.tone, 'warning')
  assert.equal(idempotent.blockingRetained, true)
  assert.match(idempotent.title, /没有改变任何记录/)
})

test('只读核对启动：密码为空或非管理员时禁用，字段错误有中文提示', () => {
  const noPassword = reviewStartGate({ isAdmin: true, password: '' })
  assert.equal(noPassword.allowed, false)
  assert.match(noPassword.reason, /密码/)
  const notAdmin = reviewStartGate({ isAdmin: false, password: 'secret' })
  assert.equal(notAdmin.allowed, false)
  const running = reviewStartGate({ isAdmin: true, password: 'secret', operationActive: true })
  assert.equal(running.allowed, false)
  assert.match(running.reason, /等待/)
  assert.equal(reviewStartGate({ isAdmin: true, password: 'secret' }).allowed, true)

  const rejected = reviewStartView({
    ok: false,
    status: 'rejected',
    reason: 'validation_failed',
    next_action: '修正表单后重试',
    fields: { password: { message: '请输入登录密码' } },
  })
  assert.equal(rejected.ok, false)
  assert.equal(rejected.tone, 'warning')
  assert.deepEqual(rejected.fieldMessages, ['请输入登录密码'])

  const conflict = reviewStartView({
    ok: false, status: 'rejected', reason: 'operation_conflict', next_action: '等待当前操作结束后重试',
  })
  assert.match(conflict.message, /等待/)

  const started = reviewStartView({
    ok: true, status: 'running', reason: '', next_action: '...',
    summary: { message: '已启动只读核对' }, operation_id: 'sss-review-0123456789abcdef',
  })
  assert.equal(started.ok, true)
  assert.equal(started.operationId, 'sss-review-0123456789abcdef')
  assert.equal(started.message, '已启动只读核对')
})

test('面板挂载判定：只在闪时送阻断/存在未决记录时出现，且不发请求', () => {
  assert.equal(shouldShowUncertainPanel({ key: 'blocked_uncertain' }, { mode: 'sss' }), true)
  assert.equal(shouldShowUncertainPanel({ key: 'blocked_uncertain' }, { mode: 'sss_review' }), true)
  assert.equal(shouldShowUncertainPanel({ key: 'blocked_uncertain' }, null), true)
  // 其它模式即使状态叫 blocked_uncertain 也不在闪时送页签显示。
  assert.equal(shouldShowUncertainPanel({ key: 'blocked_uncertain' }, { mode: 'order' }), false)

  const reviewRun = {
    mode: 'sss_review',
    status: 'running',
    summary: { result: { review: { counts: { active: 62 } } } },
  }
  assert.equal(shouldShowUncertainPanel({ key: 'running' }, reviewRun), true)

  // operation_status 会把整份 payload 再包一层：summary.result.summary.result.review。
  const nestedReviewRun = {
    mode: 'sss_review',
    status: 'running',
    summary: {
      result: {
        summary: {
          result: { review: { counts: { active: 62 }, total: 62 } },
        },
      },
    },
  }
  assert.equal(shouldShowUncertainPanel({ key: 'running' }, nestedReviewRun), true)

  const blockedHistory = {
    mode: 'sss', status: 'blocked_uncertain',
    summary: { uncertain_count: 62, result: { uncertain_records: 62 } },
  }
  assert.equal(shouldShowUncertainPanel({ key: 'running' }, { mode: 'sss_review', status: 'running' }, [blockedHistory]), true)
  assert.equal(shouldShowUncertainPanel({ key: 'success' }, { mode: 'sss', status: 'success', summary: {} }), false)
  assert.equal(shouldShowUncertainPanel({ key: 'ready' }, null), false)
  assert.equal(shouldShowUncertainPanel({ key: 'success' }, { mode: 'sss', status: 'success', summary: { uncertain_count: 0 } }), false)
})
