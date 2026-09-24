/**
 * 应用根组件：手机 APK 优先的工作台布局。
 *
 * - phone：任务面板 + 底栏主动作，日志由右上角悬浮按钮（LogFab）展开为全屏层；
 * - tablet/desktop：任务/日志左右或上下分栏，保留可拖拽比例；
 * - 顶栏固定显示任务类型、权威运行状态与安全模式；
 * - 全局请求错误统一呈现“哪里失败 + 下一步”，断线只标记连接，不篡改任务状态。
 */
import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type PointerEvent as ReactPointerEvent,
} from 'react'
import { AlertTriangle, X } from 'lucide-react'
import { Toaster } from '@/components/ui/sonner'
import { Button } from '@/components/ui/button'
import {
  CaptchaDialog,
  DecisionDialog,
  UpdateAvailableDialog,
} from '@/components/dialogs'
import { LogConsole } from '@/components/LogConsole'
import { LogFab } from '@/components/LogFab'
import { TaskPanel } from '@/components/TaskPanel'
import { TitleBar } from '@/components/TitleBar'
import { useApp } from '@/hooks/appContext'
import { interactionRecoveryView, interactionKindLabel } from '@/lib/interactionRecovery'
import { useLogReveal } from '@/lib/useLogReveal'
import { layoutForWidth } from '@/lib/layout'
import { FAB_CLEARANCE } from '@/lib/reveal'
import { cn } from '@/lib/utils'

const RATIO_MIN = 0.3
const RATIO_MAX = 0.55

type Layout = 'phone' | 'tablet' | 'desktop'

function readLayout(): Layout {
  return layoutForWidth(window.innerWidth)
}

function useLayout(): Layout {
  const [layout, setLayout] = useState<Layout>(readLayout)
  useEffect(() => {
    const sync = () => setLayout(readLayout())
    window.addEventListener('resize', sync)
    window.addEventListener('orientationchange', sync)
    return () => {
      window.removeEventListener('resize', sync)
      window.removeEventListener('orientationchange', sync)
    }
  }, [])
  return layout
}

