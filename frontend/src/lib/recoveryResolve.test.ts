import assert from 'node:assert/strict'
import test from 'node:test'
import {
  DECISION_SPECS,
  OPERATION_ID_PATTERN,
  RECOVERY_AUTO_RESUME_ALLOWED,
  RETIRED_GUARDED_CODE,
  ResolveGate,
  buildResolvePayload,
  decisionsFor,
  recoveryOperationView,
  recoveryResolveView,
  retiredGuardedCount,
} from './recoveryResolve.ts'
import type { WpsRecoveryOperation, WpsRecoveryResolveResult } from './bridge.ts'

const OP_ID = 'wps-0123456789abcdef'

function operation(overrides: Partial<WpsRecoveryOperation> = {}): WpsRecoveryOperation {
  return {
    operation_id: OP_ID,
    operation_ref: 'wps-op:abc123def456',
    status: 'uncertain',
    pending: true,
    cloud_checked: true,
    created_at: '2026-09-20T09:00:00',
    updated_at: '2026-09-20T09:01:00',
    target_date: '2026-09-20',
    target_refs: ['wps-target:abc123def456'],
    sheet_count: 1,
    error_code: 'wps_recovery_uncertain',
    allowed_next_actions: ['manual_reconcile'],
    manual_required: true,
    sheets: [],
    ...overrides,
  }
}

test('W3：操作标识必须是内部格式，非法格式本地就拦住', () => {
  assert.equal(OPERATION_ID_PATTERN.test(OP_ID), true)
  assert.equal(OPERATION_ID_PATTERN.test('wps-XYZ'), false)
  const built = buildResolvePayload({
    operationId: 'wps-XYZ', decision: 'keep', confirm: 'keep', note: '人工核对说明', structureChecked: false,
  })
  assert.equal(built.ok, false)
  if (!built.ok) assert.equal(built.code, 'invalid_operation_id')
})

test('W3：逐字确认必须与 decision 完全相同', () => {
  const built = buildResolvePayload({
    operationId: OP_ID, decision: 'keep', confirm: 'Keep', note: '人工核对说明', structureChecked: false,
  })
  assert.equal(built.ok, false)
  if (!built.ok) assert.equal(built.code, 'confirmation_required')
})

test('W3：备注至少 4 字符', () => {
  const built = buildResolvePayload({
    operationId: OP_ID, decision: 'keep', confirm: 'keep', note: '短', structureChecked: false,
  })
  assert.equal(built.ok, false)
  if (!built.ok) assert.equal(built.code, 'note_required')
})

test('W3：retire_guarded 必须确认已人工核对云端表结构', () => {
  const missing = buildResolvePayload({
    operationId: OP_ID, decision: 'retire_guarded', confirm: 'retire_guarded',
    note: '人工核对后仍无法判定', structureChecked: false,
  })
  assert.equal(missing.ok, false)
  if (!missing.ok) assert.equal(missing.code, 'structure_confirmation_required')
  const good = buildResolvePayload({
    operationId: OP_ID, decision: 'retire_guarded', confirm: 'retire_guarded',
    note: '人工核对后仍无法判定', structureChecked: true,
  })
  assert.equal(good.ok, true)
  if (good.ok) {
    assert.deepEqual(good.payload, {
      operation_id: OP_ID,
      decision: 'retire_guarded',
      confirm: 'retire_guarded',
      note: '人工核对后仍无法判定',
      confirm_structure_checked: true,
    })
  }
})

test('W3：请求体永远只有契约允许的字段，不存在删除账本/直接重传', () => {
  const built = buildResolvePayload({
    operationId: OP_ID, decision: 'cloud_untouched', confirm: 'cloud_untouched',
    note: '云端未发现记录', structureChecked: false,
  })
  assert.equal(built.ok, true)
  if (built.ok) {
    const keys = Object.keys(built.payload).sort()
    assert.deepEqual(keys, ['confirm', 'decision', 'note', 'operation_id'])
    assert.equal('confirm_structure_checked' in built.payload, false)
  }
  // 决策白名单只有 4 个，且都不写云端。
  for (const spec of Object.values(DECISION_SPECS)) {
    assert.equal(spec.autoRetryAllowed, false)
    assert.ok(['retire_guarded', 'cloud_verified', 'cloud_untouched', 'keep'].includes(spec.decision))
  }
})

test('W3：retire_guarded 的风险文案明确“退场不等于可重传”', () => {
  const spec = DECISION_SPECS.retire_guarded
  assert.equal(spec.guardRetained, true)
  assert.match(spec.risk, /防重复闸门仍然生效/)
  assert.match(spec.risk, /退场不等于可以重传/)
})

