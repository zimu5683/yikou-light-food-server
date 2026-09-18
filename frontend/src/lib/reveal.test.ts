/**
 * `lib/reveal.ts` 的回归锁 —— 手机端日志「水波展开/收回」的几何与降级判定。
 *
 * 这些计算错了不会报错，只会表现为：
 * - 水波扩散完面板缺一角 → 半径必须取四角最大；
 * - 半径算小了日志被切掉 → 结束半径必须盖住四个角；
 * - 收回时反向波不完整 → closeFrames 必须正好是 openFrames 的倒放。
 */
import assert from 'node:assert/strict'
import test from 'node:test'

import {
  FAB,
  LOG_REVEAL_ORIGIN,
  LOG_REVEAL_RADIUS,
  REVEAL_TIMING,
  canAnimate,
  centerOfRect,
  circleClip,
  closeFrames,
  closeFramesCss,
  cornerRadius,
  openFrames,
  openFramesCss,
  pointFromEvent,
  prefersReducedMotion,
  resolveOrigin,
  type Point,
  type RectLike,
} from './reveal.ts'

/** 手机视口 400×800；日志展开后铺满整个视口。 */
const VIEWPORT_W = 400
const VIEWPORT_H = 800
const SHEET: RectLike = { left: 0, top: 0, width: VIEWPORT_W, height: VIEWPORT_H }

/** 右上角日志按钮的中心：展开、收回都围绕这个点扩散。 */
const FAB_CENTER: Point = {
  x: VIEWPORT_W - FAB.rightPx - FAB.sizePx / 2,
  y: FAB.fallbackTopPx + FAB.sizePx / 2,
}

function corners(rect: RectLike): Point[] {
  return [
    { x: rect.left, y: rect.top },
    { x: rect.left + rect.width, y: rect.top },
    { x: rect.left, y: rect.top + rect.height },
    { x: rect.left + rect.width, y: rect.top + rect.height },
  ]
}

function distance(a: Point, b: Point) {
  return Math.hypot(a.x - b.x, a.y - b.y)
}

function radiusOf(clip: string): number {
  const matched = clip.match(/^circle\((\d+)px at (\d+)px (\d+)px\)$/)
  assert.ok(matched, `不是合法的 circle() 值：${clip}`)
  return Number.parseInt(matched[1], 10)
}

test('自检：日志按钮固定在视口右上角，且圆心到最远角足够远', () => {
  assert.ok(FAB_CENTER.x > VIEWPORT_W * 0.75)
  assert.ok(FAB_CENTER.y < VIEWPORT_H * 0.25)
  assert.ok(cornerRadius(FAB_CENTER, SHEET) > 500)
})

// ----------------------------------------------------------------------
// cornerRadius：必须盖住四个角
// ----------------------------------------------------------------------
test('cornerRadius 盖住全屏日志的四个角', () => {
  const radius = cornerRadius(FAB_CENTER, SHEET)
  for (const corner of corners(SHEET)) {
    assert.ok(radius >= distance(FAB_CENTER, corner), `角落 ${JSON.stringify(corner)} 露在圆外`)
  }
})

test('cornerRadius 取四角最大而不是最近的那个角', () => {
  const origin = { x: 380, y: 20 }
  const far = distance(origin, { x: 0, y: 800 })
  assert.equal(cornerRadius(origin, SHEET), Math.ceil(far))
  assert.ok(cornerRadius(origin, SHEET) > distance(origin, { x: 400, y: 0 }))
})

test('cornerRadius 圆心在矩形正中心时等于半对角线', () => {
  const rect: RectLike = { left: 0, top: 0, width: 60, height: 80 }
  assert.equal(cornerRadius({ x: 30, y: 40 }, rect), Math.ceil(Math.hypot(30, 40)))
})

test('cornerRadius 对 0 尺寸矩形退化为「圆心到该点」的距离', () => {
  const rect: RectLike = { left: 10, top: 20, width: 0, height: 0 }
  assert.equal(cornerRadius({ x: 13, y: 24 }, rect), 5)
})

