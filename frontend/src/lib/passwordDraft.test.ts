import assert from 'node:assert/strict'
import test from 'node:test'
import { nextPasswordDraft, passwordResetVersion, shouldResetPasswordDraft } from './passwordDraft.ts'

test('清除密码成功信号只清对应模式，且普通配置刷新不会覆盖输入', () => {
  let orderDraft = 'typed-order'
  let sssDraft = 'typed-sss'
  const normalRefresh = { mode: null, nonce: 0 }
  assert.equal(nextPasswordDraft(orderDraft, normalRefresh, 'order'), 'typed-order')
  assert.equal(nextPasswordDraft(sssDraft, normalRefresh, 'sss'), 'typed-sss')

  const clearedOrder = { mode: 'order' as const, nonce: 1 }
  orderDraft = nextPasswordDraft(orderDraft, clearedOrder, 'order')
  assert.equal(orderDraft, '')
  assert.equal(nextPasswordDraft(sssDraft, clearedOrder, 'sss'), 'typed-sss')

  sssDraft = nextPasswordDraft(sssDraft, { mode: 'sss', nonce: 2 }, 'sss')
  assert.equal(sssDraft, '')
  assert.equal(shouldResetPasswordDraft({ mode: 'order', nonce: 0 }, 'order'), false)
})

test('passwordResetVersion 只对匹配模式生效，供表单派生草稿版本', () => {
  assert.equal(passwordResetVersion({ mode: null, nonce: 0 }, 'order'), 0)
  assert.equal(passwordResetVersion({ mode: 'order', nonce: 3 }, 'order'), 3)
  assert.equal(passwordResetVersion({ mode: 'order', nonce: 3 }, 'sss'), 0)
})
