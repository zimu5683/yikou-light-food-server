/**
 * `lib/logSheet.ts` 的回归锁 —— 手机端日志底部抽屉的几何与吸附。
 *
 * 这些计算**错了不会报错**：拖拽方向反了会「往上拖反而缩回」、吸附写错会停在
 * 越界高度、按像素算会在不同屏高的手机上表现不一致（小屏露出大半个把手、
 * 大屏露出半屏）。都只能靠测试守住。
 */
import assert from 'node:assert/strict'
import test from 'node:test'

import {
  LOG_SHEET,
  clampFraction,
  dragToFraction,
  isCollapsed,
  readStoredHalf,
  snapFraction,
} from './logSheet.ts'

// ----------------------------------------------------------------------
// clampFraction：夹到合法区间
// ----------------------------------------------------------------------
test('clampFraction 把超出上限的值夹到 full', () => {
  assert.equal(clampFraction(1.5, 0), LOG_SHEET.full)
})

test('clampFraction 把负值夹到 0', () => {
  assert.equal(clampFraction(-0.2, 0), 0)
})

test('clampFraction 保留区间内的正常值', () => {
  assert.equal(clampFraction(0.7, 0), 0.7)
})

test('clampFraction 遇到非有限值一律回退到默认展开高度', () => {
  // NaN / ±Infinity 只可能来自「没量到视口高度」这类异常，
  // 回退到默认大半屏比「铺满全屏」安全：不会把任务界面整个盖住。
  assert.equal(clampFraction(Number.NaN, 0), LOG_SHEET.half)
  assert.equal(clampFraction(Number.POSITIVE_INFINITY, 0), LOG_SHEET.half)
  assert.equal(clampFraction(Number.NEGATIVE_INFINITY, 0), LOG_SHEET.half)
})

test('clampFraction 在已知屏高下保证收起时至少露出 26px 把手', () => {
  // 一条很矮的视口（横屏手机）：3.6% 只有 ~15px，必须抬到 26px 对应比例
  const viewport = 400
  const value = clampFraction(0, viewport)
  assert.ok(value * viewport >= 26 - 1e-9, `收起草露高度不足：${value * viewport}px`)
})

test('clampFraction 屏高充足时不会被 26px 下限影响', () => {
  const viewport = 900
  assert.equal(clampFraction(LOG_SHEET.peek, viewport), LOG_SHEET.peek)
})

// ----------------------------------------------------------------------
// isCollapsed
// ----------------------------------------------------------------------
test('isCollapsed 在收起位为真', () => {
  assert.equal(isCollapsed(LOG_SHEET.peek, 900), true)
})

test('isCollapsed 在展开位为假', () => {
  assert.equal(isCollapsed(LOG_SHEET.half, 900), false)
})

// ----------------------------------------------------------------------
// snapFraction：松手吸附到最近的停靠位
// ----------------------------------------------------------------------
test('snapFraction 收起位附近吸到 peek', () => {
  assert.equal(snapFraction(0.05, 900), LOG_SHEET.peek)
})

test('snapFraction 展开位附近吸到 half', () => {
  assert.equal(snapFraction(0.6, 900), LOG_SHEET.half)
})

test('snapFraction 接近顶部吸到 full', () => {
  assert.equal(snapFraction(0.88, 900), LOG_SHEET.full)
})

test('snapFraction 按下探到一半以下仍吸回 half，不会半途悬停', () => {
  // peek=0.036, half=0.62 的中点约 0.328；0.35 仍应吸到 half
  assert.equal(snapFraction(0.35, 900), LOG_SHEET.half)
})

test('snapFraction 明显拖到底部吸回 peek', () => {
  assert.equal(snapFraction(0.1, 900), LOG_SHEET.peek)
})

test('snapFraction 对越界输入同样安全', () => {
  assert.equal(snapFraction(9, 900), LOG_SHEET.full)
  assert.equal(snapFraction(-9, 900), LOG_SHEET.peek)
})

// ----------------------------------------------------------------------
// dragToFraction：拖拽方向（最容易写反的地方）
// ----------------------------------------------------------------------
test('dragToFraction 手指向上拖 → 高度变大', () => {
  const viewport = 1000
  const next = dragToFraction(0.4, 500, 400, viewport) // 上移 100px
  assert.ok(next > 0.4, `向上拖应增高，实际 ${next}`)
  assert.equal(next, 0.5)
})

test('dragToFraction 手指向下拖 → 高度变小', () => {
  const viewport = 1000
  const next = dragToFraction(0.6, 400, 500, viewport) // 下移 100px
  assert.ok(next < 0.6, `向下拖应降低，实际 ${next}`)
  assert.equal(next, 0.5)
})

test('dragToFraction 向上拖超过上限时夹到 full', () => {
  const viewport = 1000
  assert.equal(dragToFraction(0.85, 500, 0, viewport), LOG_SHEET.full)
})

test('dragToFraction 向下拖到底时不会低于 0', () => {
  const viewport = 1000
  assert.ok(dragToFraction(0.4, 100, 900, viewport) >= 0)
})

test('dragToFraction 未量到视口高度时保持原值', () => {
  assert.equal(dragToFraction(0.5, 100, 200, 0), 0.5)
})

// ----------------------------------------------------------------------
// readStoredHalf：localStorage 记忆
// ----------------------------------------------------------------------
test('readStoredHalf 缺失时回退到默认大半屏', () => {
  assert.equal(readStoredHalf(null), LOG_SHEET.half)
})

test('readStoredHalf 读到合法值就用它', () => {
  assert.equal(readStoredHalf('0.75'), 0.75)
})

test('readStoredHalf 拒绝非法字符串', () => {
  assert.equal(readStoredHalf('abc'), LOG_SHEET.half)
})

test('readStoredHalf 拒绝越界数值（避免抽屉高度异常）', () => {
  assert.equal(readStoredHalf('5'), LOG_SHEET.half)
  assert.equal(readStoredHalf('-1'), LOG_SHEET.half)
})
