/**
 * 运行日志 = 可筛选、可复制、可展开明细的运行摘要。
 * 警告/错误使用结构化 level 字段着色；过滤只影响展示，不删除数据。
 */
import { useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { ArrowDownToLine, ChevronDown, ChevronRight, Copy, Eraser, Filter } from 'lucide-react'
import { Input } from '@/components/ui/input'
import type { AddressInputRequest, LogEntry } from '@/lib/bridge'
import { useApp } from '@/hooks/appContext'
import { cn } from '@/lib/utils'
import { formatLogMsg, isOrderSummary, splitOrderSummary } from '@/lib/format'
import type { InteractionResolveResult, LogRow } from '@/hooks/appContext'
import { interactionRecoveryView } from '@/lib/interactionRecovery'
import { LOG_REVEAL_ORIGIN, REVEAL_TIMING, canAnimate, closeFramesCss, openFramesCss, prefersReducedMotion } from '@/lib/reveal'
import type { LogReveal } from '@/lib/useLogReveal'

const LEVELS = ['ALL', 'INFO', 'OK', 'WARN', 'ERROR'] as const
type LevelFilter = (typeof LEVELS)[number]

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
  const [level, setLevel] = useState<LevelFilter>('ALL')
  const [autoscroll, setAutoscroll] = useState(true)
  const [copied, setCopied] = useState(false)
  const paperRef = useRef<HTMLDivElement>(null)

  const query = filter.trim().toLowerCase()
  const filtered = useMemo(
    () => logs.filter((row) => {
      if (level !== 'ALL' && row.level !== level) return false
      if (!query) return true
      return row.msg.toLowerCase().includes(query)
    }),
    [logs, level, query],
  )

  const warningCount = useMemo(() => logs.filter((row) => row.level === 'WARN').length, [logs])
  const errorCount = useMemo(() => logs.filter((row) => row.level === 'ERROR').length, [logs])

  useEffect(() => {
    if (autoscroll && paperRef.current) paperRef.current.scrollTop = paperRef.current.scrollHeight
  }, [filtered, autoscroll, addressInput])

  async function copyAll() {
    const text = filtered.map((row) => `${row.ts} ${row.level} ${formatLogMsg(row.msg)}`).join('\n')
    try {
      await navigator.clipboard.writeText(text)
      setCopied(true)
      window.setTimeout(() => setCopied(false), 1500)
    } catch {
      setCopied(false)
    }
  }

  return (
    <section className="flex min-h-0 flex-1 flex-col px-3 pb-3 pt-3 sm:px-5 sm:pb-4">
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1.5">
        <h2 className="shrink-0 whitespace-nowrap font-serif text-base font-semibold tracking-[1px]">运行日志</h2>
        <span
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
        {warningCount > 0 && <span className="shrink-0 rounded-full border border-warning/40 bg-warning/10 px-2 py-0.5 text-[10px] text-warning">警告 {warningCount}</span>}
        {errorCount > 0 && <span className="shrink-0 rounded-full border border-destructive/40 bg-destructive/10 px-2 py-0.5 text-[10px] text-destructive">错误 {errorCount}</span>}
        <div className="ml-auto flex min-w-0 flex-wrap items-center gap-1.5">
          <Input
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
            placeholder="过滤日志…"
            aria-label="过滤运行日志"
            className="h-8 min-w-[8rem] flex-1 rounded-[6px] border-border bg-card text-xs sm:h-7 sm:w-44 sm:flex-none"
          />
          <ToolButton onClick={() => setAutoscroll((v) => !v)} active={autoscroll} label="自动滚动">
            <ArrowDownToLine className="size-3.5" />
          </ToolButton>
        </div>
      </div>

      <div className="mt-1.5 flex flex-wrap items-center gap-1" role="group" aria-label="按级别筛选日志">
        <Filter className="mr-0.5 size-3.5 text-muted-foreground" />
        {LEVELS.map((item) => (
          <button
            key={item}
            type="button"
            aria-pressed={level === item}
            onClick={() => setLevel(item)}
            className={cn(
              'h-7 rounded-full border px-2 text-[10px] font-medium transition-colors',
              level === item
                ? 'border-primary bg-primary-soft text-primary-strong'
                : 'border-border bg-card text-muted-foreground hover:text-foreground',
            )}
          >
            {item === 'ALL' ? '全部' : item}
          </button>
        ))}
        <span className="ml-auto text-[10px] text-muted-foreground">显示 {filtered.length}/{logs.length}</span>
        <ToolButton onClick={() => void copyAll()} label="复制当前筛选日志">
          <Copy className="size-3.5" />
          {copied ? '已复制' : '复制'}
        </ToolButton>
        <ToolButton onClick={clearLogs} label="清理常规日志，保留警告与审计">
          <Eraser className="size-3.5" />
          清理常规
        </ToolButton>
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
          {logs.length > 0 && (
            <p className="mt-2.5 text-center font-serif text-[11px] tracking-[2px] text-ink-faint">
              — 一口轻食 · 一单一味 —
            </p>
          )}
        </div>
        <div className="h-0.5 shrink-0 border-x bg-card" />
      </div>
    </section>
  )
}

