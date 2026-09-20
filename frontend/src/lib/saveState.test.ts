import assert from 'node:assert/strict'
import test from 'node:test'
import { INITIAL_SAVE_STATE, saveStateReducer, saveStateView } from './saveState.ts'

test('自动保存状态机：未保存→保存中→已保存/失败重试', () => {
  let s = INITIAL_SAVE_STATE
  assert.equal(saveStateView(s).label, '尚未改动')
  s = saveStateReducer(s, { type: 'change' })
  assert.equal(saveStateView(s).label, '未保存')
  s = saveStateReducer(s, { type: 'submit' })
  assert.equal(saveStateView(s).label, '保存中…')
  s = saveStateReducer(s, { type: 'failure', error: '网络已断开' })
  assert.equal(saveStateView(s).canRetry, true)
  assert.match(saveStateView(s).label, /保存失败/)
  s = saveStateReducer(s, { type: 'retry' })
  assert.equal(s.phase, 'saving')
  s = saveStateReducer(s, { type: 'success', at: 123 })
  assert.equal(s.phase, 'saved')
  assert.equal(s.savedAt, 123)
})

test('保存中收到新改动不打断本次提交', () => {
  const saving = saveStateReducer(INITIAL_SAVE_STATE, { type: 'submit' })
  assert.equal(saveStateReducer(saving, { type: 'change' }).phase, 'saving')
})

test('版本化 reducer：保存中 change 记录新版本，旧响应不会覆盖新草稿', () => {
  let s = INITIAL_SAVE_STATE
  s = saveStateReducer(s, { type: 'submit', version: 1 })
  s = saveStateReducer(s, { type: 'change', version: 2 })
  assert.equal(s.phase, 'saving')
  assert.equal(s.draftVersion, 2)
  assert.equal(s.pending, true)
  // 旧版本 1 成功返回：不能把当前草稿 2 标成 saved
  const staleSuccess = saveStateReducer(s, { type: 'success', at: 1, version: 1 })
  assert.equal(staleSuccess.phase, 'saving')
  assert.equal(staleSuccess.savedVersion, 1)
  assert.equal(staleSuccess.pending, true)
  // 旧版本 1 失败：退回 dirty，保留最新草稿等待重试
  const staleFailure = saveStateReducer(s, { type: 'failure', error: '旧请求失败', version: 1 })
  assert.equal(staleFailure.phase, 'dirty')
  assert.equal(staleFailure.draftVersion, 2)
  assert.equal(staleFailure.error, '旧请求失败')
})