test('W3：状态 → 允许决策映射不越权', () => {
  assert.deepEqual(decisionsFor(operation({ status: 'retired_guarded', error_code: RETIRED_GUARDED_CODE })), ['keep'])
  assert.deepEqual(decisionsFor(operation({ status: 'verified', allowed_next_actions: [] })), [])
  assert.deepEqual(decisionsFor(operation({ status: 'failed', allowed_next_actions: ['repreview'] })), [])
  assert.deepEqual(decisionsFor(operation({ status: 'not_started', allowed_next_actions: ['repreview'] })), [])
  const uncertain = decisionsFor(operation())
  assert.deepEqual(uncertain, ['keep', 'retire_guarded'])
  const pending = decisionsFor(operation({ status: 'writing', allowed_next_actions: ['recover_journal', 'manual_reconcile'] }))
  assert.deepEqual(pending, ['keep', 'retire_guarded', 'cloud_verified', 'cloud_untouched'])
})

test('W3：面板展示目标日期、操作标识、风险与允许动作', () => {
  const view = recoveryOperationView(operation())
  assert.ok(view)
  assert.equal(view?.operationId, OP_ID)
  assert.equal(view?.operationRef, 'wps-op:abc123def456')
  assert.equal(view?.targetDate, '2026-09-20')
  assert.equal(view?.sheetCount, 1)
  assert.match(view?.statusLabel || '', /待核对/)
  assert.match(view?.risk || '', /防重复闸门仍在/)
  assert.deepEqual(view?.allowedActions, ['manual_reconcile'])
  assert.match(view?.allowedActionLabels.join(''), /人工只读核对/)
  assert.equal(view?.blockingRetained, true)
})

test('W3：retired_guarded 展示专用标签与保留闸门风险', () => {
  const view = recoveryOperationView(operation({
    status: 'retired_guarded', pending: false, error_code: RETIRED_GUARDED_CODE,
    allowed_next_actions: ['manual_reconcile'],
  }))
  assert.match(view?.statusLabel || '', /已带审计退场/)
  assert.match(view?.risk || '', /防重复闸门阻断/)
  assert.equal(view?.alreadyRetired, true)
  assert.deepEqual(view?.decisions, ['keep'])
})

test('W3：恢复面板永不自动恢复写入', () => {
  assert.equal(RECOVERY_AUTO_RESUME_ALLOWED, false)
})

test('W3：双击/连点不会重复提交（单飞闸门）', () => {
  const gate = new ResolveGate()
  const first = gate.begin()
  assert.notEqual(first, null)
  // 在途期间的第二次点击必须被拒绝。
  assert.equal(gate.begin(), null)
  assert.equal(gate.begin(), null)
  assert.equal(gate.pending, true)
  gate.finish(first as number)
  assert.equal(gate.pending, false)
  // 第一次结束后才允许再提交。
  const second = gate.begin()
  assert.notEqual(second, null)
  assert.notEqual(second, first)
})

test('W3：乱序响应不会被当成当前结果（旧 token 必须丢弃）', () => {
  const gate = new ResolveGate()
  const first = gate.begin() as number
  gate.finish(first)
  const second = gate.begin() as number
  // 更晚的请求已经发出：第一个请求的迟到响应不能再改界面状态。
  assert.equal(gate.isCurrent(first), false)
  assert.equal(gate.isCurrent(second), true)
  gate.finish(second)
  assert.equal(gate.isCurrent(second), true)
})

function resolveResult(overrides: Partial<WpsRecoveryResolveResult> = {}): WpsRecoveryResolveResult {
  return {
    ok: true,
    status: 'retired_guarded',
    code: 'retired_guarded',
    reason: '',
    next_action: 'manual_reconcile',
    cloud_write: false,
    changed: true,
    verified_on_disk: true,
    scope: {
      operation_ref: 'wps-op:abc123def456',
      target_dates: ['2026-09-20'],
      target_refs: ['wps-target:abc123def456'],
      sheet_count: 1,
      guard_retained: true,
      blocking: 'retired_guarded',
    },
    ...overrides,
  }
}

test('W3：退场成功仍保持阻断，措辞不声称可重传', () => {
  const view = recoveryResolveView(resolveResult())
  assert.equal(view.ok, true)
  assert.equal(view.blockingRetained, true)
  assert.equal(view.cloudWrite, false)
  assert.match(view.title, /防重复闸门仍保留/)
  assert.match(view.detail, /没有写云端/)
  assert.match(view.detail, /不会因此变成可以重传/)
})

