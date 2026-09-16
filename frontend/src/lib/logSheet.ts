/**
 * 手机端日志「底部抽屉」的几何与吸附逻辑（纯函数，无 React/DOM 依赖）。
 *
 * 为什么单独抽出来：抽屉的高度计算和松手吸附是这个交互里**唯一会算错的部分**
 * （拖拽方向反了、吸附到越界值、按像素算导致不同屏高表现不一致）。
 * 抽成纯函数后可以用 `node --test` 直接覆盖，不依赖 jsdom。
 *
 * 约定：所有高度都用**视口高度的比例**（0~1）表示，而不是像素 ——
 * 手机屏高差异很大（还带地址栏伸缩），比例能保证任何设备上「大半屏」都是大半屏。
 */

/** 抽屉的三个停靠位（比例）。 */
export interface SheetConfig {
  /** 收起：只露出把手与标题行，日志区不可见。 */
  peek: number
  /** 展开：点「日志」后的默认高度，「占满大半屏」。 */
  half: number
  /** 全屏：向上拖到顶，几乎占满。 */
  full: number
}

export const LOG_SHEET: SheetConfig = {
  peek: 0.036,
  half: 0.62,
  full: 0.9,
}

/** 至少露出这么多像素，保证把手一定可见可点（比例在不同屏高下会差很多）。 */
const MIN_PEEK_PX = 26

/**
 * 把比例夹到合法区间，并按视口高度保证收起时露出足够像素。
 *
 * `viewportPx <= 0`（SSR、测试里没量到）时只做比例夹紧。
 */
export function clampFraction(
  fraction: number,
  viewportPx = 0,
  config: SheetConfig = LOG_SHEET,
): number {
  if (!Number.isFinite(fraction)) return config.half
  let value = Math.min(config.full, Math.max(0, fraction))
  if (viewportPx > 0) {
    const minFraction = Math.max(config.peek, MIN_PEEK_PX / viewportPx)
    value = Math.max(value, Math.min(minFraction, config.full))
  }
  return value
}

/** 当前是否处于「收起」状态（日志区不可见）。 */
export function isCollapsed(
  fraction: number,
  viewportPx = 0,
  config: SheetConfig = LOG_SHEET,
): boolean {
  return fraction <= clampFraction(config.peek, viewportPx, config) + 1e-6
}

/**
 * 松手后吸附到哪个停靠位：取距离最近的。
 *
 * 用几何中点做分界，而不是「拖过一半」—— 三个停靠位间距不同时，
 * 按最近点判断才是手感最一致的（拖到哪儿最近就停在哪儿）。
 */
export function snapFraction(
  fraction: number,
  viewportPx = 0,
  config: SheetConfig = LOG_SHEET,
): number {
  const value = clampFraction(fraction, viewportPx, config)
  const stops = [config.peek, config.half, config.full]
  let best = stops[0]
  let bestDistance = Number.POSITIVE_INFINITY
  for (const stop of stops) {
    const distance = Math.abs(value - stop)
    if (distance < bestDistance) {
      bestDistance = distance
      best = stop
    }
  }
  return clampFraction(best, viewportPx, config)
}

/**
 * 把一次竖向拖拽位移换算成新的高度比例。
 *
 * 关键：抽屉是**往上长**的，所以手指向上移动（clientY 变小）高度要**变大**，
 * 因此用 `startY - currentY` 而不是反过来 —— 方向写反会导致往上拖反而缩回。
 *
 * @param startFraction 按下时的比例
 * @param startY        按下时的 clientY
 * @param currentY      当前 clientY
 * @param viewportPx    视口高度（像素），用于把位移换算成比例
 */
export function dragToFraction(
  startFraction: number,
  startY: number,
  currentY: number,
  viewportPx: number,
  config: SheetConfig = LOG_SHEET,
): number {
  if (!(viewportPx > 0)) return clampFraction(startFraction, viewportPx, config)
  const deltaFraction = (startY - currentY) / viewportPx
  return clampFraction(startFraction + deltaFraction, viewportPx, config)
}

/** 上一次展开的高度记忆（存 localStorage，刷新后保持）。 */
export const SHEET_STORAGE_KEY = 'yikou.phone.logsheet.v1'

/** 读出记忆的展开高度；无效或缺失时回退到默认的「大半屏」。 */
export function readStoredHalf(raw: string | null): number {
  if (!raw) return LOG_SHEET.half
  const value = Number.parseFloat(raw)
  if (!Number.isFinite(value)) return LOG_SHEET.half
  // 只接受合法区间内的值，避免存进离谱数字后抽屉高度异常
  if (value < LOG_SHEET.peek || value > LOG_SHEET.full) return LOG_SHEET.half
  return value
}