test('cornerRadius 对无效圆心给 0 而不是 NaN', () => {
  assert.equal(cornerRadius({ x: Number.NaN, y: 10 }, SHEET), 0)
  assert.equal(cornerRadius({ x: Number.NaN, y: Number.NaN }, SHEET), 0)
})

test('cornerRadius 对含 NaN 的矩形不产生 NaN，且仍盖住能算出来的角', () => {
  const broken: RectLike = { left: 0, top: 0, width: Number.NaN, height: 10 }
  const radius = cornerRadius(FAB_CENTER, broken)
  assert.ok(Number.isFinite(radius), '半径不能是 NaN')
  assert.ok(radius >= distance(FAB_CENTER, { x: 0, y: 0 }))
  assert.ok(radius >= distance(FAB_CENTER, { x: 0, y: 10 }))
})

test('cornerRadius 向上取整（亚像素半径会让 clip-path 边缘发虚）', () => {
  const rect: RectLike = { left: 0, top: 0, width: 10, height: 10 }
  const radius = cornerRadius({ x: 3, y: 4 }, rect)
  assert.equal(radius, Math.ceil(Math.hypot(7, 6)))
  assert.equal(radius, Math.ceil(radius))
})

// ----------------------------------------------------------------------
// openFrames / closeFrames：展开和反向收回
// ----------------------------------------------------------------------
test('openFrames 从日志按钮那一点（半径 0）开始', () => {
  const frames = openFrames(FAB_CENTER, SHEET)
  assert.equal(frames.from, circleClip(FAB_CENTER, 0))
  assert.equal(radiusOf(frames.from), 0)
})

test('openFrames 的结束圆盖住整个全屏日志', () => {
  const frames = openFrames(FAB_CENTER, SHEET)
  const endRadius = radiusOf(frames.to)
  assert.equal(endRadius, cornerRadius(FAB_CENTER, SHEET))
  for (const corner of corners(SHEET)) {
    assert.ok(endRadius >= distance(FAB_CENTER, corner), `角落 ${JSON.stringify(corner)} 没被盖住`)
  }
  assert.ok(endRadius > radiusOf(frames.from), '结束半径必须大于起始半径，否则水波看不见')
})

test('openFrames 的半径增长足够明显（不是一闪而过）', () => {
  assert.ok(cornerRadius(FAB_CENTER, SHEET) >= 300)
})

test('openFrames 在退化的矩形上也不产生 NaN', () => {
  const degenerate: RectLike = { left: 100, top: 100, width: 0, height: 0 }
  const frames = openFrames({ x: 100, y: 100 }, degenerate)
  assert.equal(frames.from, 'circle(0px at 100px 100px)')
  assert.equal(frames.to, 'circle(0px at 100px 100px)')
})

test('closeFrames 正好是 openFrames 的倒放', () => {
  const opened = openFrames(FAB_CENTER, SHEET)
  const closed = closeFrames(FAB_CENTER, SHEET)
  assert.equal(closed.from, opened.to)
  assert.equal(closed.to, opened.from)
})

// ----------------------------------------------------------------------
// circleClip
// ----------------------------------------------------------------------
test('circleClip 生成合法的 circle() 值并四舍五入坐标', () => {
  assert.equal(circleClip({ x: 351.6, y: 595.4 }, 456.2), 'circle(457px at 352px 595px)')
})

test('circleClip 对非正/非有限半径退化为 0（完全不可见）而不是负数', () => {
  assert.equal(circleClip({ x: 1, y: 2 }, 0), 'circle(0px at 1px 2px)')
  assert.equal(circleClip({ x: 1, y: 2 }, -30), 'circle(0px at 1px 2px)')
  assert.equal(circleClip({ x: 1, y: 2 }, Number.NaN), 'circle(0px at 1px 2px)')
})

test('circleClip 对无效坐标退化为 0 而不是 NaN', () => {
  assert.equal(circleClip({ x: Number.NaN, y: Number.NaN }, 10), 'circle(10px at 0px 0px)')
})

// ----------------------------------------------------------------------
// 圆心选取
// ----------------------------------------------------------------------
test('pointFromEvent 直接取 clientX/clientY', () => {
  assert.deepEqual(pointFromEvent({ clientX: 12, clientY: 34 }), { x: 12, y: 34 })
})