test('W3：already_retired 幂等，不重复写盘', () => {
  const view = recoveryResolveView(resolveResult({
    status: 'already_retired', code: 'already_retired', changed: false,
    audit: {
      actor: 'admin@example.com', at: '2026-09-20T10:00:00', decision: 'retire_guarded',
      note_recorded: true, duplicate: true,
      effects: { cloud_written: false, guard_retained: true, blocking: 'retired_guarded', auto_retry_allowed: false },
    },
  }))
  assert.equal(view.ok, true)
  assert.equal(view.changed, false)
  assert.equal(view.duplicate, true)
  assert.equal(view.blockingRetained, true)
  assert.match(view.title, /已退场/)
})

test('W3：确认失败保留阻断，不显示成功', () => {
  for (const code of ['note_required', 'confirmation_required', 'structure_confirmation_required', 'invalid_operation_id', 'decision_not_allowed']) {
    const view = recoveryResolveView(resolveResult({ ok: false, status: 'rejected', code, changed: false }))
    assert.equal(view.ok, false, code)
    assert.equal(view.blockingRetained, true, code)
    assert.equal(view.autoRetryAllowed, false, code)
    assert.doesNotMatch(view.title, /成功|已完成/, code)
  }
})

test('W3：权限不足提示准确且保持阻断', () => {
  const view = recoveryResolveView(resolveResult({ ok: false, status: 'forbidden', code: 'forbidden', changed: false }))
  assert.equal(view.ok, false)
  assert.equal(view.tone, 'warning')
  assert.match(view.title, /当前账号没有该权限/)
  assert.equal(view.blockingRetained, true)
})

test('W3：云端证明不足不得当作可重传', () => {
  for (const code of ['cloud_verify_failed', 'cloud_not_untouched']) {
    const view = recoveryResolveView(resolveResult({ ok: false, status: 'failed', code, changed: false }))
    assert.equal(view.ok, false, code)
    assert.match(view.nextStep, /不得.*重传/, code)
    assert.equal(view.blockingRetained, true, code)
  }
})

test('W3：本地日志持久化失败保持阻断，不提供删除账本/直接重传捷径', () => {
  for (const code of ['journal_write_failed', 'journal_unreadable', 'local_state_blocked']) {
    const view = recoveryResolveView(resolveResult({ ok: false, status: 'failed', code, changed: false }))
    assert.equal(view.ok, false, code)
    assert.equal(view.blockingRetained, true, code)
    const text = `${view.detail}${view.nextStep}`
    // 先剥掉「不要…」「不得…」这类否定式提示，剩下的文本里不允许出现任何捷径。
    const stripped = text.replace(/[，；。]?\s*(不要|不得|不能|禁止)[^，；。]*/g, '')
    assert.doesNotMatch(stripped, /删除账本|删除日志|直接重传|强制清除|重新上传即可/, code)
    assert.match(text, /修复|核对|升级/, code)
  }
  const unreadable = recoveryResolveView(resolveResult({ ok: false, status: 'failed', code: 'journal_unreadable', changed: false }))
  assert.match(`${unreadable.detail}${unreadable.nextStep}`, /不要删除账本/)
  const versionUnsupported = recoveryResolveView(resolveResult({ ok: false, status: 'failed', code: 'local_state_blocked', changed: false }))
  assert.match(`${versionUnsupported.detail}${versionUnsupported.nextStep}`, /不要删除日志/)
})

test('F3：verified_on_disk=false 时必须提示未确认落盘并保持阻断', () => {
  const retired = recoveryResolveView(resolveResult({ verified_on_disk: false }))
  assert.equal(retired.ok, true)
  assert.equal(retired.blockingRetained, true)
  assert.match(retired.title, /未确认落盘/)
  assert.match(retired.detail, /重新读盘没有确认/)
  assert.doesNotMatch(retired.detail, /已写入审计退场记录/)

  const kept = recoveryResolveView(resolveResult({
    status: 'keep_recorded', code: 'keep_recorded', verified_on_disk: false,
  }))
  assert.match(kept.title, /未确认落盘/)
  assert.equal(kept.blockingRetained, true)

  // verified_on_disk=true 的常态文案不受影响。
  assert.match(recoveryResolveView(resolveResult()).title, /防重复闸门仍保留/)
})

test('W3：keep 只留痕，阻断保持', () => {
  const view = recoveryResolveView(resolveResult({
    status: 'keep_recorded', code: 'keep_recorded', next_action: 'manual_reconcile',
  }))
  assert.equal(view.ok, true)
  assert.equal(view.blockingRetained, true)
  assert.match(view.title, /阻断保持/)
})

test('W3：retired_guarded 计数兼容三种来源', () => {
  assert.equal(retiredGuardedCount({ counts: { retired_guarded: 2 } }), 2)
  assert.equal(retiredGuardedCount({ summary: { retired_guarded_count: 3 } }), 3)
  assert.equal(retiredGuardedCount({ retired_guarded_count: 4 }), 4)
  assert.equal(retiredGuardedCount(null), 0)
})
