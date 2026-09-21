/**
 * 运行日志 = 可搜索、可复制、可展开明细的运行摘要。
 *
 * 常驻只留四样：标题、一个任务状态、关闭入口（手机端是右上角 `LogFab`，它在
 * 面板之上、始终可点）、日志正文。搜索是图标入口，级别筛选/自动滚动/复制/清理
 * 收进工具菜单 —— 低频操作不再占满头部。
 *
 * 警告与错误**不默认过滤**（级别默认「全部」）；需要用户处理的待确认地址输入
 * 直接渲染在正文上方，不进菜单。
 *
 * 折叠/展开规则见 `lib/logDisplay.ts`：能不能展开取决于「是否真的有隐藏内容」，
 * 不看字符串长度；展开是**替换**摘要，不会把同一份内容显示两遍。
 *
 * 过滤只影响展示，不删除数据。
 */
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type ComponentProps, type ReactNode } from 'react'
import { ChevronDown, ChevronRight, Copy, Eraser, Search, SlidersHorizontal, X } from 'lucide-react'
import { toast } from 'sonner'
import { Input } from '@/components/ui/input'
import {
  DropdownMenu,
  DropdownMenuCheckboxItem,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import type { AddressInputRequest, LogEntry } from '@/lib/bridge'
import { useApp } from '@/hooks/appContext'
import { cn } from '@/lib/utils'
import { copyText } from '@/lib/clipboard'
import { logCopyText, logRowRender, logRowToggle, logRowView, normalizeLogQuery } from '@/lib/logDisplay'
import type { InteractionResolveResult, LogRow } from '@/hooks/appContext'
import { interactionRecoveryView } from '@/lib/interactionRecovery'
import { LOG_REVEAL_ORIGIN, REVEAL_TIMING, canAnimate, closeFramesCss, openFramesCss, prefersReducedMotion } from '@/lib/reveal'
import type { LogReveal } from '@/lib/useLogReveal'

const LEVELS = ['ALL', 'INFO', 'OK', 'WARN', 'ERROR'] as const
type LevelFilter = (typeof LEVELS)[number]

const LEVEL_LABEL: Record<LevelFilter, string> = {
  ALL: '全部',
  INFO: 'INFO',
  OK: 'OK',
  WARN: '警告',
  ERROR: '错误',
}

const LEVEL_CLASS: Record<string, string> = {
  OK: 'text-success',
  INFO: 'text-muted-foreground',
  WARN: 'text-warning',
  ERROR: 'text-destructive',
}

export function LogConsole({
  layout = 'desktop',
  reveal,
}: {
  layout?: 'phone' | 'tablet' | 'desktop'
  reveal?: LogReveal
}) {
  return (
    <>
      {layout !== 'phone' && <LogConsoleBody />}
      {layout === 'phone' && (reveal ? <PhoneLogSheet reveal={reveal} /> : <LogConsoleBody />)}
    </>
  )
}

function LogConsoleBody() {
  const { logs, operationView, clearLogs, addressInput, resolveAddressInput } = useApp()
  const [filter, setFilter] = useState('')
  const [searchOpen, setSearchOpen] = useState(false)
  const [level, setLevel] = useState<LevelFilter>('ALL')
  const [autoscroll, setAutoscroll] = useState(true)
  const paperRef = useRef<HTMLDivElement>(null)

  const query = normalizeLogQuery(filter)
  const filtered = useMemo(
    () => logs.filter((row) => {
      if (level !== 'ALL' && row.level !== level) return false
      if (!query) return true
      // 按整条原始消息匹配：被折叠的行也参与搜索，命中后再强制展开。
      return row.msg.toLowerCase().includes(query)
    }),
    [logs, level, query],
  )

  const levelCounts = useMemo(() => {
    const counts: Record<LevelFilter, number> = { ALL: logs.length, INFO: 0, OK: 0, WARN: 0, ERROR: 0 }
    for (const row of logs) counts[row.level] += 1
    return counts
  }, [logs])
  const problemCount = levelCounts.WARN + levelCounts.ERROR

  useEffect(() => {
    // 搜索时定位交给下面的「滚动到第一条命中」，不抢着跳到底部。
    if (query) return
    if (autoscroll && paperRef.current) paperRef.current.scrollTop = paperRef.current.scrollHeight
  }, [filtered, autoscroll, addressInput, query])

  // 命中可能落在原本被折叠的行里（同一帧已强制展开）：把第一条命中滚到可视区中间，
  // 否则「搜索命中隐藏部分」只是渲染出来了，用户还得自己找。
  useEffect(() => {
    const paper = paperRef.current
    if (!paper || !query) return
    const hit = paper.querySelector<HTMLElement>('[data-log-hit="true"]')
    if (!hit) return
    const box = paper.getBoundingClientRect()
    const target = hit.getBoundingClientRect()
    paper.scrollTop += target.top - box.top - (box.height - target.height) / 2
  }, [query, level])

  async function copyAll() {
    const outcome = await copyText(logCopyText(filtered))
    if (outcome.ok) toast.success(`已复制 ${filtered.length} 条日志（完整原文）`)
    else toast.error(outcome.error || '复制失败，请长按日志手动选择')
  }

  function closeSearch() {
    // 关闭搜索同时清空筛选：否则日志少了一截却看不出原因（隐藏状态比少一个控件更糟）。
    setSearchOpen(false)
    setFilter('')
  }

  return (
    <section className="flex min-h-0 flex-1 flex-col px-3 pb-3 pt-3 sm:px-5 sm:pb-4">
      {/* 第一行右侧让位给右上角日志按钮（手机端 fixed，不占布局）：
          否则搜索/工具按钮会被它压住点不到。
          变量只在手机布局由 App 注入，其余布局回落到 0px。 */}
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1.5 pr-[var(--fab-clearance,0px)]">
        <h2 className="shrink-0 whitespace-nowrap font-serif text-base font-semibold tracking-[1px]">运行日志</h2>
        <span
          data-log-status="true"
          className={cn(
            'inline-flex h-6 shrink-0 items-center gap-1.5 rounded-full border bg-card px-2 text-xs font-medium',
            operationView.tone === 'danger' && 'border-destructive/40 text-destructive',
            operationView.tone === 'warning' && 'border-warning/45 text-warning',
            operationView.tone === 'success' && 'border-success/40 text-success',
            operationView.tone === 'progress' && 'border-primary/40 text-primary',
          )}
          title={operationView.detail}
        >
          <span className={cn('size-1.5 rounded-full bg-current', operationView.active && 'led-breathe')} />
          {operationView.label}
        </span>
        <div className="ml-auto flex min-w-0 flex-1 items-center justify-end gap-1.5 sm:flex-none">
          {searchOpen ? (
            <>
              <Input
                autoFocus
                value={filter}
                onChange={(e) => setFilter(e.target.value)}
                placeholder="搜索日志…"
                aria-label="搜索运行日志"
                data-log-search="true"
                className="h-8 min-w-[6rem] flex-1 rounded-[6px] border-border bg-card text-xs sm:h-7 sm:w-40 sm:flex-none"
              />
              {query !== '' && (
                <span data-log-count="true" className="shrink-0 whitespace-nowrap text-[10px] text-muted-foreground">
                  显示 {filtered.length}/{logs.length}
                </span>
              )}
              <ToolButton onClick={closeSearch} label="关闭搜索并清除筛选" data-log-search-close="true">
                <X className="size-3.5" />
              </ToolButton>
            </>
          ) : (
            <ToolButton onClick={() => setSearchOpen(true)} label="搜索日志" data-log-search-open="true">
              <Search className="size-3.5" />
            </ToolButton>
          )}
          <LogToolsMenu
            level={level}
            onLevel={setLevel}
            levelCounts={levelCounts}
            autoscroll={autoscroll}
            onAutoscroll={setAutoscroll}
            problemCount={problemCount}
            onCopy={() => void copyAll()}
            onClear={clearLogs}
          />
        </div>
      </div>

      {addressInput && (
        <InlineAddressInput key={addressInput.id} request={addressInput} onResolve={resolveAddressInput} />
      )}

      <div className="receipt mt-2 flex min-h-0 flex-1 flex-col sm:mt-3">
        <div className="receipt-tear" />
        <div ref={paperRef} className="receipt-paper scroll-contain min-h-0 flex-1 select-text overflow-y-auto border-x bg-card py-2.5 font-mono text-xs">
          {filtered.length === 0 && !addressInput && (
            <p className="px-4 py-6 text-center text-[11px] text-ink-faint">
              {logs.length === 0
                ? '等待任务启动，日志将实时显示在这里。'
                : level !== 'ALL' || query
                  ? '当前筛选无匹配日志；警告和错误仍完整保留在数据中。'
                  : '暂无日志。'}
            </p>
          )}
          {filtered.map((row) => <LogRowItem key={row.id} row={row} query={query} />)}
        </div>
        <div className="h-0.5 shrink-0 border-x bg-card" />
      </div>
    </section>
  )
}

/**
 * 一条日志：折叠态只显示「真的看不全」的那部分，展开是替换而不是追加。
 *
 * 数据不因折叠丢失：`logRowView` 始终保留 `fullLines`，复制走的是原始 `msg`。
 */
function LogRowItem({ row, query }: { row: LogEntry | LogRow; query: string }) {
  const [expanded, setExpanded] = useState(false)
  const view = useMemo(() => logRowView(row.msg, query), [row.msg, query])
  const render = logRowRender(view, expanded)
  const toggle = logRowToggle(view, expanded)
  const hitLines = new Set(render.hitLines)
  const hit = render.hitLines.length > 0 || render.wholeHit
  return (
    <div
      data-log-row="true"
      data-log-hit={hit ? 'true' : 'false'}
      data-log-expandable={view.expandable ? 'true' : 'false'}
      data-log-full={render.full ? 'true' : 'false'}
      className="receipt-row flex gap-2.5 px-3 leading-[1.7]"
    >
      <span className="mt-0.5 w-[48px] shrink-0 text-[10px] text-ink-faint">{row.ts}</span>
      <span
        data-log-level-badge={row.level}
        className={cn('mt-0.5 w-[38px] shrink-0 text-[10px] font-semibold', LEVEL_CLASS[row.level] || 'text-muted-foreground')}
      >
        {row.level}
      </span>
      <div className="min-w-0 flex-1">
        <span className={cn('block break-words', render.wholeHit && 'rounded-[2px] bg-hit px-0.5')}>
          {render.lines.map((line, index) => (
            <span
              key={index}
              data-log-line={index}
              className={cn(
                'block break-words',
                hitLines.has(index) && 'rounded-[2px] bg-hit px-0.5',
                view.orderSummary && index === 0 && 'font-semibold text-primary-strong',
              )}
            >
              {/* 空行用不换行空格撑住高度，否则多行日志里的空行会被压没。 */}
              {line === '' ? '\u00a0' : line}
            </span>
          ))}
        </span>
        {toggle === 'toggle' && (
          <button
            type="button"
            data-log-toggle={expanded ? 'collapse' : 'expand'}
            aria-expanded={expanded}
            onClick={() => setExpanded((value) => !value)}
            className="mt-0.5 inline-flex items-center gap-0.5 text-[10px] text-primary underline"
          >
            {expanded ? <ChevronDown className="size-3" /> : <ChevronRight className="size-3" />}
            {expanded ? '收起明细' : `展开明细（另有 ${view.fullLines.length - 1} 行）`}
          </button>
        )}
        {toggle === 'forced' && (
          <span data-log-forced="true" className="mt-0.5 inline-flex items-center gap-0.5 text-[10px] text-muted-foreground">
            <ChevronDown className="size-3" />
            搜索命中隐藏内容，已展开
          </span>
        )}
      </div>
    </div>
  )
}

/**
 * 日志工具菜单：级别筛选、自动滚动、复制、清理。
 *
 * 这些是低频操作，默认不占头部；警告/错误条数以角标形式留在入口上，
 * 不打开菜单也能看见（不是「藏进菜单」）。
 */
function LogToolsMenu({
  level,
  onLevel,
  levelCounts,
  autoscroll,
  onAutoscroll,
  problemCount,
  onCopy,
  onClear,
}: {
  level: LevelFilter
  onLevel: (level: LevelFilter) => void
  levelCounts: Record<LevelFilter, number>
  autoscroll: boolean
  onAutoscroll: (value: boolean) => void
  problemCount: number
  onCopy: () => void
  onClear: () => void
}) {
  const toolsLabel = '日志工具：级别筛选、自动滚动、复制、清理'
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <button
          type="button"
          data-log-tools="true"
          aria-label={toolsLabel}
          title={toolsLabel}
          className={cn(
            'inline-flex h-7 shrink-0 items-center gap-1 rounded-[6px] border border-border bg-card px-1.5 text-[10px] text-muted-foreground transition-colors hover:border-primary hover:text-foreground',
            problemCount > 0 && (levelCounts.ERROR > 0 ? 'border-destructive/40 text-destructive' : 'border-warning/45 text-warning'),
          )}
        >
          <SlidersHorizontal className="size-3.5" />
          {problemCount > 0 && (
            <span
              data-log-problems={problemCount}
              className={cn(
                'inline-flex h-4 min-w-4 items-center justify-center rounded-full px-1 text-[10px] font-semibold',
                levelCounts.ERROR > 0 ? 'bg-destructive/15 text-destructive' : 'bg-warning/15 text-warning',
              )}
            >
              {problemCount}
            </span>
          )}
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-60 rounded-md text-xs">
        <DropdownMenuLabel className="text-[11px] text-muted-foreground">级别筛选（默认显示全部）</DropdownMenuLabel>
        <DropdownMenuRadioGroup value={level} onValueChange={(value) => onLevel(value as LevelFilter)}>
          {LEVELS.map((item) => (
            <DropdownMenuRadioItem key={item} value={item} data-log-level={item} className="text-xs">
              {LEVEL_LABEL[item]}
              <span className="ml-auto pl-3 text-[10px] text-muted-foreground">{levelCounts[item]}</span>
            </DropdownMenuRadioItem>
          ))}
        </DropdownMenuRadioGroup>
        <DropdownMenuSeparator />
        <DropdownMenuCheckboxItem
          checked={autoscroll}
          onCheckedChange={(value) => onAutoscroll(value === true)}
          data-log-autoscroll="true"
          className="text-xs"
        >
          自动滚动到最新
        </DropdownMenuCheckboxItem>
        <DropdownMenuSeparator />
        <DropdownMenuItem data-log-copy="true" className="text-xs" onSelect={onCopy}>
          <Copy className="size-3.5" />
          复制当前筛选（完整原文）
        </DropdownMenuItem>
        <DropdownMenuItem data-log-clear="true" className="text-xs" onSelect={onClear}>
          <Eraser className="size-3.5" />
          清理常规日志（保留警告与审计）
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

/** 日志区内的待确认地址：服务端确认成功前不清空、不关闭，失败可重试。 */
function InlineAddressInput({
  request,
  onResolve,
}: {
  request: AddressInputRequest
  onResolve: (id: string, entries: Record<string, string>) => Promise<InteractionResolveResult>
}) {
  const { connection, recovery, operationActive } = useApp()
  const [values, setValues] = useState<Record<string, string>>({})
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const recoveryView = interactionRecoveryView({
    hasLocalRequest: true, hasPersistedMeta: false, connection, recovery, operationActive, hasServerReadApi: true,
  })
  const filled = request.items.some((item) => (values[item.raw_address] ?? '').trim().length > 0)

  async function submit() {
    if (busy) return
    const entries: Record<string, string> = {}
    for (const item of request.items) {
      const value = (values[item.raw_address] ?? '').trim()
      if (value) entries[item.raw_address] = value
    }
    setBusy(true)
    setError('')
    const result = await onResolve(request.id, entries)
    if (!result.ok && result.retryable) setError(result.message || '提交失败，输入已保留，请重试')
    setBusy(false)
  }

  return (
    <div className="mt-2 rounded-lg border border-dashed border-primary bg-primary-soft/30 p-3">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-xs font-semibold text-primary-strong">待确认地址 · 请输入最终地址</span>
        <span className="text-[10px] text-muted-foreground">{request.message}</span>
      </div>
      {recoveryView.visible && (
        <div className="mt-2 rounded-md border border-warning/45 bg-warning/10 px-2.5 py-2 text-[11px]">
          <p className="font-medium">{recoveryView.title}</p>
          <p className="mt-0.5 leading-relaxed text-muted-foreground">{recoveryView.detail}</p>
        </div>
      )}
      <div className="mt-2 space-y-2">
        {request.items.map((item) => (
          <div key={item.raw_address} className="rounded-md border bg-card p-2.5">
            <div className="flex flex-wrap items-center gap-x-2 text-[11px]">
              <span className="font-semibold text-foreground">{item.order_numbers.join('、')}</span>
              <span className="text-ink-faint">{item.reason || '无法自动识别'}</span>
            </div>
            <div className="mt-0.5 break-all text-[11px] text-ink-faint">{item.raw_address}</div>
            {item.suggested_point && <div className="mt-0.5 text-[11px] text-muted-foreground">规则建议：{item.suggested_point}</div>}
            <input
              autoFocus={request.items.indexOf(item) === 0}
              value={values[item.raw_address] ?? ''}
              disabled={busy}
              onChange={(e) => setValues((prev) => ({ ...prev, [item.raw_address]: e.target.value }))}
              placeholder="输入最终地址，如 D2 / 学三 / 教5"
              className="mt-1.5 h-10 w-full rounded-[6px] border border-border bg-secondary px-3 text-sm outline-none focus:border-primary focus:ring-2 focus:ring-primary/30"
            />
          </div>
        ))}
      </div>
      {error && <p role="alert" className="mt-2 rounded border border-destructive/40 bg-destructive/5 px-2 py-1 text-[11px] text-destructive">{error}</p>}
      <div className="mt-3 flex justify-end gap-2">
        <button
          type="button"
          disabled={busy}
          onClick={() => void submit()}
          className="h-9 rounded-[6px] border border-border bg-card px-3 text-xs text-muted-foreground hover:border-primary hover:text-foreground disabled:opacity-50"
        >
          暂不处理
        </button>
        <button
          type="button"
          disabled={!filled || busy}
          onClick={() => void submit()}
          className="h-9 rounded-[6px] bg-primary px-3 text-xs font-medium text-primary-foreground hover:bg-primary-strong disabled:opacity-40"
        >
          {busy ? '提交中…' : '应用并排序'}
        </button>
      </div>
    </div>
  )
}

type ToolButtonProps = Omit<ComponentProps<'button'>, 'onClick' | 'children' | 'title' | 'aria-label'> & {
  onClick: () => void
  active?: boolean
  label: string
  children: ReactNode
}

function ToolButton({ onClick, active, label, children, className, ...rest }: ToolButtonProps) {
  return (
    <button
      type="button"
      onClick={onClick}
      title={label}
      aria-label={label}
      className={cn(
        'inline-flex h-7 shrink-0 items-center gap-1 rounded-[6px] border px-1.5 text-[10px] transition-colors',
        active ? 'border-primary bg-primary-soft text-primary-strong' : 'border-border bg-card text-muted-foreground hover:border-primary hover:text-foreground',
        className,
      )}
      {...rest}
    >
      {children}
    </button>
  )
}

function cancelReveal(animation: { current: Animation | null }) {
  try {
    animation.current?.cancel()
  } catch {
    // 个别 WebView 上 cancel() 也会抛；取消失败不能影响状态机，丢弃动画对象即可。
  }
  animation.current = null
}

function currentClipPath(element: HTMLElement, fallback: string): string {
  try {
    const value = getComputedStyle(element).clipPath
    if (value && value !== 'none' && value.startsWith('circle(')) return value
  } catch {
    // fallback
  }
  return fallback
}

const CLOSED_CLIP = `circle(0px at ${LOG_REVEAL_ORIGIN})`
/** 完全展开时的裁剪值：与 `openFramesCss().to` 同一个表达式。 */
const OPEN_CLIP = openFramesCss().to

function PhoneLogSheet({ reveal }: { reveal: LogReveal }) {
  const { addressInput } = useApp()
  const [phase, setPhase] = useState<'closed' | 'open' | 'closing'>('closed')
  const sheetRef = useRef<HTMLElement>(null)
  const revealAnimation = useRef<Animation | null>(null)
  const handledNonce = useRef(0)
  /**
   * 收尾定时器 + 代次。
   *
   * 关闭动画的 onfinish / 兜底定时器都可能**迟到**（快速开关、合成器丢帧、页面切后台），
   * 代次让迟到的收尾变成空操作：每次展开或关闭都自增，回调只在自己那一代仍是最新时
   * 才落 `closed`。这样不必依赖 effect cleanup —— effect 的依赖里就有 `phase`，
   * 用它自己的 cleanup 去作废收尾会在 `setPhase('closing')` 之后**立刻误杀本次关闭**，
   * 面板于是永远停在 `closing`（全屏、不透明、点不动 = 用户退不出来）。
   * 浏览器门禁第 7 节实测过这条：`SCENARIO=log-close-stuck-closing` 就是它的反证。
   */
  const closeTimer = useRef(0)
  const generation = useRef(0)

  const clearCloseTimer = useCallback(() => {
    if (!closeTimer.current) return
    window.clearTimeout(closeTimer.current)
    closeTimer.current = 0
  }, [])

  useEffect(() => () => {
    // 卸载：作废一切在途收尾，别让定时器碰到已卸载的 DOM。
    generation.current += 1
    clearCloseTimer()
    cancelReveal(revealAnimation)
  }, [clearCloseTimer])

  useEffect(() => {
    if (!addressInput) return
    reveal.openFrom(null)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [addressInput?.id])

  useLayoutEffect(() => {
    if (!reveal.open) return
    if (reveal.nonce === handledNonce.current) return
    handledNonce.current = reveal.nonce
    generation.current += 1
    clearCloseTimer()
    const element = sheetRef.current
    cancelReveal(revealAnimation)
    // 状态先落地：即使下面量不到元素或环境不支持动画，界面也必须是「展开」的。
    setPhase('open')
    if (!element || !canAnimate(element) || prefersReducedMotion()) return
    const frames = openFramesCss()
    try {
      revealAnimation.current = element.animate(
        [{ clipPath: frames.from }, { clipPath: frames.to }],
        { duration: REVEAL_TIMING.openMs, easing: REVEAL_TIMING.openEase },
      )
    } catch {
      // 动画不可用时保持已展开的终态（内联裁剪值由 style 保证），不吞掉展开动作。
      revealAnimation.current = null
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [reveal.open, reveal.nonce, clearCloseTimer])

  useLayoutEffect(() => {
    if (reveal.open) return
    if (phase !== 'open') return
    const element = sheetRef.current
    if (!element) {
      setPhase('closed')
      return
    }
    const frames = closeFramesCss()
    const startClip = currentClipPath(element, frames.from)
    const currentGeneration = generation.current + 1
    generation.current = currentGeneration
    clearCloseTimer()
    cancelReveal(revealAnimation)
    setPhase('closing')
    // 降级 1：没有 Web Animations API 或用户要求减弱动效 —— 直接落终态。
    if (!canAnimate(element) || prefersReducedMotion()) {
      element.style.clipPath = frames.to
      setPhase('closed')
      return
    }
    let animation: Animation
    // 降级 2：animate() 抛错（老 WebView 不支持 clip-path 关键帧等）也必须可靠关闭。
    // 否则 phase 停在 closing：面板仍是全屏尺寸、不透明，用户就困在日志层里出不来。
    try {
      animation = element.animate(
        [{ clipPath: startClip }, { clipPath: frames.to }],
        { duration: REVEAL_TIMING.closeMs, easing: REVEAL_TIMING.closeEase },
      )
    } catch {
      element.style.clipPath = frames.to
      setPhase('closed')
      return
    }
    revealAnimation.current = animation
    const finish = () => {
      // 迟到的收尾（已被重新展开 / 已被新的开合取代）必须是空操作。
      if (generation.current !== currentGeneration) return
      clearCloseTimer()
      if (revealAnimation.current === animation) revealAnimation.current = null
      element.style.clipPath = frames.to
      setPhase('closed')
    }
    // 降级 3：onfinish / oncancel 都没来（合成器丢帧、页面切后台等）时由定时器兜底，
    // 保证一定收回到终态。定时器存在 ref 里、由代次判定有效性，**不能**用 effect
    // cleanup 作废：依赖里的 `phase` 会在 setPhase('closing') 后立刻触发 cleanup，
    // 那会把本次关闭的收尾一起杀掉（面板永远停在 closing = 用户退不出来）。
    closeTimer.current = window.setTimeout(finish, REVEAL_TIMING.closeMs + 150)
    animation.onfinish = finish
    animation.oncancel = () => {
      if (revealAnimation.current === animation) revealAnimation.current = null
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [reveal.open, phase, clearCloseTimer])

  return (
    <section
      id="phone-log-sheet"
      ref={sheetRef}
      role="dialog"
      aria-label="运行日志"
      aria-modal={reveal.open}
      aria-hidden={phase === 'closed'}
      inert={phase !== 'open'}
      className={cn('fixed inset-0 z-30 flex flex-col bg-background', phase !== 'open' && 'pointer-events-none')}
      style={{
        // 裁剪值始终由 React 显式写入（不依赖动画结束后残留的内联样式）：
        // 展开态写满圆，收起态写半径 0；动画只在这两者之间插值。
        clipPath: phase === 'closed' ? CLOSED_CLIP : OPEN_CLIP,
        visibility: reveal.open || phase !== 'closed' ? 'visible' : 'hidden',
        pointerEvents: phase === 'open' ? 'auto' : 'none',
        willChange: 'clip-path',
        transform: 'translateZ(0)',
        backfaceVisibility: 'hidden',
        contain: 'layout paint style',
      }}
    >
      {/* 内容顶部 = 安全区 + 手机端提示区域高度：提示区域是正常布局的一部分
          （见 App.tsx 与 index.css 的 .phone-notice-region），而本层是 fixed inset-0
          整屏覆盖；不在这里让位，提示条就会盖住日志头部（状态/搜索/工具）。
          变量由 App 用 ResizeObserver 实测注入，长提示/多条提示/字体放大都会跟着变。 */}
      <div
        className="flex min-h-0 flex-1 flex-col px-2.5"
        style={{
          // 提示区域是正常布局的一部分且在提示层之上（z-40 > z-30），所以内容要从
          // **它的底边**之下开始：无提示时底边变量是 0，退回原来的 safe-top。
          paddingTop: 'max(var(--safe-top), var(--phone-notice-bottom, 0px))',
          paddingBottom: 'var(--safe-bottom)',
        }}
      >
        <LogConsoleBody />
      </div>
    </section>
  )
}
