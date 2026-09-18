/**
 * 运行日志控制台 = 一张正在打印的小票（Wheat Press 记忆点）：
 * 锯齿顶边 + 等宽时间戳/级别 + 虚线裁切线 + 命中价签黄高亮 + 页脚印章小字。
 * 功能：即输即滤（保留命中高亮与无命中提示）、复制、清空、自动滚动。
 */
import { useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { ArrowDownToLine, Copy, Eraser } from 'lucide-react'
import { Input } from '@/components/ui/input'
import type { AddressInputRequest } from '@/lib/bridge'
import { statusLabel, useApp } from '@/hooks/appContext'
import { cn } from '@/lib/utils'
import { formatLogMsg, isOrderSummary, splitOrderSummary } from '@/lib/format'
import { LOG_REVEAL_ORIGIN, REVEAL_TIMING, canAnimate, closeFramesCss, openFramesCss, prefersReducedMotion } from '@/lib/reveal'
import type { LogReveal } from '@/lib/useLogReveal'

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
 * ``layout === 'phone'`` 时改用**全屏水波层**：点右上角日志按钮，日志直接铺满
 * 视口，由圆形 `clip-path` 从按钮圆心扩散；再次点击按钮反向收回。
 * 没有底部抽屉、把手或高度拖拽。平板/桌面维持原来的并排布局
 * （`LogConsoleBody` 直接铺满容器），不受影响。
 */
export function LogConsole({
  layout = 'desktop',
  reveal,
}: {
  layout?: 'phone' | 'tablet' | 'desktop'
  /** 手机端全屏水波扩散的开合状态（由 App 的 useLogReveal 创建）。 */
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
        历史坑：这行原先是**不换行**的单行 flex，窄屏会被挤爆 —— 「运行日志」没有
        shrink-0，被压到近 0 宽后 CJK 字符只能逐个换行，标题变成竖排。
        现在用 flex-wrap 排两行：标题+状态 / （过滤框 + 工具按钮同排）。
        过滤框在窄屏自适应收窄（flex-1），宽屏才固定 w-44 并靠右。
        全屏层顶部空间有限，头部越紧凑越好，日志区越宽。
      */}
      <div className="flex flex-wrap items-center gap-x-2.5 gap-y-1.5">
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
        {/* 过滤框与按钮同排：原来各占一行，在手机上头部会撑到约 110px，
            放进抽屉后显得很占地方。窄屏下过滤框自适应收窄。 */}
        <div className="flex w-full min-w-0 items-center gap-1.5 sm:ml-auto sm:w-auto">
          <Input
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
            placeholder="过滤日志…"
            className="h-8 min-w-0 flex-1 rounded-[4px] border-border bg-card text-xs sm:h-7 sm:w-44 sm:flex-none"
          />
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

      <div className="receipt mt-2 flex min-h-0 flex-1 flex-col sm:mt-3.5">
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
 * 手机端日志全屏层。
 *
 * 交互（按需求）：
 * - 点右上角日志按钮 / 任务启动 → 全屏面板**瞬间到位**，圆形水波从触发按钮扩散；
 * - 不再有底部抽屉、把手和高度拖拽；日志直接铺满整个视口；
 * - 再次点右上角日志按钮 → 反向水波从按钮圆心收回，露出原来的任务界面；
 * - 收/展全程没有高度过渡和从下往上的翻滚，只有 `clip-path` 圆形变化。
 *
 * 日志按钮由 `LogFab` 固定在右上角，层级高于本面板（z-30），所以展开后不会消失。
 */
function cancelReveal(animation: { current: Animation | null }) {
  animation.current?.cancel()
  animation.current = null
}

/** 读取当前动画中的 clip-path；取不到时回退到给定值。 */
function currentClipPath(element: HTMLElement, fallback: string): string {
  try {
    const value = getComputedStyle(element).clipPath
    if (value && value !== 'none' && value.startsWith('circle(')) return value
  } catch {
    /* 读取失败时用 fallback */
  }
  return fallback
}

/** 关闭后的基础裁剪：半径为 0，面板常驻但不显示、不响应点击。 */
const CLOSED_CLIP = `circle(0px at ${LOG_REVEAL_ORIGIN})`

function PhoneLogSheet({ reveal }: { reveal: LogReveal }) {
  const { addressInput } = useApp()
  const [phase, setPhase] = useState<'closed' | 'open' | 'closing'>('closed')
  const sheetRef = useRef<HTMLElement>(null)
  /** 正在跑的水波动画。只留这一条引用，避免 cancel 时误伤其它 CSS 动画。 */
  const revealAnimation = useRef<Animation | null>(null)
  /** 已经播过扩散动画的那一次 nonce，避免重复渲染时重播。 */
  const handledNonce = useRef(0)

  useEffect(() => () => cancelReveal(revealAnimation), [])

  // 待确认地址输入框就在日志区里：收起时若来了输入请求必须自动弹出，
  // 否则任务会卡在等输入，而用户看不到输入框。
  useEffect(() => {
    if (!addressInput) return
    reveal.openFrom(null)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [addressInput?.id])

  // 展开：面板已经在完整尺寸，只由圆形 clip-path 从按钮圆心扩到全屏。
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
      // 个别 WebView 对 clip-path 关键帧挑剔：动画失败不影响功能，面板已可见
      revealAnimation.current = null
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [reveal.open, reveal.nonce])

  // 收起：反向水波缩回右上角日志按钮，露出原本界面后再卸载。
  useLayoutEffect(() => {
    if (reveal.open) return
    if (phase !== 'open') return
    const element = sheetRef.current
    if (!element) {
      setPhase('closed')
      return
    }
    // 先记录「当前水波半径」再取消旧动画：如果用户展开到一半就点收起，
    // 收起要从当前可见半径继续缩，而不是跳回全屏再缩。
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
      // 动画结束时没有 fill，computed clip-path 会短暂回到基础值；这里直接把
      // 基础值写成 circle(0)，保证从「动画最后一帧」到「React 卸载/隐藏」之间
      // 不会闪出完整日志。
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
      className={cn(
        'fixed inset-0 z-30 flex flex-col bg-background',
        // 收起动画进行中允许点击透传回原界面，避免面板还挡着操作
        phase !== 'open' && 'pointer-events-none',
      )}
      // 面板常驻（关闭时 visibility:hidden + circle(0) 裁剪），这样：
      // 1. 打开时布局/绘制已经预热，不再在第一帧现搭 DOM；
      // 2. 关闭动画结束后基础 clipPath 仍是 circle(0)，不会瞬间恢复成完整日志而闪烁。
      // clipPath / visibility 只做合成层上的裁剪，不再逐帧重排整页。
      style={{
        // 关闭动画期间不能提前设 circle(0)，否则反向水波会从半径 0 开始收缩；
        // 关闭完成时由 onfinish 直接写基础样式，再切到 phase='closed'。
        clipPath: phase === 'closed' ? CLOSED_CLIP : undefined,
        visibility: reveal.open || phase !== 'closed' ? 'visible' : 'hidden',
        pointerEvents: phase === 'open' ? 'auto' : 'none',
        willChange: 'clip-path',
        transform: 'translateZ(0)',
        backfaceVisibility: 'hidden',
        contain: 'layout paint style',
      }}
    >
      <div
        className="flex min-h-0 flex-1 flex-col px-2.5"
        style={{ paddingTop: 'var(--safe-top)', paddingBottom: 'var(--safe-bottom)' }}
      >
        <LogConsoleBody />
      </div>
    </section>
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
