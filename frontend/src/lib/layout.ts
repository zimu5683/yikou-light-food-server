/** 视口布局分类（纯函数，便于在 Node 测试里锁死 360/390/640/1024/1440 关键档位）。 */
export type AppLayout = 'phone' | 'tablet' | 'desktop'

export const PHONE_MAX = 640
export const DESKTOP_MIN = 1024

export function layoutForWidth(width: number): AppLayout {
  const safe = Number.isFinite(width) ? width : 0
  if (safe < PHONE_MAX) return 'phone'
  if (safe < DESKTOP_MIN) return 'tablet'
  return 'desktop'
}

export function isSoftKeyboardSafeHeight(viewportHeight: number, visualHeight?: number): boolean {
  if (!Number.isFinite(viewportHeight) || viewportHeight <= 0) return false
  if (!Number.isFinite(visualHeight ?? NaN)) return true
  const visible = Number(visualHeight)
  // 软键盘弹出时 visualViewport 高度会明显小于 layout viewport；只要底部操作条在文档流中滚动即可。
  return visible > 0 && visible <= viewportHeight
}
