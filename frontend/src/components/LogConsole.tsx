/**
 * 运行日志控制台 = 一张正在打印的小票（Wheat Press 记忆点）：
 * 锯齿顶边 + 等宽时间戳/级别 + 虚线裁切线 + 命中价签黄高亮 + 页脚印章小字。
 * 功能：即输即滤（保留命中高亮与无命中提示）、复制、清空、自动滚动。
 */
import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { ArrowDownToLine, ClipboardList, Copy, Eraser, ScrollText } from 'lucide-react'
import { Input } from '@/components/ui/input'
import type { AddressInputRequest } from '@/lib/bridge'
import { statusLabel, useApp } from '@/hooks/appContext'
import { cn } from '@/lib/utils'
import { formatLogMsg, isOrderSummary, splitOrderSummary } from '@/lib/format'
import {
  LOG_SHEET,
  SHEET_STORAGE_KEY,
  clampFraction,
  dragToFraction,
  readStoredHalf,
  snapFraction,
} from '@/lib/logSheet'

const LEVEL_CLASS: Record<string, string> = {
  OK: 'text-success',
  INFO: 'text-muted-foreground',
  WARN: 'text-warning',
  ERROR: 'text-destructive',
}

/**
 * 状态徽章文案与「是否在跑」。
 *
 * 留在组件里而不搬进 `@/lib/format`：它依赖 `statusLabel`，而 `statusLabel` 所在的
 * `@/hooks/appContext` 用了 `@/` 别名导入，Node 的测试运行器解析不了 —— 搬过去会让
 * `format.ts` 无法被测试直接 import。真正的文案映射本来就在 appContext 里。
 */
function statusBadgeMeta(status: string): { label: string; live: boolean } {
  return { label: statusLabel(status as never), live: status === 'running' }
}

/**
 * 日志控制台入口。
 *
 * ``layout === 'phone'`` 时改用**底部抽屉**：点底部「日志」从下往上弹出占大半屏，
 * 点上方非日志区域自动退回底部，也可以拖把手自由调高度。
 * 平板/桌面维持原来的并排布局（`LogConsoleBody` 直接铺满容器）。
 */
export function LogConsole({
  layout = 'desktop',
  open,
  onOpenChange,
  status = 'ready',
  running = false,
}: {
  layout?: 'phone' | 'tablet' | 'desktop'
  /** 手机端由 App 控制的展开状态（底部「日志」按钮与遮罩都要能改它）。 */
  open?: boolean
  onOpenChange?: (next: boolean) => void
  /** 手机端底部 tab 栏要显示的状态（与抽屉同属一个底部列，所以由这里渲染）。 */
  status?: string
  running?: boolean
}) {
  if (layout !== 'phone') return <LogConsoleBody />
  return (
    <PhoneLogSheet
      open={Boolean(open)}
      onOpenChange={onOpenChange ?? (() => {})}
      tabBar={<PhoneLogTabBar open={Boolean(open)} onToggle={() => onOpenChange?.(!open)} status={status} running={running} />}
    />
  )
}

/**
 * 手机底部 tab 栏。
 *
 * 放在这个文件里（而不是 App.tsx）是因为它必须与抽屉处于**同一个固定底部列**：
 * 抽屉若单独 `fixed bottom-0`，把手会被 tab 栏压在下面。两者做兄弟节点后，
 * 无论 tab 栏多高（字体缩放在不同机型上还会变）都不会重叠。
 */
function PhoneLogTabBar({
  open,
  onToggle,
  status,
  running,
}: {
  open: boolean
  onToggle: () => void
  status: string
  running: boolean
}) {
  return (
    <nav
      className="flex shrink-0 items-stretch border-t bg-card"
      style={{ paddingBottom: 'var(--safe-bottom)', paddingLeft: 'var(--safe-left)', paddingRight: 'var(--safe-right)' }}
    >
      {/* 「任务」不是切屏而是「把日志收回去」：任务面板一直常驻在下面，
          所以点它等于回到无遮挡的任务视图，与点击上方遮罩语义一致。 */}
      <PhoneLogTab active={!open} onClick={() => open && onToggle()} icon={<ClipboardList className="size-4" />}>
        任务
      </PhoneLogTab>
      <PhoneLogTab
        active={open}
        onClick={onToggle}
        icon={<ScrollText className="size-4" />}
        hint={running ? statusLabel(status as never) : undefined}
        led={running}
      >
        日志
      </PhoneLogTab>
    </nav>
  )
}

