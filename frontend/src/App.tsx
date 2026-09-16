/**
 * 应用根组件（网页版）。
 *
 * 三种布局，按视口宽度切换：
 * - **phone**（<640px）：一次只占一屏，底部 tab 在「任务 / 日志」间切换。
 *   手机上左右分栏或上下堆叠都会让两块内容互相挤，各给一整屏才够用。
 * - **tablet**（640–1023px）：任务在上、日志在下，上下堆叠。
 * - **desktop**（≥1024px）：左右分栏，分隔条可拖拽，比例持久化到 AppConfig。
 *
 * 桌面端（pywebview 原生窗口）已不是目标，因此没有窗口按钮、拖拽区与
 * 「无边框窗口」那套版式；标题栏退化为普通网页页头。
 */
import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type PointerEvent as ReactPointerEvent,
  type ReactNode,
} from 'react'
import { ClipboardList, ScrollText } from 'lucide-react'
import { Toaster } from '@/components/ui/sonner'
import {
  CaptchaDialog,
  DecisionDialog,
  UpdateAvailableDialog,
  UpdateProgressDialog,
} from '@/components/dialogs'
import { LogConsole } from '@/components/LogConsole'
import { TaskPanel } from '@/components/TaskPanel'
import { TitleBar } from '@/components/TitleBar'
import { statusLabel, useApp } from '@/hooks/appContext'
import type { StatusState } from '@/lib/bridge'
import { cn } from '@/lib/utils'

/** 分栏比例的上下限（与旧版一致，含持久化夹紧）。 */
const RATIO_MIN = 0.3
const RATIO_MAX = 0.55
/** 手机单屏模式的上限。 */
const PHONE_MAX = 640
/** 桌面双栏的下限；窄于此改上下堆叠（与 TaskPanel 的 `lg:` 断点一致）。 */
const DESKTOP_MIN = 1024

type Layout = 'phone' | 'tablet' | 'desktop'
type PhoneView = 'task' | 'log'

function readLayout(): Layout {
  const width = window.innerWidth
  if (width < PHONE_MAX) return 'phone'
  if (width < DESKTOP_MIN) return 'tablet'
  return 'desktop'
}

function useLayout(): Layout {
  const [layout, setLayout] = useState<Layout>(readLayout)
  useEffect(() => {
    const sync = () => setLayout(readLayout())
    window.addEventListener('resize', sync)
    // 手机旋屏时 resize 可能晚于布局变化，补一次 orientationchange
    window.addEventListener('orientationchange', sync)
    return () => {
      window.removeEventListener('resize', sync)
      window.removeEventListener('orientationchange', sync)
    }
  }, [])
  return layout
}

