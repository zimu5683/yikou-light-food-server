/**
 * 手机端日志「水波扩散」的几何与降级判定（纯函数，无 React/DOM 依赖）。
 *
 * 为什么单独抽出来：圆形扩散**算错不会报错**，只会表现为「水波没盖住面板，
 * 角落露出空白」或「半径太小，日志内容被切掉」。抽成纯函数后可以用
 * `node --test` 直接覆盖，不依赖 jsdom（和 `logSheet.ts` 同一套路数）。
 *
 * ## 这个圆是怎么来的
 *
 * 圆心是**触发按钮的中心**（悬浮按钮 / 开始处理 / 确认上传），半径是圆心到
 * 面板矩形**四个角的最大距离** —— 只取到某一角的距离，另外三个角就会露在
 * 圆外（表现为扩散结束时面板缺一角）。这与 react-circular-reveal、
 * react-theme-switch-animation 内部用的是同一个套路，区别只是我们不做
 * View Transitions 快照（那会让流式日志在动画期间停止刷新）。
 *
 * ## 为什么起始半径是 0，而且展开时面板要「瞬间到目标高度」
 *
 * 收起状态下面板只剩底部一条 34px 把手条。若让面板高度照常做 200ms 过渡、
 * 同时跑扩散动画，扩散会在面板还没长起来时就把它整个盖住 —— 水波变成一闪
 * 而过（实测半径增长远快于高度增长）。所以展开时高度**直接跳到目标值**、
 * 由圆形裁剪负责揭示，`App` 里主内容区的 `padding-bottom` 另做 200ms 过渡
 * 让表单平滑上移。起始半径取 0 是 Material 圆形揭示的标准做法：水波从按钮
 * 那一点涌出，约三分之一行程时扫过把手条。
 *
 * ## 为什么收起不做圆形动画
 *
 * 面板贴底、把手条就是它的最下面一条，而圆心在面板上方 —— 把手条的某个角
 * 往往**就是整个面板的最远角**（`cornerRadius` 两者相等），收缩圆会变成空
 * 操作。收起沿用原有的高度下滑过渡（见 `LogConsole` 的 `PhoneLogSheet`），
 * 平滑且不会出现「把手条啪地弹出来」。
 */

/** 视口坐标下的一个点（与 clientX/clientY 同一坐标系）。 */
export interface Point {
  x: number
  y: number
}

/** `getBoundingClientRect()` 里我们真正用到的那几个字段。 */
export interface RectLike {
  left: number
  top: number
  width: number
  height: number
}

/** 扩散动画的时长与缓动。 */
export const REVEAL_TIMING = {
  openMs: 360,
  openEase: 'cubic-bezier(0.22, 1, 0.36, 1)',
} as const

/** 悬浮按钮的尺寸与落点。 */
export const FAB = {
  sizePx: 48,
  /** 距屏幕右缘。 */
  rightPx: 12,
  /** 与下方操作栏之间的空隙。 */
  gapPx: 10,
  /**
   * 量不到操作栏高度时的兜底值（云文档页签没有操作栏）。
   * 与 `TaskPanel` 里 BottomDock 的实际高度一致：pt-2.5(10) + 按钮 38 +
   * mt-3(12) + 「更多」一行 18 + pb-3(12) = 90。
   */
  fallbackDockPx: 90,
} as const

/** 从指针事件里取视口坐标。 */
export function pointFromEvent(event: { clientX: number; clientY: number }): Point {
  return { x: event.clientX, y: event.clientY }
}

/** 矩形中心。 */
export function centerOfRect(rect: RectLike): Point {
  return { x: rect.left + rect.width / 2, y: rect.top + rect.height / 2 }
}

function isUsablePoint(point: Point | null | undefined): point is Point {
  return Boolean(point) && Number.isFinite(point!.x) && Number.isFinite(point!.y)
}

/**
 * 选出本次扩散的圆心：优先用点击点，无效时退回兜底点（悬浮按钮中心）。
 *
 * 为什么要兜底：任务可能不是「本机点击」启动的（另一台设备/另一个会话），
 * 这时没有可用的点击坐标，用悬浮按钮中心至少保证水波方向是对的。
 */
export function resolveOrigin(pointer: Point | null | undefined, fallback: Point): Point {
  return isUsablePoint(pointer) ? pointer : fallback
}

/**
 * 圆心到矩形四个角的最大距离（向上取整）。
 *
 * 必须取四角最大：只算一角会让另外三个角露在圆外。取整是因为亚像素半径
 * 在部分 WebView 上会让 clip-path 的边缘发虚。
 */
export function cornerRadius(origin: Point, rect: RectLike): number {
  if (!isUsablePoint(origin)) return 0
  const xs = [rect.left, rect.left + rect.width]
  const ys = [rect.top, rect.top + rect.height]
  let max = 0
  for (const x of xs) {
    for (const y of ys) {
      const distance = Math.hypot(x - origin.x, y - origin.y)
      if (Number.isFinite(distance) && distance > max) max = distance
    }
  }
  return Number.isFinite(max) ? Math.ceil(max) : 0
}

/** 生成 clip-path 的圆形裁剪值。半径非正/非有限时退化为 0（完全不可见）。 */
export function circleClip(origin: Point, radiusPx: number): string {
  const radius = Number.isFinite(radiusPx) && radiusPx > 0 ? Math.ceil(radiusPx) : 0
  const x = Number.isFinite(origin.x) ? Math.round(origin.x) : 0
  const y = Number.isFinite(origin.y) ? Math.round(origin.y) : 0
  return `circle(${radius}px at ${x}px ${y}px)`
}

/** 一次扩散动画的首尾裁剪值。 */
export interface RevealFrames {
  from: string
  to: string
}

/** 展开：从按钮那一点（半径 0）扩到盖住整个面板。 */
export function openFrames(origin: Point, rect: RectLike): RevealFrames {
  return {
    from: circleClip(origin, 0),
    to: circleClip(origin, cornerRadius(origin, rect)),
  }
}

/** `window.matchMedia` 的最小可用形状（注入以便测试）。 */
export type MatchMediaLike = (query: string) => { matches: boolean } | null | undefined

/**
 * 是否要求「减弱动态效果」。
 *
 * 注入式：node --test 里没有 matchMedia，注入假实现就能覆盖三条分支
 * （不支持 / 命中 / 未命中），而不是只能靠人肉在系统设置里切换验证。
 */
export function prefersReducedMotion(matchMedia?: MatchMediaLike): boolean {
  const query =
    matchMedia ??
    (typeof window === 'undefined' ? undefined : (q: string) => window.matchMedia(q))
  if (!query) return false
  try {
    return Boolean(query('(prefers-reduced-motion: reduce)')?.matches)
  } catch {
    return false
  }
}

/** 环境是否支持 Web Animations API（老 WebView 上退化为「直接落终态」）。 */
export function canAnimate(element?: { animate?: unknown } | null): boolean {
  return typeof element?.animate === 'function'
}

/**
 * 量到的多个操作栏高度里选一个用。
 *
 * 三个页签常驻挂载、非当前页签是 `display:none`（高度 0），所以取**最大**的
 * 那个可见值；一个都没量到（云文档页签本来就没有操作栏）时退回兜底值，
 * 避免悬浮按钮贴到屏幕最底部。
 */
export function pickDockHeight(heights: number[], fallback: number = FAB.fallbackDockPx): number {
  const visible = heights.filter((height) => Number.isFinite(height) && height > 0)
  return visible.length > 0 ? Math.max(...visible) : fallback
}