function PhoneLogTab({
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
        // 两行（图标 + 文字）就够：多出的一行状态和一个下划线整条近 66px，手机上太占地方。
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

function LogConsoleBody() {
  const { logs, status, clearLogs, addressInput, resolveAddressInput } = useApp()
  const [filter, setFilter] = useState('')
  const [autoscroll, setAutoscroll] = useState(true)
  const paperRef = useRef<HTMLDivElement>(null)

  const query = filter.trim().toLowerCase()
  const filtered = useMemo(
    () => (query ? logs.filter((row) => row.msg.toLowerCase().includes(query)) : logs),
    [logs, query],
  )

  useEffect(() => {
    if (autoscroll && paperRef.current) {
      paperRef.current.scrollTop = paperRef.current.scrollHeight
    }
  }, [filtered, autoscroll, addressInput])

  async function copyAll() {
    const text = (query ? filtered : logs)
      .map((r) => `${r.ts} ${r.level} ${formatLogMsg(r.msg)}`)
      .join('\n')
    try {
      await navigator.clipboard.writeText(text)
      // 反馈由按钮自身短暂变化呈现
    } catch {
      /* 剪贴板不可用时静默 */
    }
  }

  const { label: badgeLabel, live } = statusBadgeMeta(status)

  return (
    <section className="flex min-h-0 flex-1 flex-col px-3 pb-3 pt-4 sm:px-5 sm:pb-4">
      {/*
        这一行原来是不换行的单行 flex，窄屏（手机）时会被挤爆：
        「运行日志」没有 shrink-0，被压到近 0 宽后 CJK 字符只能逐个换行，
        于是标题变成竖排；固定 w-44 的过滤框又和三个按钮抢空间。
        改成 flex-wrap 分三行排：标题+状态 / 过滤框整行 / 工具按钮。
      */}
      <div className="flex flex-wrap items-center gap-x-2.5 gap-y-2">
        <h2 className="shrink-0 whitespace-nowrap font-serif text-base font-semibold tracking-[1px]">
          运行日志
        </h2>
        <span className="inline-flex h-6 shrink-0 items-center gap-1.5 rounded-[2px] border bg-card px-2 text-xs font-medium">
          <span
            className={cn(
              'size-[7px] rounded-[1px] bg-primary',
              live && 'led-breathe',
            )}
          />
          {badgeLabel}
        </span>
        <Input
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
          placeholder="过滤日志…"
          className="h-8 w-full rounded-[4px] border-border bg-card text-xs sm:ml-auto sm:h-7 sm:w-44"
        />
        <div className="flex items-center gap-1.5">
          <ToolButton onClick={copyAll} label="复制日志">
            <Copy className="size-3.5" />
            <span className="hidden sm:inline">复制</span>
          </ToolButton>
          <ToolButton onClick={clearLogs} label="清空日志">
            <Eraser className="size-3.5" />
            <span className="hidden sm:inline">清空</span>
          </ToolButton>
          <ToolButton
            onClick={() => setAutoscroll((v) => !v)}
            active={autoscroll}
            label="自动滚动"
          >
            <ArrowDownToLine className="size-3.5" />
            <span className="hidden sm:inline">自动滚动</span>
          </ToolButton>
        </div>
      </div>

      <div className="receipt mt-3.5 flex min-h-0 flex-1 flex-col">
        <div className="receipt-tear" />
        <div
          ref={paperRef}
          className="receipt-paper scroll-contain min-h-0 flex-1 select-text overflow-y-auto border-x bg-card py-2.5 font-mono text-xs"
        >
          {filtered.length === 0 && !addressInput && (
            <p className="px-4 py-6 text-center text-[11px] text-ink-faint">
              {logs.length === 0
                ? '等待任务启动，日志将实时打印在这里。'
                : `未找到包含“${filter.trim()}”的日志。`}
            </p>
          )}
          {filtered.map((row) => {
            const hit = Boolean(query) && row.msg.toLowerCase().includes(query)
            return (
              <div key={row.id} className="receipt-row flex gap-2.5 px-4 leading-[1.75]">
                <span className="shrink-0 text-ink-faint">{row.ts}</span>
                <span className={cn('w-[38px] shrink-0 font-semibold', LEVEL_CLASS[row.level])}>
                  {row.level}
                </span>
                <span
                  className={cn(
                    'min-w-0 break-all text-foreground',
                    hit && 'rounded-[2px] bg-hit px-0.5',
                  )}
                >
                  {isOrderSummary(row.msg) ? <OrderSummaryText msg={row.msg} /> : row.msg}
                </span>
              </div>
            )
          })}
          {addressInput && (
            <InlineAddressInput
              key={addressInput.id}
              request={addressInput}
              onResolve={resolveAddressInput}
            />
          )}
          {logs.length > 0 && (
            <p className="mt-2.5 text-center font-serif text-[11px] tracking-[2px] text-ink-faint">
              — 一 口 轻 食 · 一 单 一 味 —
            </p>
          )}
        </div>
        <div className="h-0.5 shrink-0 border-x bg-card" />
      </div>
    </section>
  )
}

/**
 * 手机端日志「底部抽屉」。
 *
 * 交互（按需求）：
 * - 点底部「日志」→ 从下往上弹出，占大半屏（高度记住上次拖到的位置）；
 * - 点上方非日志区域（遮罩）→ 退回底部；
 * - 拖把手 → 自由调节高度，松手吸附到 收起/大半屏/全屏 三个停靠位；
 * - 收起状态只露出把手，不占地方（状态在底部 tab 上已有 LED 与文字）。
 *
 * 已展开时才渲染遮罩并拦截点击 —— 收起时遮罩不能存在，否则会把上面的任务界面
 * 全部点不动。
 */
function PhoneLogSheet({
  open,
  onOpenChange,
  tabBar,
}: {
  open: boolean
  onOpenChange: (next: boolean) => void
  /** 底部 tab 栏，与抽屉同列渲染以保证不重叠。 */
  tabBar: ReactNode
}) {
  const { addressInput } = useApp()

  /**
   * 展开高度单独记（`open` 由 App 管开合，这里只管「开多高」）。
   * 两者分开的好处：收起/展开切换不会丢掉用户拖出来的高度。
   */
  const [expandedFraction, setExpandedFraction] = useState(() => {
    const saved = typeof localStorage === 'undefined' ? null : localStorage.getItem(SHEET_STORAGE_KEY)
    return readStoredHalf(saved)
  })
  const [dragging, setDragging] = useState(false)

  // 拖动过程中的临时高度；松手后写回 expandedFraction。
  // 同时存一份到 ref：setState 是异步的，松手时若只读 state 可能还是上一帧的值，
  // 会把抽屉吸附到「手指已经离开的位置」。ref 永远是最后收到的那个值。
  const [dragFraction, setDragFraction] = useState<number | null>(null)
  const dragFractionRef = useRef<number | null>(null)
  const drag = useRef({ startY: 0, startFraction: 0, moved: false })

  const viewportPx = viewportHeight()
  // 抽屉会往上长，所以用视口高度换算位移。
  // 手机地址栏伸缩会改变 innerHeight：每次取用时读一次即可，不必常驻监听。
  const collapsedFraction = clampFraction(LOG_SHEET.peek, viewportPx)
  const fraction = dragFraction ?? (open ? expandedFraction : collapsedFraction)
  const collapsed = !open

  function viewportHeight() {
    return typeof window === 'undefined' ? 0 : window.innerHeight
  }

  // 手机浏览器地址栏伸缩 / 旋屏会改变视口高度，而「收起」是按比例存的。
  // 不重算的话，横竖屏切换后把手可能被挤出屏幕。
  const [, setViewportTick] = useState(0)
  useEffect(() => {
    const sync = () => setViewportTick((n) => n + 1)
    window.addEventListener('resize', sync)
    window.addEventListener('orientationchange', sync)
    return () => {
      window.removeEventListener('resize', sync)
      window.removeEventListener('orientationchange', sync)
    }
  }, [])

  // 待确认地址输入框在日志区里：收起时如果来了输入请求，必须自动弹出来，
  // 否则任务会一直卡在等输入，而用户看不到输入框。
  useEffect(() => {
    if (addressInput) onOpenChange(true)
    // 只在请求变化时触发；onOpenChange 由 App 提供，故意不入依赖
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [addressInput?.id])

  const onPointerDown = (e: React.PointerEvent) => {
    drag.current = { startY: e.clientY, startFraction: fraction, moved: false }
    setDragging(true)
    setDragFraction(fraction)
    dragFractionRef.current = fraction
    ;(e.currentTarget as HTMLElement).setPointerCapture(e.pointerId)
  }

  const onPointerMove = (e: React.PointerEvent) => {
    if (!dragging) return
    if (Math.abs(e.clientY - drag.current.startY) > 4) drag.current.moved = true
    const next = dragToFraction(
      drag.current.startFraction,
      drag.current.startY,
      e.clientY,
      viewportPx,
    )
    dragFractionRef.current = next
    setDragFraction(next)
  }

  const onPointerUp = () => {
    if (!dragging) return
    setDragging(false)
    const snapped = snapFraction(dragFractionRef.current ?? fraction, viewportPx)
    setDragFraction(null)
    dragFractionRef.current = null

    if (snapped <= collapsedFraction + 1e-6) {
      // 拖到底 → 收起
      onOpenChange(false)
      return
    }
    // 展开并记住这次拖到的高度，下次打开回到这里
    setExpandedFraction(snapped)
    onOpenChange(true)
    try {
      localStorage.setItem(SHEET_STORAGE_KEY, String(snapped))
    } catch {
      /* 隐私模式下 localStorage 可能抛错，静默即可 */
    }
  }

  /** 点把手：展开 ↔ 收起。刚拖过就不切换，避免松手时误触。 */
  const onHandleClick = () => {
    if (drag.current.moved) return
    onOpenChange(!open)
  }

  return (
    /*
      固定底部列：遮罩（撑满剩余空间）+ 抽屉 + tab 栏自下而上排列。
      - 抽屉高度用 CSS 变量驱动，遮罩的边界与之同源，不可能算出不同高度；
      - 抽屉与 tab 栏是兄弟节点，tab 栏多高都不会被把手压住或压住把手；
      - 容器只在手机布局下渲染，不会挡到平板/桌面的布局。
    */
    <div
      style={{ '--sheet-h': `${Math.round(fraction * 100)}vh` } as React.CSSProperties}
      className="fixed inset-x-0 bottom-0 z-30 flex flex-col"
    >
      {!collapsed && (
        <button
          type="button"
          aria-label="收起日志"
          onClick={() => onOpenChange(false)}
          className="min-h-0 flex-1 bg-black/25"
        />
      )}
      <section
        role="dialog"
        aria-label="运行日志"
        aria-modal={!collapsed}
        className={cn(
          'flex shrink-0 flex-col border-t bg-background',
          'h-[var(--sheet-h)]',
          !dragging && 'transition-[height] duration-200 ease-out',
        )}
      >
        <div
          onPointerDown={onPointerDown}
          onPointerMove={onPointerMove}
          onPointerUp={onPointerUp}
          onPointerCancel={onPointerUp}
          onClick={onHandleClick}
          className="flex h-7 shrink-0 cursor-grab touch-none select-none items-center justify-center active:cursor-grabbing"
        >
          {/* 把手：告诉用户这里可以拖 */}
          <span className="h-1 w-12 rounded-full bg-border" />
        </div>
        <div className="flex min-h-0 flex-1 flex-col px-2.5 pb-1.5">
          {/* 收起时隐藏内容（保留 DOM 以免日志滚动位置丢失），并禁止聚焦 */}
          <div
            className={cn('flex min-h-0 flex-1 flex-col', collapsed && 'invisible')}
            inert={collapsed}
          >
            <LogConsoleBody />
          </div>
        </div>
      </section>
      {tabBar}
    </div>
  )
}

/** 日志区内的待确认地址输入框：不弹窗，直接在原地址下面换地址 */
function InlineAddressInput({
  request,
  onResolve,
}: {
  request: AddressInputRequest
  onResolve: (id: string, entries: Record<string, string>) => void
}) {
  const [values, setValues] = useState<Record<string, string>>({})
  const filled = request.items.some((item) => (values[item.raw_address] ?? '').trim().length > 0)

  function update(raw: string, value: string) {
    setValues((prev) => ({ ...prev, [raw]: value }))
  }

  function submit() {
    const entries: Record<string, string> = {}
    for (const item of request.items) {
      const value = (values[item.raw_address] ?? '').trim()
      if (value) entries[item.raw_address] = value
    }
    onResolve(request.id, entries)
  }

  function skip() {
    onResolve(request.id, {})
  }

  return (
    <div className="mx-4 my-2.5 rounded-[4px] border border-dashed border-primary bg-primary-soft/40 p-3">
      <div className="font-serif text-xs font-semibold text-primary-strong">
        待确认地址 · 请直接输入最终地址
      </div>
      <p className="mt-1 text-[11px] leading-relaxed text-muted-foreground">
        输入后点「应用并排序」，系统会自动改表并整理；留空则保持原待确认流程。
      </p>
      <div className="mt-2.5 space-y-2.5">
        {request.items.map((item) => (
          <div key={item.raw_address} className="rounded-[3px] border border-border bg-card p-2.5">
            <div className="flex flex-wrap items-center gap-x-2 gap-y-0.5 text-[11px]">
              <span className="font-semibold text-foreground">{item.order_numbers.join('、')}</span>
              <span className="text-ink-faint">{item.reason || '无法自动识别'}</span>
            </div>
            <div className="mt-0.5 break-all text-[11px] text-ink-faint">{item.raw_address}</div>
            {item.suggested_point ? (
              <div className="mt-0.5 text-[11px] text-ink-faint">规则建议：{item.suggested_point}</div>
            ) : null}
            <input
              autoFocus={request.items.indexOf(item) === 0}
              value={values[item.raw_address] ?? ''}
              onChange={(e) => update(item.raw_address, e.target.value)}
              placeholder="在这里输入地址，如 D2 / 学三 / 教5"
              className="mt-1.5 h-9 w-full rounded-[4px] border border-border bg-secondary px-3 text-sm outline-none focus:border-primary focus:ring-2 focus:ring-primary/30"
            />
          </div>
        ))}
      </div>
      <div className="mt-3 flex justify-end gap-2">
        <button
          type="button"
          onClick={skip}
          className="h-8 rounded-[4px] border border-border bg-card px-3 text-xs text-muted-foreground transition-colors hover:border-primary hover:text-foreground"
        >
          暂不处理
        </button>
        <button
          type="button"
          onClick={submit}
          disabled={!filled}
          className="h-8 rounded-[4px] bg-primary px-3 text-xs font-medium text-primary-foreground transition-colors hover:bg-primary-strong disabled:cursor-not-allowed disabled:opacity-40"
        >
          应用并排序
        </button>
      </div>
    </div>
  )
}

/** 订单摘要行（W8 | 李 | 电话 | 地址 | 餐品）→ 每字段一行 */
function OrderSummaryText({ msg }: { msg: string }) {
  const parts = splitOrderSummary(msg)
  return (
    <span className="block">
      {parts.map((part, i) => (
        <span key={i} className={cn('block', i === 0 && 'font-semibold text-primary-strong')}>
          {part}
        </span>
      ))}
    </span>
  )
}

function ToolButton({
  onClick,
  active,
  label,
  children,
}: {
  onClick: () => void
  active?: boolean
  label: string
  children: ReactNode
}) {
  return (
    <button
      onClick={onClick}
      title={label}
      aria-label={label}
      className={cn(
        'touch-target inline-flex h-8 items-center gap-1 rounded-[4px] border px-2.5 text-xs transition-colors sm:h-7 sm:px-2',
        active
          ? 'border-primary bg-primary-soft text-primary-strong'
          : 'border-border bg-card text-muted-foreground hover:border-primary hover:text-foreground',
      )}
    >
      {children}
    </button>
  )
}
