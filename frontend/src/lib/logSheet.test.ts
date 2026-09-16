/**
 * `lib/logSheet.ts` 的回归锁 —— 手机端日志抽屉的几何与拖拽换算。
 *
 * 这些计算**错了不会报错**，只会表现为别扭的交互，所以必须靠测试守住。
 * 其中多条直接对应使用者实际报上来的问题：
 * - 「拖到一半看不到内容」→ 高度范围必须连续（不得吸附到固定档位）；
 * - 「拖到最大时把手不见了」→ 任何合法高度都必须给顶部留白；
 * - 「往上拖反而缩回」→ 拖拽方向；
 * - 「拖到任意位置却停在别处」→ 松手后除「接近收起」外一律原样保留。
 */
import assert from 'node:assert/strict'
import test from 'node:test'

import {
  DRAG_SLOP_PX,
  HANDLE_PX,
  LOG_SHEET,
  clampHeight,
  dragToHeight,
  isClick,
  isCollapsed,
  maxHeight,
  readStoredHeight,
  shouldCollapse,
} from './logSheet.ts'

const VIEWPORT = 900
const MAX = VIEWPORT - LOG_SHEET.topGapPx

// ----------------------------------------------------------------------
// maxHeight：顶部留白（「拖到全屏时把手消失」的根治点）
// ----------------------------------------------------------------------
test('maxHeight 永远小于视口高度（顶部留白让把手可见）', () => {
  assert.equal(maxHeight(VIEWPORT), MAX)
  assert.ok(maxHeight(VIEWPORT) < VIEWPORT)
})

test('maxHeight 在很小的视口下也不会低于收起高度', () => {
  assert.ok(maxHeight(100) >= LOG_SHEET.peekPx)
})

test('maxHeight 对无效视口给出兜底值而不是 0', () => {
  assert.ok(maxHeight(0) > 0)
  assert.ok(maxHeight(Number.NaN) > 0)
})

// ----------------------------------------------------------------------
// clampHeight：高度范围必须连续（「拖到一半没内容」的根治点）
// ----------------------------------------------------------------------
test('clampHeight 保留区间内的任意高度（不做任何吸附）', () => {
  // 关键是「任意」：若这里被吸到固定档位，就会出现「拖到一半没有内容区」
  for (const h of [40, 137, 300, 511, 640, MAX]) {
    assert.equal(clampHeight(h, VIEWPORT), h, `高度 ${h} 被改动，说明存在吸附`)
  }
})

test('clampHeight 下限是收起高度（不能比把手还小）', () => {
  assert.equal(clampHeight(0, VIEWPORT), LOG_SHEET.peekPx)
  assert.equal(clampHeight(-50, VIEWPORT), LOG_SHEET.peekPx)
})

test('clampHeight 上限等于 maxHeight', () => {
  assert.equal(clampHeight(99999, VIEWPORT), MAX)
})

test('clampHeight 对 NaN 回退到收起高度', () => {
  assert.equal(clampHeight(Number.NaN, VIEWPORT), LOG_SHEET.peekPx)
})

// ----------------------------------------------------------------------
// isCollapsed
// ----------------------------------------------------------------------
test('isCollapsed 只在收起高度附近为真', () => {
  assert.equal(isCollapsed(LOG_SHEET.peekPx), true)
  assert.equal(isCollapsed(LOG_SHEET.peekPx + 0.5), true)
  assert.equal(isCollapsed(LOG_SHEET.peekPx + 5), false)
  assert.equal(isCollapsed(300), false)
})

// ----------------------------------------------------------------------
// dragToHeight：方向（最容易写反）
// ----------------------------------------------------------------------
test('dragToHeight 手指向上拖 → 抽屉变高', () => {
  assert.equal(dragToHeight(300, 500, 400, VIEWPORT), 400)
})

test('dragToHeight 手指向下拖 → 抽屉变矮', () => {
  assert.equal(dragToHeight(400, 400, 500, VIEWPORT), 300)
})

test('dragToHeight 向上拖超过上限时夹在 maxHeight（把手不会跑出屏幕）', () => {
  assert.equal(dragToHeight(300, 800, 0, VIEWPORT), MAX)
})

test('dragToHeight 向下拖到底时夹在收起高度', () => {
  assert.equal(dragToHeight(300, 100, 900, VIEWPORT), LOG_SHEET.peekPx)
})

test('dragToHeight 位移与高度变化 1:1（不放大也不缩小）', () => {
  const before = 350
  const after = dragToHeight(before, 600, 550, VIEWPORT)
  assert.equal(after - before, 50)
})

// ----------------------------------------------------------------------
// isClick / shouldCollapse：点击与拖动的区分、拖到底即收起
// ----------------------------------------------------------------------
test('isClick 在小位移时为真（区分点击与拖动）', () => {
  assert.equal(isClick(0), true)
  assert.equal(isClick(DRAG_SLOP_PX - 1), true)
  assert.equal(isClick(-(DRAG_SLOP_PX - 1)), true)
})

test('isClick 在超过阈值时为假（拖动不应触发开合切换）', () => {
  assert.equal(isClick(DRAG_SLOP_PX + 1), false)
  assert.equal(isClick(-40), false)
})

test('shouldCollapse 只在拖到接近收起时为真', () => {
  assert.equal(shouldCollapse(LOG_SHEET.peekPx), true)
  assert.equal(shouldCollapse(LOG_SHEET.peekPx + HANDLE_PX), false)
  assert.equal(shouldCollapse(400), false)
  assert.equal(shouldCollapse(MAX), false)
})

// ----------------------------------------------------------------------
// readStoredHeight：记忆上次拖到的高度
// ----------------------------------------------------------------------
test('readStoredHeight 缺失时回退到约 62% 的大半屏', () => {
  const fallback = readStoredHeight(null, VIEWPORT)
  assert.ok(fallback > MAX * 0.5 && fallback < MAX * 0.75, `回退值不合理：${fallback}`)
})

test('readStoredHeight 读到合法值就原样用（不吸附）', () => {
  assert.equal(readStoredHeight('437', VIEWPORT), 437)
})

test('readStoredHeight 拒绝非法字符串', () => {
  assert.equal(readStoredHeight('abc', VIEWPORT), readStoredHeight(null, VIEWPORT))
})

test('readStoredHeight 把越界值夹进合法区间', () => {
  assert.equal(readStoredHeight('99999', VIEWPORT), MAX)
  assert.equal(readStoredHeight('-10', VIEWPORT), LOG_SHEET.peekPx)
})