test('centerOfRect 取矩形中心', () => {
  assert.deepEqual(centerOfRect({ left: 10, top: 20, width: 40, height: 60 }), { x: 30, y: 50 })
})

test('resolveOrigin 优先用点击点（「从被点的按钮扩散」）', () => {
  assert.deepEqual(resolveOrigin({ x: 5, y: 6 }, { x: 1, y: 1 }), { x: 5, y: 6 })
})

test('resolveOrigin 在点击点缺失或非法时退回日志按钮中心', () => {
  const fallback = { x: 100, y: 200 }
  assert.deepEqual(resolveOrigin(null, fallback), fallback)
  assert.deepEqual(resolveOrigin(undefined, fallback), fallback)
  assert.deepEqual(resolveOrigin({ x: Number.NaN, y: 3 }, fallback), fallback)
})

// ----------------------------------------------------------------------
// 降级判定
// ----------------------------------------------------------------------
test('prefersReducedMotion 命中系统设置时返回 true', () => {
  assert.equal(prefersReducedMotion(() => ({ matches: true })), true)
})

test('prefersReducedMotion 未命中时返回 false', () => {
  assert.equal(prefersReducedMotion(() => ({ matches: false })), false)
})

test('prefersReducedMotion 在没有 matchMedia 时返回 false（不是抛错）', () => {
  assert.equal(prefersReducedMotion(() => undefined), false)
})

test('prefersReducedMotion 在 matchMedia 抛错时返回 false', () => {
  assert.equal(
    prefersReducedMotion(() => {
      throw new Error('不支持')
    }),
    false,
  )
})

test('prefersReducedMotion 查询的是减弱动效这一条媒体查询', () => {
  const seen: string[] = []
  prefersReducedMotion((query) => {
    seen.push(query)
    return { matches: false }
  })
  assert.deepEqual(seen, ['(prefers-reduced-motion: reduce)'])
})

test('canAnimate 只在元素真的带 animate 方法时为真', () => {
  assert.equal(canAnimate({ animate: () => undefined }), true)
  assert.equal(canAnimate({}), false)
  assert.equal(canAnimate(null), false)
  assert.equal(canAnimate(undefined), false)
})

// ----------------------------------------------------------------------
// 常量本身要合理
// ----------------------------------------------------------------------
test('展开/收回动画时长都是有限正数且不至于长到卡手', () => {
  assert.ok(REVEAL_TIMING.openMs > 0 && REVEAL_TIMING.openMs <= 600)
  assert.ok(REVEAL_TIMING.closeMs > 0 && REVEAL_TIMING.closeMs <= 600)
})

test('日志按钮精确适配标题栏高度，且离右缘有安全间距', () => {
  // 标题栏内容高度为 40px；再大会溢出边界，再小会显得单薄。
  assert.equal(FAB.sizePx, 40)
  assert.ok(FAB.rightPx >= 8)
})

// ----------------------------------------------------------------------
// CSS 圆心：完全由布局表达式决定，不再依赖 JS 测量
// ----------------------------------------------------------------------
test('LOG_REVEAL_ORIGIN 直接定位到右上角日志按钮中心', () => {
  assert.ok(LOG_REVEAL_ORIGIN.includes('100%'))
  assert.ok(LOG_REVEAL_ORIGIN.includes('var(--safe-right)'))
  assert.ok(LOG_REVEAL_ORIGIN.includes('var(--safe-top)'))
  assert.ok(LOG_REVEAL_ORIGIN.includes('32px'))
  assert.ok(LOG_REVEAL_ORIGIN.includes('20px'))
})

test('openFramesCss 从半径 0 扩到 LOG_REVEAL_RADIUS（142vmax），圆心不变', () => {
  const frames = openFramesCss()
  assert.equal(frames.from, `circle(0px at ${LOG_REVEAL_ORIGIN})`)
  assert.equal(frames.to, `circle(${LOG_REVEAL_RADIUS} at ${LOG_REVEAL_ORIGIN})`)
})

test('closeFramesCss 是 openFramesCss 的倒放', () => {
  const opened = openFramesCss()
  const closed = closeFramesCss()
  assert.equal(closed.from, opened.to)
  assert.equal(closed.to, opened.from)
})
