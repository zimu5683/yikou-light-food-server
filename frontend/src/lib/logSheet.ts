/**
 * 手机端日志「底部抽屉」的几何与拖拽逻辑（纯函数，无 React/DOM 依赖）。
 *
 * 为什么单独抽出来：高度计算和拖拽换算是这个交互里**唯一会算错的部分**
 * （拖拽方向反了、越界、顶部留白算漏导致把手跑到屏幕外）。抽成纯函数后
 * 可以用 `node --test` 直接覆盖，不依赖 jsdom。
 *
 * ## 为什么用像素而不是 vh
 *
 * 之前用 `vh` 表示高度：手机上 `100vh` 与 `window.innerHeight` **不相等**
 * （`vh` 取的是不含地址栏的最大视口，`innerHeight` 是当前实际高度），
 * 而拖拽换算用的是 `innerHeight` —— 两套单位混用会让「拖到一半时看不到内容」、
 * 「拖到最大时把手被顶出屏幕」。现在统一用像素，拖多少就是多少。
 */

/** 像素制的三个关键高度。 */
export interface SheetMetrics {
  /** 收起时的高度：刚好露出把手。 */
  peekPx: number
  /** 顶部留白：拖到最大也不铺满全屏，既露出下巴（可拖回来）也保留把手。 */
  topGapPx: number
}

export const LOG_SHEET: SheetMetrics = {
  peekPx: 34,
  topGapPx: 28,
}

/** 把手本身的高度（与组件里的 `h-7` 对应）。 */
export const HANDLE_PX = 28

/** 拖到这个像素阈值以上才认为「是拖动」，低于它算点击（避免误触收展）。 */
export const DRAG_SLOP_PX = 6

/** 视口高度无效（未测量/异常）时的兜底值。 */
const FALLBACK_VIEWPORT_PX = 800

function safeViewport(viewportPx: number): number {
  return Number.isFinite(viewportPx) && viewportPx > 0 ? viewportPx : FALLBACK_VIEWPORT_PX
}

/** 抽屉允许的最大高度：视口高度减去顶部留白。 */
export function maxHeight(viewportPx: number, metrics: SheetMetrics = LOG_SHEET): number {
  const viewport = safeViewport(viewportPx)
  return Math.max(metrics.peekPx, viewport - metrics.topGapPx)
}

/** 把任意高度夹到 [peekPx, maxHeight] 区间。 */
export function clampHeight(
  heightPx: number,
  viewportPx: number,
  metrics: SheetMetrics = LOG_SHEET,
): number {
  if (!Number.isFinite(heightPx)) return metrics.peekPx
  return Math.min(maxHeight(viewportPx, metrics), Math.max(metrics.peekPx, heightPx))
}

/** 是否处于收起状态（容 1px 误差）。 */
export function isCollapsed(heightPx: number, metrics: SheetMetrics = LOG_SHEET): boolean {
  return heightPx <= metrics.peekPx + 1
}

/**
 * 拖拽 API：抽屉把它交给操作栏里的「日志」按钮，让那个按钮也能当拖拽把手用。
 *
 * 只依赖 `clientY`，所以这里不必引入 React 的类型。
 */
export interface SheetDragApi {
  onDown: (event: { clientY: number }) => void
  onMove: (event: { clientY: number }) => void
  onUp: () => void
}

/**
 * 把一次竖向拖拽换算成新的抽屉高度（像素）。
 *
 * 抽屉**往上长**：手指向上移动（clientY 变小）高度要变大，
 * 所以是 `startHeight + (startY - currentY)`。方向写反会「往上拖反而缩回」。
 */
export function dragToHeight(
  startHeightPx: number,
  startY: number,
  currentY: number,
  viewportPx: number,
  metrics: SheetMetrics = LOG_SHEET,
): number {
  return clampHeight(startHeightPx + (startY - currentY), viewportPx, metrics)
}

/** 手柄/遮罩点击与拖动的区分：位移超过阈值就不当作点击。 */
export function isClick(movedPx: number): boolean {
  return Math.abs(movedPx) < DRAG_SLOP_PX
}

/**
 * 松手时是否应该收起。
 *
 * 只要没拖到比「一个把手 + 一点余量」更高（也就是用户其实是想关掉），就收起；
 * 其余高度**原样保留** —— 用户要的是「随意调到任何位置」，不做任何吸附。
 */
export function shouldCollapse(
  heightPx: number,
  metrics: SheetMetrics = LOG_SHEET,
): boolean {
  return heightPx < metrics.peekPx + HANDLE_PX / 2
}

/** 上一次展开的高度记忆（存 localStorage，刷新后保持）。 */
export const SHEET_STORAGE_KEY = 'yikou.phone.logsheet.v2'

/** 读出记忆的展开高度（像素）；无效、越界或缺失时回退到默认的大半屏。 */
export function readStoredHeight(raw: string | null, viewportPx: number): number {
  const fallback = Math.round(maxHeight(viewportPx) * 0.62)
  if (!raw) return fallback
  const value = Number.parseFloat(raw)
  if (!Number.isFinite(value)) return fallback
  return clampHeight(value, viewportPx)
}
