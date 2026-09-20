import assert from 'node:assert/strict'
import test from 'node:test'
import { evaluatePasswordClearResult } from './credentialClear.ts'

test('密码清除：删除成功/账号为空/此前已清才允许清空草稿并提示成功', () => {
  for (const state of ['deleted', 'account_empty', 'already_cleared']) {
    const decision = evaluatePasswordClearResult({
      ok: true, status: state === 'deleted' ? 'success' : 'no_change', state,
      mode: 'order', deleted: state === 'deleted', reason: '', next_action: '', summary: {},
    })
    assert.equal(decision.clearDraft, true, state)
    assert.equal(decision.toast, 'success', state)
  }
})

test('密码清除失败/未知/网络中断均保留草稿且绝不提示成功', () => {
  const failed = evaluatePasswordClearResult({
    ok: false, status: 'error', state: 'delete_failed', mode: 'order',
    deleted: false, reason: '模拟删除失败', next_action: '模拟下一步', summary: {},
  })
  assert.equal(failed.clearDraft, false)
  assert.equal(failed.toast, 'error')
  assert.match(failed.reason, /模拟删除失败/)
  assert.match(failed.nextAction, /模拟下一步/)

  const unknown = evaluatePasswordClearResult({
    ok: true, status: 'success', state: 'unknown_state', mode: 'sss',
    deleted: false, reason: '', next_action: '', summary: {},
  })
  assert.equal(unknown.clearDraft, false)
  assert.equal(unknown.toast, 'error')

  const thrown = evaluatePasswordClearResult(null, 'Failed to fetch')
  assert.equal(thrown.clearDraft, false)
  assert.equal(thrown.toast, 'error')
  assert.match(thrown.reason, /Failed to fetch/)
})

test('密码内容不参与判断或返回', () => {
  const decision = evaluatePasswordClearResult({
    ok: true, status: 'success', state: 'deleted', mode: 'order',
    deleted: true, reason: '', next_action: '', summary: {},
  })
  assert.equal(JSON.stringify(decision).includes('password'), false)
})
