/**
 * 手机端日志「水波扩散」的几何与降级判定（纯函数，无 React/DOM 依赖）。
 *
 * 为什么单独抽出来：圆形扩散**算错不会报错**，只会表现为「水波没盖住面板，
 * 角落露出空白」或「半径太小，日志内容被切掉」。抽成纯函数后可以用
 * `node --test` 直接覆盖，不依赖 jsdom。
 *
 * ## 这个圆是怎么来的
 *
 * 圆心是**触发按钮的中心**（悬浮按钮 / 开始处理 / 确认上传），半径是圆心到
 * 面板矩形**四个角的最大距离** —— 只取到某一角的距离，另外三个角就会露在
 * 圆外（表现为扩散结束时面板缺一角）。这与 react-circular-reveal、
 * react-theme-switch-animation 内部用的是同一个套路，区别只是我们不做
 * View Transitions 快照（那会让流式日志在动画期间停止刷新）。
 *
 * ## 起始半径 0
 *
 * 收起状态下面板不存在，展开时面板直接铺满整个视口，只由 `clip-path` 的
 * 圆形半径从 0 扩到「盖住四角」的最大值。没有高度过渡，也没有自下而上的
 * 翻滚；`start` 半径取 0 是 Material 圆形揭示的标准做法：水波从按钮那一点
 * 涌出，逐渐扫过整个屏幕。
 *
 * ## 收起做反向水波
 *
 * 收起时面板保持全屏尺寸，由 `circleClip` 从「盖住四角的最大半径」缩回
 * 按钮圆心的半径 0，露出下面的任务界面；日志按钮始终固定在上层，点击它
 * 即可开合。
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

/** 扩散/收回动画的时长与缓动。 */
export const REVEAL_TIMING = {
  // 两端慢、中间快的缓动，让大面积 clip-path 每帧变化更均匀，减少跳帧感。
  openMs: 480,
  openEase: 'cubic-bezier(0.4, 0, 0.2, 1)',
  /** 收回稍快，反向水波不拖手。 */
  closeMs: 320,
  closeEase: 'cubic-bezier(0.4, 0, 0.2, 1)',
} as const

/** 右上角日志按钮的尺寸与落点。 */
export const FAB = {
  // 40px = 标题栏内容高度，多 1px 都会溢出标题栏边界。
  sizePx: 40,
  /** 距屏幕右缘（在安全区之外，调用方会再叠加 --safe-right）。 */
  rightPx: 12,
  /** 量不到按钮位置时的兜底顶边距（正常由 ref 实测中心）。 */
  fallbackTopPx: 0,
  /**
   * 让位后与相邻内容保留的呼吸间隙。
   *
   * 按钮是 `fixed` 的，不占布局；标题栏/日志头部的右侧文字与筛选控件若不让位，
   * 就会被它压住（点不到、看不清）。间隙取 8px，正好在触控目标之间留出视觉分隔。
   */
  clearanceGapPx: 8,
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

/** 收回：从盖住整个面板的圆缩回按钮那一点。 */
export function closeFrames(origin: Point, rect: RectLike): RevealFrames {
  const opened = openFrames(origin, rect)
  return { from: opened.to, to: opened.from }
}

/**
 * 纯 CSS 圆心表达式：固定在右上角日志按钮中心。
 *
 * 不再由 JS 测量 `getBoundingClientRect()`，而是直接使用与 LogFab 相同的
 * CSS 布局规则（right = safe-right + 12px，宽 40px；top = safe-top，高 40px）。
 * 这样圆心由浏览器布局引擎决定，彻底避免 JS 测量/坐标系差异。
 */
export const LOG_REVEAL_ORIGIN =
  `calc(100% - var(--safe-right) - ${FAB.rightPx + FAB.sizePx / 2}px) ` +
  `calc(var(--safe-top) + ${FAB.sizePx / 2}px)`

/**
 * 给右上角日志按钮让出的右侧空间（CSS 表达式，含 8px 呼吸间隙）。
 *
 * 与 `LOG_REVEAL_ORIGIN` 同源：都从 `FAB.rightPx / FAB.sizePx` 推导，改按钮尺寸时
 * 圆心与让位一起变，不会出现「按钮挪了、标题栏文字或日志筛选控件还被压着」。
 * 用法：`padding-right: var(--fab-clearance)`（未定义时调用方给 0px 兜底）。
 */
export const FAB_CLEARANCE =
  `calc(var(--safe-right) + ${FAB.rightPx + FAB.sizePx + FAB.clearanceGapPx}px)`

/** 屏幕对角线最大值是 141.42vmax；142vmax 刚好盖满全屏，不做过多的无效扩大。 */
export const LOG_REVEAL_RADIUS = '142vmax'

/** 展开：半径 0 → LOG_REVEAL_RADIUS（142vmax），圆心始终固定在右上角日志按钮中心。 */
export function openFramesCss(): RevealFrames {
  return {
    from: circleClipCss('0px'),
    to: circleClipCss(LOG_REVEAL_RADIUS),
  }
}

/** 收回：半径 LOG_REVEAL_RADIUS（142vmax）→ 0，圆心不变。 */
export function closeFramesCss(): RevealFrames {
  const opened = openFramesCss()
  return { from: opened.to, to: opened.from }
}

function circleClipCss(radius: string): string {
  return `circle(${radius} at ${LOG_REVEAL_ORIGIN})`
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
