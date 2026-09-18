/**
 * 手机端日志悬浮按钮（FAB）—— 运行日志的唯一入口。
 *
 * 行为：
 * - **固定在右上角**，始终可见：收起时也不消失，全屏日志展开后仍浮在面板上方；
 * - 点一下 → 水波从这颗按钮的圆心扩散到全屏日志；
 * - 再点一下 → 反向水波从这颗按钮收回，回到原来的任务界面；
 * - 运行中时右上角一颗呼吸 LED，与日志头部徽章同一个动效。
 *
 * 层级 z-40：高于全屏日志面板(z-30)，低于对话框与 Toast(z-50)，
 * 保证全屏日志时仍能点它收起，弹窗出现时也不会被按钮压住。
 */
import { ScrollText } from 'lucide-react'

import { FAB } from '@/lib/reveal'
import { cn } from '@/lib/utils'

export function LogFab({
  open,
  running,
  onToggle,
}: {
  open: boolean
  /** 任务运行中（LED 呼吸）。 */
  running: boolean
  onToggle: () => void
}) {
  return (
    <button
      type="button"
      aria-label={open ? '收起运行日志' : '展开运行日志'}
      aria-expanded={open}
      aria-controls="phone-log-sheet"
      onClick={onToggle}
      style={{
        top: 'var(--safe-top)',
        right: 'calc(var(--safe-right) + 12px)',
        width: FAB.sizePx,
        height: FAB.sizePx,
      }}
      className={cn(
        'fixed z-40 flex touch-manipulation items-center justify-center rounded-lg border bg-card',
        'shadow-[0_6px_14px_-10px_var(--receipt-edge)]',
        'transition-[transform,color,border-color] duration-150 ease-out active:scale-95',
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