export default function App() {
  const { config, setSplitRatio, authError, status, workerAlive } = useApp()
  const layout = useLayout()
  const [view, setView] = useState<PhoneView>('task')
  const [ratio, setRatio] = useState(config?.split_ratio ?? 0.38)
  const containerRef = useRef<HTMLDivElement>(null)
  const dragging = useRef(false)

  // 手机上按下「开始处理」后自动切到日志屏：这个工具的流程就是
  // 配置 → 启动 → 盯日志 → 收结果，启动那一刻要看的正是日志。
  const wasRunning = useRef(false)
  useEffect(() => {
    if (layout === 'phone' && workerAlive && !wasRunning.current) setView('log')
    wasRunning.current = workerAlive
  }, [workerAlive, layout])

  const onDividerDown = useCallback((e: ReactPointerEvent) => {
    dragging.current = true
    ;(e.target as HTMLElement).setPointerCapture(e.pointerId)
  }, [])

  const onDividerMove = useCallback((e: ReactPointerEvent) => {
    if (!dragging.current || !containerRef.current) return
    const rect = containerRef.current.getBoundingClientRect()
    const next = Math.min(RATIO_MAX, Math.max(RATIO_MIN, (e.clientX - rect.left) / rect.width))
    setRatio(next)
  }, [])

  const onDividerUp = useCallback(() => {
    if (!dragging.current) return
    dragging.current = false
    setSplitRatio(ratio)
  }, [ratio, setSplitRatio])

  return (
    <div className="flex h-full flex-col overflow-hidden">
      <TitleBar />
      {authError && (
        <div className="shrink-0 border-b border-destructive/40 bg-destructive/10 px-4 py-2 text-xs text-destructive">
          {authError}
        </div>
      )}

      {layout === 'phone' ? (
        <>
          <main className="flex min-h-0 flex-1 flex-col">
            {/* 两屏都常驻、只切可见性：卸载会清掉表单里尚未落盘的输入 */}
            <div className={cn('min-h-0 flex-1', view === 'task' ? 'flex' : 'hidden')}>
              <TaskPanel />
            </div>
            <div className={cn('min-h-0 flex-1', view === 'log' ? 'flex' : 'hidden')}>
              <LogConsole />
            </div>
          </main>
          <PhoneTabBar view={view} onView={setView} status={status} running={workerAlive} />
        </>
      ) : (
        <main
          ref={containerRef}
          className={cn('flex min-h-0 flex-1', layout === 'tablet' ? 'flex-col' : 'flex-row')}
          // 这两个布局下方没有 tab 栏，底部安全区（手势条）由 main 自己让开
          style={{ paddingBottom: 'var(--safe-bottom)' }}
        >
          <div
            className={cn(
              'flex min-h-0 flex-col',
              layout === 'tablet' ? 'max-h-[55%] flex-none border-b' : 'shrink-0 border-r',
            )}
            style={layout === 'desktop' ? { width: `${Math.round(ratio * 100)}%`, minWidth: 360 } : undefined}
          >
            <TaskPanel />
          </div>

          {layout === 'desktop' && (
            <div
              role="separator"
              aria-orientation="vertical"
              onPointerDown={onDividerDown}
              onPointerMove={onDividerMove}
              onPointerUp={onDividerUp}
              className="group relative z-10 w-1 shrink-0 cursor-col-resize bg-border transition-colors hover:bg-primary/50"
            >
              <div className="absolute inset-y-0 -left-1.5 -right-1.5" />
            </div>
          )}

          <LogConsole />
        </main>
      )}

      {/* 对话框与通知 */}
      <DecisionDialog />
      <CaptchaDialog />
      <UpdateAvailableDialog />
      <UpdateProgressDialog />
      <Toaster position="bottom-right" richColors closeButton />
    </div>
  )
}

/** 手机底部双 tab；横屏/手势条安全区在这里吃掉。 */
function PhoneTabBar({
  view,
  onView,
  status,
  running,
}: {
  view: PhoneView
  onView: (next: PhoneView) => void
  status: StatusState
  running: boolean
}) {
  return (
    <nav
      className="flex shrink-0 items-stretch border-t bg-card"
      style={{ paddingBottom: 'var(--safe-bottom)', paddingLeft: 'var(--safe-left)', paddingRight: 'var(--safe-right)' }}
    >
      <PhoneTab active={view === 'task'} onClick={() => onView('task')} icon={<ClipboardList className="size-4" />}>
        任务
      </PhoneTab>
      <PhoneTab
        active={view === 'log'}
        onClick={() => onView('log')}
        icon={<ScrollText className="size-4" />}
        // 运行中直接把状态写进 tab：不用切过去也知道跑到哪一步了
        hint={running ? statusLabel(status) : undefined}
        led={running}
      >
        日志
      </PhoneTab>
    </nav>
  )
}

function PhoneTab({
  active,
  onClick,
  icon,
  hint,
  led,
  children,
}: {
  active: boolean
  onClick: () => void
  icon: ReactNode
  hint?: string
  led?: boolean
  children: ReactNode
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-current={active ? 'page' : undefined}
      className={cn(
        // 两行（图标 + 文字）就够：原来还多出一行状态和一个下划线，整条近 66px，
        // 在手机上太占地方。状态并到文字行里。
        'flex min-h-11 flex-1 flex-col items-center justify-center gap-0.5 py-1.5 transition-colors',
        active ? 'text-primary' : 'text-muted-foreground',
      )}
    >
      <span className="flex items-center gap-1.5">
        {led && <span className="led-breathe size-[6px] rounded-[1px] bg-primary" />}
        {icon}
      </span>
      <span className="text-[11px] font-medium">
        {children}
        {hint && <span className="ml-1 text-primary">{hint}</span>}
      </span>
    </button>
  )
}
