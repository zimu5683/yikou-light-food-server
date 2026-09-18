/**
 * 手机端日志悬浮按钮（FAB）—— 运行日志的唯一入口。
 *
 * 取代了原先操作栏里的「日志」按钮（那个按钮还兼任抽屉把手，位置在左下角、
 * 且会被表单挤成很小的触控目标）。现在它：
 *
 * - **浮在底部操作栏上方**：`bottom = 日志面板高度 + 操作栏高度 + 间隙`。
 *   操作栏与面板都会随开合移动，所以按钮跟着一起上移，永远压在操作栏之上
 *   （否则会盖住「停止」），展开后则浮在日志面板上方，再点一次就能收起。
 * - **点一下 → 日志从这颗按钮的圆心像水波一样扩散**（圆心由 App 交给
 *   `useLogReveal`，这里只负责把点击事件报上去）。
 * - 运行时右上角一颗呼吸 LED，与日志头部徽章同一个动效（设计规范里
 *   「running 徽章呼吸是唯一常驻动效」）。
 *
 * 层级 z-[45]：在遮罩(30)/面板(40)之上（否则被面板盖住就收不起来），
 * 在对话框与 Toast(50) 之下（弹窗要能盖住它）。
 */
import type { Ref } from 'react'
import { ScrollText } from 'lucide-react'

import { FAB } from '@/lib/reveal'
import { cn } from '@/lib/utils'

export function LogFab({
  ref,
  open,
  running,
  bottomPx,
  onToggle,
}: {
  /** 由 App 持有：扩散圆心要读这颗按钮的位置。React 19 允许 ref 当普通 prop 传。 */
  ref?: Ref<HTMLButtonElement>
  open: boolean
  /** 任务运行中（LED 呼吸）。 */
  running: boolean
  /** 按钮下缘距屏幕底部的距离（含安全区），由 App 按面板与操作栏高度算出。 */
  bottomPx: number
  onToggle: () => void
}) {
  return (
    <button
      ref={ref}
      type="button"
      aria-label={open ? '收起运行日志' : '展开运行日志'}
      aria-expanded={open}
      aria-controls="phone-log-sheet"
      onClick={onToggle}
      style={{ bottom: bottomPx, right: FAB.rightPx, width: FAB.sizePx, height: FAB.sizePx }}
      className={cn(
        'fixed z-[45] flex touch-manipulation items-center justify-center rounded-lg border bg-card',
        // 只过渡 bottom/transform：过渡 height 之类会让按钮在旋屏时抖
        'shadow-[0_6px_14px_-10px_var(--receipt-edge)]',
        'transition-[bottom,transform] duration-200 ease-out active:scale-95',
        open ? 'border-primary/60 text-primary' : 'border-border text-muted-foreground',
      )}
    >
      <ScrollText className="size-[18px]" />
      {running && (
        <span className="led-breathe absolute right-1.5 top-1.5 size-1.5 rounded-[2px] bg-primary" />
      )}
    </button>
  )
}
