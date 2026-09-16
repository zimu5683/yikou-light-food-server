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
} from 'react'
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
import { useApp } from '@/hooks/appContext'
import { cn } from '@/lib/utils'

/** 分栏比例的上下限（与旧版一致，含持久化夹紧）。 */
const RATIO_MIN = 0.3
const RATIO_MAX = 0.55
/** 手机单屏模式的上限。 */
const PHONE_MAX = 640
/** 桌面双栏的下限；窄于此改上下堆叠（与 TaskPanel 的 `lg:` 断点一致）。 */
const DESKTOP_MIN = 1024

type Layout = 'phone' | 'tablet' | 'desktop'

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
  const [ratio, setRatio] = useState(config?.split_ratio ?? 0.38)
  // 手机端日志是底部抽屉；App 只需要知道「现在是不是展开着」，
  // 具体高度由 PhoneLogSheet 自己管（并记忆上次拖到的位置）。
  const [logOpen, setLogOpen] = useState(false)
  const containerRef = useRef<HTMLDivElement>(null)
  const dragging = useRef(false)

  // 手机上按下「开始处理」后自动弹出日志抽屉：这个工具的流程就是
  // 配置 → 启动 → 盯日志 → 收结果，启动那一刻要看的正是日志。
  const wasRunning = useRef(false)
  useEffect(() => {
    if (layout === 'phone' && workerAlive && !wasRunning.current) setLogOpen(true)
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
            {/* 任务面板常驻：日志现在是它上面的抽屉，不再是与它并列的一屏。
                常驻也避免了「切走再切回」把表单里未落盘的输入清掉。 */}
            <div className="flex min-h-0 flex-1">
              <TaskPanel />
            </div>
          </main>
          {/* 底部 tab 栏由 LogConsole 内部与抽屉同列渲染：两者必须做兄弟节点，
              否则抽屉的把手会被 tab 栏压住（见 LogConsole 的 PhoneLogTabBar 注释）。 */}
          <LogConsole
            layout="phone"
            open={logOpen}
            onOpenChange={setLogOpen}
            status={status}
            running={workerAlive}
          />
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

