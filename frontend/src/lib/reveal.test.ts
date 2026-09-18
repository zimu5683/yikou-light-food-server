/**
 * `lib/reveal.ts` 的回归锁 —— 手机端日志「水波扩散」的几何与降级判定。
 *
 * 这些计算**错了不会报错**，只会表现为别扭或残缺的动画，所以必须靠测试守住。
 * 对应使用者实际能看到的症状：
 * - 「水波扩散完，面板缺一角」→ 半径必须取四角最大，不能只算一角；
 * - 「半径算小了，日志内容被切掉」→ 结束半径必须盖住四个角；
 * - 「系统开了减弱动效还在转」→ prefersReducedMotion 的降级判定。
 */
import assert from 'node:assert/strict'
import test from 'node:test'

import {
  FAB,
  REVEAL_TIMING,
  canAnimate,
  centerOfRect,
  circleClip,
  cornerRadius,
  openFrames,
  pickDockHeight,
  pointFromEvent,
  prefersReducedMotion,
  resolveOrigin,
  type Point,
  type RectLike,
} from './reveal.ts'

/** 手机视口 400×800：日志面板展开后占满宽度、贴底、高 500。 */
const VIEWPORT_W = 400
const VIEWPORT_H = 800
const SHEET: RectLike = { left: 0, top: 300, width: VIEWPORT_W, height: 500 }

/** 收起时露出的把手条高度，与 `LOG_SHEET.peekPx` 一致。 */
const PEEK = 34
/** 操作栏高度（BottomDock 实测值），与 `FAB.fallbackDockPx` 一致。 */
const DOCK = FAB.fallbackDockPx

/** 悬浮按钮**收起时**的中心：它停在操作栏上方。点它就是在这个位置扩散。 */
const FAB_CENTER: Point = {
  x: VIEWPORT_W - FAB.rightPx - FAB.sizePx / 2,
  y: VIEWPORT_H - (PEEK + DOCK + FAB.gapPx) - FAB.sizePx / 2,
}

/** 悬浮按钮**展开后**的中心：跟着面板上移，此时在面板上边缘之外。 */
const FAB_CENTER_OPEN: Point = {
  x: FAB_CENTER.x,
  y: VIEWPORT_H - (SHEET.height + DOCK + FAB.gapPx) - FAB.sizePx / 2,
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

// ----------------------------------------------------------------------
// 真实几何自检：这组坐标就是手机上会发生的情形
// ----------------------------------------------------------------------
test('自检：收起时悬浮按钮在面板展开区域之内，展开后跑到面板上边缘之外', () => {
  assert.ok(FAB_CENTER.y > SHEET.top && FAB_CENTER.y < VIEWPORT_H, '收起时按钮应在面板区域内')
  assert.ok(FAB_CENTER_OPEN.y < SHEET.top, '展开后按钮应浮在面板上方')
})

// ----------------------------------------------------------------------
// cornerRadius：必须盖住四个角（「面板缺一角」的根治点）
// ----------------------------------------------------------------------
test('cornerRadius 盖住矩形的四个角（圆心在矩形内部，即收起时点悬浮按钮）', () => {
  const radius = cornerRadius(FAB_CENTER, SHEET)
  for (const corner of corners(SHEET)) {
    assert.ok(radius >= distance(FAB_CENTER, corner), `角落 ${JSON.stringify(corner)} 露在圆外`)
  }
})

test('cornerRadius 盖住矩形的四个角（圆心在矩形上方，即展开后点悬浮按钮）', () => {
  const radius = cornerRadius(FAB_CENTER_OPEN, SHEET)
  for (const corner of corners(SHEET)) {
    assert.ok(radius >= distance(FAB_CENTER_OPEN, corner), `角落 ${JSON.stringify(corner)} 露在圆外`)
  }
})

test('cornerRadius 取四角最大而不是最近的那个角', () => {
  // 圆心偏右下：左上角最远。只算右下角会得到一个小得多的值。
  const origin = { x: 380, y: 780 }
  const far = distance(origin, { x: 0, y: 300 })
  assert.equal(cornerRadius(origin, SHEET), Math.ceil(far))
  assert.ok(cornerRadius(origin, SHEET) > distance(origin, { x: 400, y: 800 }))
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
  // 有效的那两个角（x=0 的上下角）仍要在圆内，否则会露出空白
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
// openFrames：从按钮那一点扩到盖住整个面板
// ----------------------------------------------------------------------
test('openFrames 从半径 0（按钮那一点）开始', () => {
  const frames = openFrames(FAB_CENTER, SHEET)
  assert.equal(frames.from, circleClip(FAB_CENTER, 0))
  assert.equal(radiusOf(frames.from), 0)
})

test('openFrames 的结束圆盖住整个面板（否则日志会被切掉一角）', () => {
  for (const origin of [FAB_CENTER, FAB_CENTER_OPEN]) {
    const frames = openFrames(origin, SHEET)
    const endRadius = radiusOf(frames.to)
    assert.equal(endRadius, cornerRadius(origin, SHEET))
    for (const corner of corners(SHEET)) {
      assert.ok(endRadius >= distance(origin, corner), `角落 ${JSON.stringify(corner)} 没被盖住`)
    }
    assert.ok(endRadius > radiusOf(frames.from), '结束半径必须大于起始半径，否则水波看不见')
  }
})

test('openFrames 的半径增长足够明显（不是一闪而过）', () => {
  // 从悬浮按钮扩散时，结束半径至少要比起始大 300px，否则肉眼几乎看不出水波
  assert.ok(cornerRadius(FAB_CENTER, SHEET) - 0 >= 300)
})

test('openFrames 在退化的矩形上也不产生 NaN', () => {
  const degenerate: RectLike = { left: 100, top: 100, width: 0, height: 0 }
  const frames = openFrames({ x: 100, y: 100 }, degenerate)
  assert.equal(frames.from, 'circle(0px at 100px 100px)')
  assert.equal(frames.to, 'circle(0px at 100px 100px)')
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

test('resolveOrigin 在点击点缺失或非法时退回悬浮按钮中心', () => {
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
// 操作栏高度
// ----------------------------------------------------------------------
test('pickDockHeight 取可见操作栏的最大高度（隐藏页签量到 0）', () => {
  assert.equal(pickDockHeight([0, 90, 0]), 90)
  assert.equal(pickDockHeight([60, 90]), 90)
})

test('pickDockHeight 一个都没量到时退回兜底值（云文档页签没有操作栏）', () => {
  assert.equal(pickDockHeight([]), FAB.fallbackDockPx)
  assert.equal(pickDockHeight([0, 0]), FAB.fallbackDockPx)
  assert.equal(pickDockHeight([Number.NaN, -1]), FAB.fallbackDockPx)
})

test('pickDockHeight 允许调用方指定兜底值', () => {
  assert.equal(pickDockHeight([0], 123), 123)
})

// ----------------------------------------------------------------------
// 常量本身要合理（改坏了这里先报）
// ----------------------------------------------------------------------
test('扩散时长是有限正数且不至于长到卡手', () => {
  assert.ok(REVEAL_TIMING.openMs > 0 && REVEAL_TIMING.openMs <= 600)
})

test('悬浮按钮触达目标不小于 44px，且离右缘有安全间距', () => {
  assert.ok(FAB.sizePx >= 44)
  assert.ok(FAB.rightPx >= 8)
  assert.ok(FAB.gapPx >= 0)
})
