import assert from 'node:assert/strict'
import test from 'node:test'
import { journalIssueOf, previewFailureView, uploadFailureView } from './wpsFailure.ts'
import type { WpsUploadResult } from './bridge.ts'

function upload(overrides: Partial<WpsUploadResult> = {}): WpsUploadResult {
  return {
    ok: false,
    status: 'rejected',
    code: '',
    reason: '',
    next_action: '',
    operation_id: '',
    ...overrides,
  }
}

test('wps_disabled：说明开关语义与下一步，且证明零写入', () => {
  const view = uploadFailureView(upload({ code: 'wps_disabled', reason: '云文档同步未启用' }))
  assert.ok(view)
  assert.equal(view?.tone, 'warning')
  assert.match(view?.title || '', /云文档同步已关闭/)
  assert.match(view?.detail || '', /没有读云端、没有写任何内容/)
  assert.match(view?.nextStep || '', /开启/)
  assert.equal(view?.provenNoWrite, true)
  assert.equal(view?.retryAllowed, false)
})

test('blocked_concurrent / operation_conflict：统一并发文案，warning 且不需对账', () => {
  const concurrentPayload = upload({
    status: 'blocked_concurrent',
    code: 'blocked_concurrent',
    reason: '另一个任务正在运行，请等待后刷新（本次未发送任何下单请求）',
  })
  const view = uploadFailureView(concurrentPayload)
  assert.ok(view)
  assert.equal(view?.tone, 'warning')
  assert.match(view?.title || '', /另一个任务正在运行/)
  const conflict = uploadFailureView(upload({ code: 'operation_conflict' }))
  assert.match(conflict?.nextStep || '', /另一个任务正在运行，请等待后刷新/)
})

test('journal 损坏：保持阻断，提示修复而不是重传', () => {
  const view = uploadFailureView(upload({
    code: 'local_state_blocked',
    status: 'blocked',
    reason: '本地账本/意图日志不可用：意图日志 JSON 损坏',
  }))
  assert.ok(view)
  assert.equal(view?.tone, 'danger')
  assert.match(view?.title || '', /损坏/)
  assert.equal(view?.blockingRetained, true)
  assert.equal(view?.needsManualReconcile, true)
  assert.match(view?.nextStep || '', /不要重新上传|不要重传/)
})

test('journal 版本不受支持：单独文案，且不提供删除日志捷径', () => {
  const view = uploadFailureView(upload({
    code: 'local_state_blocked',
    status: 'blocked',
    reason: '本地账本/意图日志不可用：不支持的意图日志版本',
  }))
  assert.ok(view)
  assert.match(view?.title || '', /版本不受支持/)
  assert.match(view?.nextStep || '', /不要删除日志/)
  assert.doesNotMatch(view?.nextStep || '', /删除日志后重试/)
})

test('journalIssueOf 能区分损坏 / 版本 / 持久化失败', () => {
  assert.equal(journalIssueOf('local_state_blocked', '意图日志 JSON 损坏'), 'corrupt')
  assert.equal(journalIssueOf('local_state_blocked', '不支持的意图日志版本'), 'version_unsupported')
  assert.equal(journalIssueOf('journal_write_failed', 'journal_save_failed: OSError'), 'write_failed')
  assert.equal(journalIssueOf('journal_unreadable', ''), 'unreadable')
  assert.equal(journalIssueOf('preview_expired', ''), '')
})

test('持久化失败：明确不生效、不能当成功，且保持阻断', () => {
  const view = uploadFailureView(upload({
    code: 'journal_write_failed',
    status: 'failed',
    reason: 'journal_save_failed: 磁盘只读',
  }))
  assert.ok(view)
  assert.match(view?.title || '', /持久化失败/)
  assert.match(view?.detail || '', /不能当作成功/)
  assert.equal(view?.blockingRetained, true)
})

test('uncertain：不得当作成功也不得当作零写入', () => {
  const view = uploadFailureView(upload({ status: 'uncertain', code: '', reason: '' }))
  assert.ok(view)
  assert.match(view?.title || '', /不确定/)
  assert.equal(view?.provenNoWrite, false)
  assert.doesNotMatch(view?.title || '', /完成|成功/)
})

test('cloud_verify_failed / cloud_not_untouched 只能人工核对', () => {
  for (const code of ['cloud_verify_failed', 'cloud_not_untouched']) {
    const view = uploadFailureView(upload({ status: 'failed', code }))
    assert.equal(view?.needsManualReconcile, true, code)
    assert.equal(view?.blockingRetained, true, code)
    assert.match(view?.nextStep || '', /不要当作可以重传|不得/, code)
  }
})

test('成功结果不产生失败视图', () => {
  assert.equal(uploadFailureView(upload({ ok: true, status: 'success', code: '' })), null)
  assert.equal(uploadFailureView(null), null)
})

test('预览拒绝：wps_disabled / journal / 并发 都有明确下一步', () => {
  const disabled = previewFailureView({ code: 'wps_disabled', reason: '云文档同步未启用' })
  assert.match(disabled.title, /已关闭/)
  assert.match(disabled.detail, /不读云端、不建计划、不发一次性令牌/)
  const journal = previewFailureView({ code: 'local_state_blocked', reason: '不支持的意图日志版本' })
  assert.match(journal.title, /版本不受支持/)
  const conflict = previewFailureView({ code: 'operation_conflict' })
  assert.match(conflict.nextStep, /等待后刷新/)
  const fileChanged = previewFailureView({ code: 'local_file_changed' })
  assert.equal(fileChanged.provenNoWrite, true)
})

test('未知拒绝码也给出保守下一步（不要跳过预览直接上传）', () => {
  const view = previewFailureView({ code: 'plan_failed', reason: '计划失败' })
  assert.match(view.nextStep, /不要跳过预览直接上传|查看日志/)
  assert.equal(view.provenNoWrite, true)
})