function LogRowItem({ row, query }: { row: LogEntry | LogRow; query: string }) {
  const [expanded, setExpanded] = useState(false)
  const text = formatLogMsg(row.msg)
  const lines = text.split('\n')
  const summary = lines[0]
  const hasMore = lines.length > 1 || row.msg.length > 120
  const hit = Boolean(query) && row.msg.toLowerCase().includes(query)
  return (
    <div className="receipt-row flex gap-2.5 px-3 leading-[1.7]">
      <span className="mt-0.5 w-[48px] shrink-0 text-[10px] text-ink-faint">{row.ts}</span>
      <span className={cn('mt-0.5 w-[38px] shrink-0 text-[10px] font-semibold', LEVEL_CLASS[row.level] || 'text-muted-foreground')}>{row.level}</span>
      <div className="min-w-0 flex-1">
        <span className={cn('block break-words', hit && 'rounded-[2px] bg-hit px-0.5')}>
          {isOrderSummary(row.msg) ? <OrderSummaryText msg={row.msg} /> : summary}
        </span>
        {hasMore && (
          <button
            type="button"
            className="mt-0.5 inline-flex items-center gap-0.5 text-[10px] text-primary underline"
            aria-expanded={expanded}
            onClick={() => setExpanded((v) => !v)}
          >
            {expanded ? <ChevronDown className="size-3" /> : <ChevronRight className="size-3" />}
            {expanded ? '收起明细' : '展开明细'}
          </button>
        )}
        {expanded && hasMore && (
          <pre className="mt-1 max-h-52 overflow-auto whitespace-pre-wrap break-all rounded border bg-secondary/30 p-1.5 text-[10px] leading-relaxed">{text}</pre>
        )}
      </div>
    </div>
  )
}

function OrderSummaryText({ msg }: { msg: string }) {
  const parts = splitOrderSummary(msg)
  return (
    <span className="block">
      {parts.map((part, i) => (
        <span key={i} className={cn('block', i === 0 && 'font-semibold text-primary-strong')}>{part}</span>
      ))}
    </span>
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

function ToolButton({ onClick, active, label, children }: {
  onClick: () => void
  active?: boolean
  label: string
  children: ReactNode
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      title={label}
      aria-label={label}
      className={cn(
        'inline-flex h-7 items-center gap-1 rounded-[6px] border px-1.5 text-[10px] transition-colors',
        active ? 'border-primary bg-primary-soft text-primary-strong' : 'border-border bg-card text-muted-foreground hover:border-primary hover:text-foreground',
      )}
    >
      {children}
    </button>
  )
}

function cancelReveal(animation: { current: Animation | null }) {
  animation.current?.cancel()
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

function PhoneLogSheet({ reveal }: { reveal: LogReveal }) {
  const { addressInput } = useApp()
  const [phase, setPhase] = useState<'closed' | 'open' | 'closing'>('closed')
  const sheetRef = useRef<HTMLElement>(null)
  const revealAnimation = useRef<Animation | null>(null)
  const handledNonce = useRef(0)

  useEffect(() => () => cancelReveal(revealAnimation), [])

  useEffect(() => {
    if (!addressInput) return
    reveal.openFrom(null)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [addressInput?.id])

  useLayoutEffect(() => {
    if (!reveal.open) return
    const element = sheetRef.current
    if (!element) return
    if (reveal.nonce === handledNonce.current) return
    handledNonce.current = reveal.nonce
    cancelReveal(revealAnimation)
    setPhase('open')
    if (!canAnimate(element) || prefersReducedMotion()) return
    const frames = openFramesCss()
    try {
      revealAnimation.current = element.animate(
        [{ clipPath: frames.from }, { clipPath: frames.to }],
        { duration: REVEAL_TIMING.openMs, easing: REVEAL_TIMING.openEase },
      )
    } catch {
      revealAnimation.current = null
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [reveal.open, reveal.nonce])

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
    cancelReveal(revealAnimation)
    setPhase('closing')
    if (!canAnimate(element) || prefersReducedMotion()) {
      setPhase('closed')
      return
    }
    const animation = element.animate(
      [{ clipPath: startClip }, { clipPath: frames.to }],
      { duration: REVEAL_TIMING.closeMs, easing: REVEAL_TIMING.closeEase },
    )
    revealAnimation.current = animation
    animation.onfinish = () => {
      if (revealAnimation.current === animation) revealAnimation.current = null
      element.style.clipPath = frames.to
      setPhase('closed')
    }
    animation.oncancel = () => {
      if (revealAnimation.current === animation) revealAnimation.current = null
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [reveal.open, phase])

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
        clipPath: phase === 'closed' ? CLOSED_CLIP : undefined,
        visibility: reveal.open || phase !== 'closed' ? 'visible' : 'hidden',
        pointerEvents: phase === 'open' ? 'auto' : 'none',
        willChange: 'clip-path',
        transform: 'translateZ(0)',
        backfaceVisibility: 'hidden',
        contain: 'layout paint style',
      }}
    >
      <div className="flex min-h-0 flex-1 flex-col px-2.5" style={{ paddingTop: 'var(--safe-top)', paddingBottom: 'var(--safe-bottom)' }}>
        <LogConsoleBody />
      </div>
    </section>
  )
}