export default function App() {
  const { config, setSplitRatio, authError, workerAlive } = useApp()
  const layout = useLayout()
  const [ratio, setRatio] = useState(config?.split_ratio ?? 0.38)
  const [keyboardInset, setKeyboardInset] = useState(0)
  const reveal = useLogReveal()
  const containerRef = useRef<HTMLDivElement>(null)
  const dragging = useRef(false)
  const noticeRef = useRef<HTMLDivElement>(null)
  /** 手机端提示区域实测高度 + 其底边在视口里的位置（无提示时都为 0）。 */
  const [notice, setNotice] = useState({ height: 0, bottom: 0 })

  // 软键盘安全区：visualViewport 变小（adjustResize 失败）时把底栏抬到键盘之上。
  useEffect(() => {
    const viewport = window.visualViewport
    if (!viewport) return
    const sync = () => {
      const inset = Math.max(0, window.innerHeight - viewport.height - viewport.offsetTop)
      setKeyboardInset(Number.isFinite(inset) ? Math.round(inset) : 0)
    }
    sync()
    viewport.addEventListener('resize', sync)
    viewport.addEventListener('scroll', sync)
    return () => {
      viewport.removeEventListener('resize', sync)
      viewport.removeEventListener('scroll', sync)
    }
  }, [])

  const wasRunning = useRef(false)
  const openRemembered = reveal.openRemembered
  useEffect(() => {
    if (layout === 'phone' && workerAlive && !wasRunning.current) openRemembered()
    wasRunning.current = workerAlive
  }, [workerAlive, layout, openRemembered])

  /**
   * 把手机端提示区域的**真实高度**同步成 CSS 变量，供全屏日志层让位。
   *
   * 提示区域是正常布局的一部分（见 index.css 的 `.phone-notice-region`），
   * 但全屏日志层是 `fixed inset-0`：日志展开时它覆盖整屏，所以日志内容必须按
   * 提示区域的高度整体下移，提示条才不会盖住日志头部（状态/搜索/工具）。
   * 用 ResizeObserver 实测而不是固定像素：长提示、多条提示、字体放大、
   * 横竖屏切换时高度都会变，写死数字迟早对不上。
   */
  useEffect(() => {
    const element = noticeRef.current
    if (layout !== 'phone' || !element) {
      setNotice((current) => (current.height === 0 && current.bottom === 0 ? current : { height: 0, bottom: 0 }))
      return
    }
    const sync = () => {
      const rect = element.getBoundingClientRect()
      const height = Math.round(rect.height)
      // 无提示时区域高度为 0：此时不参与让位，布局与改动前完全一致。
      const bottom = height > 0 ? Math.round(rect.bottom) : 0
      setNotice((current) => (current.height === height && current.bottom === bottom ? current : { height, bottom }))
    }
    sync()
    if (typeof ResizeObserver === 'undefined') return
    const observer = new ResizeObserver(sync)
    observer.observe(element)
    return () => observer.disconnect()
  }, [layout])

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
    <div
      className="flex h-full min-h-0 flex-col overflow-hidden"
      style={{
        '--keyboard-inset': `${keyboardInset}px`,
        // 手机端右上角常驻日志按钮：标题栏与日志头部据此在右侧让位，
        // 避免 fixed 按钮压住主题切换、日志标题/筛选控件。
        ...(layout === 'phone' ? { '--fab-clearance': FAB_CLEARANCE } : {}),
        // 手机端提示区域实测高度与底边：全屏日志层据此整体下移（见下面的 effect）。
        '--phone-notice-height': `${layout === 'phone' ? notice.height : 0}px`,
        '--phone-notice-bottom': `${layout === 'phone' ? notice.bottom : 0}px`,
      } as React.CSSProperties}
    >
      <TitleBar />
      <AuthBanner message={authError} />
      <GlobalRequestError />
      <PendingInteractionRecoveryBanner />

      {layout === 'phone' ? (
        <>
          {/* 手机端提示区域：提示条在这里**占正常布局空间**（不是浮层），
              下面的任务面板整体下移 —— 标题栏状态、右上角日志按钮、底栏动作、
              待处理输入与弹窗都不可能被它盖住（弹窗 z-50 仍在区域 z-40 之上）。
              上限有两条：`45vh`（常规视口，占屏不超过 45%）与
              `calc(100vh - 15rem)`（极短视口的安全线：15rem ≈ 标题栏 + 任务面板
              固定 chrome 的实测上界，rem 随系统字体缩放一起长）。两者取小，
              配合 `overflow-y-auto`：空间不够时提示改为**区域内滚动**，
              而不会把底栏「开始/停止」挤出屏幕（横屏 320 高 + 1.25× 字体实测）。 */}
          <div
            ref={noticeRef}
            data-phone-notice-region="true"
            className="phone-notice-region relative z-40 min-h-0 max-h-[min(45vh,calc(100vh-15rem))] overflow-y-auto"
          >
            <Toaster
              position="top-center"
              expand
              className="toaster phone-notice-toaster"
              richColors
              closeButton
            />
          </div>
          <main className="flex min-h-0 flex-1 flex-col">
            <div className="flex min-h-0 flex-1">
              <TaskPanel />
            </div>
          </main>
          <LogConsole layout="phone" reveal={reveal} />
          {/* 日志的唯一入口：始终浮在右上角、全屏日志层(z-30)之上，
              点它展开、再点同一个按钮收回。running 用权威运行状态点亮呼吸 LED。 */}
          <LogFab
            open={reveal.open}
            running={workerAlive}
            onToggle={() => reveal.toggleFrom(null)}
          />
        </>
      ) : (
        <main
          ref={containerRef}
          className={cn('flex min-h-0 flex-1', layout === 'tablet' ? 'flex-col' : 'flex-row')}
          style={{ paddingBottom: 'var(--safe-bottom)' }}
        >
          <div
            className={cn(
              'flex min-h-0 flex-col',
              layout === 'tablet' ? 'max-h-[58%] flex-none border-b' : 'shrink-0 border-r',
            )}
            style={layout === 'desktop' ? { width: `${Math.round(ratio * 100)}%`, minWidth: 360 } : undefined}
          >
            <TaskPanel />
          </div>
          {layout === 'desktop' && (
            <div
              role="separator"
              aria-orientation="vertical"
              aria-label="调整任务与日志宽度"
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

      <DecisionDialog />
      <CaptchaDialog />
      <UpdateAvailableDialog />
      {/* 桌面/平板：sonner 默认的右下角浮层，行为与改动前完全一致。
          手机端的提示区域在上面（占布局空间），两者互斥渲染。 */}
      {layout !== 'phone' && (
        <Toaster position="bottom-right" richColors closeButton />
      )}
    </div>
  )
}

function PendingInteractionRecoveryBanner() {
  const {
    decision, captcha, addressInput, pendingRecoveryMeta, dismissPendingRecovery,
    redactedPendingCount, connection, recovery, operationActive, reconnect, stopTask,
  } = useApp()
  if (redactedPendingCount > 0 && !decision && !captcha && !addressInput) {
    return (
      <div role="status" className="shrink-0 border-b border-warning/50 bg-warning/10 px-3 py-2 sm:px-4">
        <div className="flex items-start gap-2">
          <AlertTriangle className="mt-0.5 size-4 shrink-0 text-warning" />
          <div className="min-w-0 flex-1">
            <p className="text-xs font-medium text-foreground">有 {redactedPendingCount} 个待处理交互属于其他账号</p>
            <p className="mt-0.5 break-words text-[11px] text-muted-foreground">
              当前账号只能查看元数据，不能打开验证码/地址/决策输入，也不会自动提交；请让发起账号处理或等待任务结束。
            </p>
          </div>
        </div>
      </div>
    )
  }
  if (decision || captcha || addressInput || !pendingRecoveryMeta) return null
  const view = interactionRecoveryView({
    hasLocalRequest: false,
    hasPersistedMeta: true,
    connection,
    recovery,
    operationActive,
    hasServerReadApi: true,
  })
  if (!view.visible) return null
  return (
    <div role="alert" className="shrink-0 border-b border-warning/50 bg-warning/10 px-3 py-2 sm:px-4">
      <div className="flex items-start gap-2">
        <AlertTriangle className="mt-0.5 size-4 shrink-0 text-warning" />
        <div className="min-w-0 flex-1">
          <p className="text-xs font-medium text-foreground">{view.title}</p>
          <p className="mt-0.5 break-words text-[11px] leading-relaxed text-muted-foreground">
            类型：{interactionKindLabel(pendingRecoveryMeta.kind)}
            {pendingRecoveryMeta.operationId ? ` · operation_id：${pendingRecoveryMeta.operationId}` : ''}
            {view.detail ? ` ${view.detail}` : ''}
          </p>
        </div>
        <div className="flex shrink-0 flex-wrap items-center justify-end gap-1">
          <Button variant="outline" size="sm" className="h-8 rounded-[6px] text-[11px]" onClick={() => void reconnect()}>
            重新查询状态
          </Button>
          {view.canStop && (
            <Button variant="outline" size="sm" className="h-8 rounded-[6px] text-[11px]" onClick={() => void stopTask()}>
              安全停止任务
            </Button>
          )}
          <Button variant="ghost" size="sm" className="h-8 rounded-[6px] text-[11px]" onClick={dismissPendingRecovery}>
            知道了
          </Button>
        </div>
      </div>
    </div>
  )
}

function AuthBanner({ message }: { message: string }) {
  if (!message) return null
  return (
    <div role="alert" className="shrink-0 border-b border-destructive/40 bg-destructive/10 px-3 py-2 text-xs text-destructive sm:px-4">
      {message}
    </div>
  )
}

function GlobalRequestError() {
  const { requestIssues, dismissRequestIssue, reconnect } = useApp()
  const issue = requestIssues[requestIssues.length - 1]
  if (!issue) return null
  const retry = issue.kind === 'offline' || issue.kind === 'timeout'
  return (
    <div role="alert" className="shrink-0 border-b border-warning/50 bg-warning/10 px-3 py-2 sm:px-4">
      <div className="flex items-start gap-2">
        <AlertTriangle className="mt-0.5 size-4 shrink-0 text-warning" />
        <div className="min-w-0 flex-1">
          <p className="text-xs font-medium text-foreground">{issue.title}</p>
          <p className="mt-0.5 break-words text-[11px] text-muted-foreground">
            {issue.detail} {issue.nextStep}
          </p>
        </div>
        <div className="flex shrink-0 items-center gap-1">
          {retry && (
            <Button variant="outline" size="sm" className="h-8 rounded-[6px] text-[11px]" onClick={() => void reconnect()}>
              重新连接
            </Button>
          )}
          <Button variant="ghost" size="sm" className="h-8 w-8 p-0" aria-label="关闭错误提示" onClick={() => dismissRequestIssue(issue.id)}>
            <X className="size-4" />
          </Button>
        </div>
      </div>
    </div>
  )
}
