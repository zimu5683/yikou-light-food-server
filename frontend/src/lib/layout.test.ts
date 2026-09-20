import assert from 'node:assert/strict'
import test from 'node:test'
import { isSoftKeyboardSafeHeight, layoutForWidth } from './layout.ts'

test('360/390/640/1024/1440 关键宽度档位分类正确', () => {
  assert.equal(layoutForWidth(360), 'phone')
  assert.equal(layoutForWidth(390), 'phone')
  assert.equal(layoutForWidth(640), 'tablet')
  assert.equal(layoutForWidth(800), 'tablet')
  assert.equal(layoutForWidth(1024), 'desktop')
  assert.equal(layoutForWidth(1440), 'desktop')
})

test('非有限宽度降级为 phone，软键盘高度判断可用', () => {
  assert.equal(layoutForWidth(Number.NaN), 'phone')
  assert.equal(isSoftKeyboardSafeHeight(800, 420), true)
  assert.equal(isSoftKeyboardSafeHeight(800, 900), false)
  assert.equal(isSoftKeyboardSafeHeight(Number.NaN), false)
})
