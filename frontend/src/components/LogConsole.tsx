/**
 * 运行日志控制台 = 一张正在打印的小票（Wheat Press 记忆点）：
 * 锯齿顶边 + 等宽时间戳/级别 + 虚线裁切线 + 命中价签黄高亮 + 页脚印章小字。
 * 功能：即输即滤（保留命中高亮与无命中提示）、复制、清空、自动滚动。
 */
import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { ArrowDownToLine, Copy, Eraser } from 'lucide-react'
import { Input } from '@/components/ui/input'
import type { AddressInputRequest } from '@/lib/bridge'
import { statusLabel, useApp } from '@/hooks/appContext'
import { cn } from '@/lib/utils'
import { formatLogMsg, isOrderSummary, splitOrderSummary } from '@/lib/format'
import type { LogSheetDrag } from '@/lib/useLogSheetDrag'

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
  drag,
  onHeightChange,
}: {
  layout?: 'phone' | 'tablet' | 'desktop'
  /** 手机端拖拽状态机（由 App 通过 useLogSheetDrag 创建并共享给操作栏按钮）。 */
  drag?: LogSheetDrag
  /** 上报抽屉当前高度，App 用它给主内容区留白（避免挡住底部按钮）。 */
  onHeightChange?: (px: number) => void
}) {
  return (
    <>
      {(layout !== 'phone' || !drag) && <LogConsoleBody />}
      {layout === 'phone' && drag && <PhoneSheetHost drag={drag} onHeightChange={onHeightChange} />}
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
        抽屉里头部越矮越好，因为收起时露出的就是这部分。
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
/**
 * 手机端日志「底部抽屉」。
 *
 * 交互（按需求）：
 * - 点底部操作栏里的「日志」→ 从下往上弹出；
 * - 点上方非日志区域（遮罩）→ 退回底部；
 * - 拖顶部把手 → **自由调节到任意高度**（不做吸附），仅夹在「收起」与
 *   「视口高度 − 顶部留白」之间；
 * - 拖到接近底部即收起；其它高度原样记住（含刷新后）。
 *
 * 三处踩过的坑，都是这次特意改掉的：
 * 1. 高度曾经用 `vh` 表示，而拖拽换算用 `innerHeight` —— 手机上这两个值不相等，
 *    导致「拖到一半看不到内容」「拖到最大时把手被顶出屏幕」。现在全程像素。
 * 2. 顶部留白（topGapPx）保证拖到最大也**不会铺满全屏**：下巴留着可以拖回来，
 *    把手也始终可见。
 * 3. 抽屉会盖住任务面板，所以高度通过 onHeightChange 上报给 App，
 *    由 App 给主内容区留出等高的底部内边距，按钮不会被压住点不到。
 */
/**
 * 手机端日志「底部抽屉」——**纯展示**：开合、高度、拖拽都由 `useLogSheetDrag`
 * 在 App 层统一管理，这样操作栏里的「日志」按钮和这里的把手用的是同一份状态。
 *
 * 高度与顶部留白由几何模块保证：
 * - 拖到最大也不铺满全屏（`topGapPx`），把手始终可见、下巴留着能拖回来；
 * - 全程像素，不再混用 `vh` 与 `innerHeight`（混用会导致「拖到一半看不到内容」）；
 * - 拖到哪停到哪，不做吸附。
 */
/**
 * 把抽屉高度上报给 App。
 *
 * 刻意用 effect 而不是在渲染期间直接调用父组件的 setState：后者是 React 反模式
 * （渲染必须纯净），在并发渲染下可能重复调用或与渲染结果不一致。卸载时归零，
 * 免得切到平板/桌面后主内容区还留着一段空白。
 */
function PhoneSheetHost({
  drag,
  onHeightChange,
}: {
  drag: LogSheetDrag
  onHeightChange?: (px: number) => void
}) {
  const height = drag.height
  useEffect(() => {
    onHeightChange?.(height)
  }, [height, onHeightChange])
  useEffect(() => () => onHeightChange?.(0), [onHeightChange])

  return <PhoneLogSheet drag={drag} />
}

function PhoneLogSheet({ drag }: { drag: LogSheetDrag }) {
  const { addressInput } = useApp()
  const { open, height, dragging, setOpen, onPointerDown, onPointerMove, onPointerUp } = drag

  // 待确认地址输入框就在日志区里：收起时若来了输入请求必须自动弹出，
  // 否则任务会卡在等输入，而用户看不到输入框。
  useEffect(() => {
    if (addressInput) setOpen(true)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [addressInput?.id])

  return (
    <>
      {open && (
        <button
          type="button"
          aria-label="收起日志"
          onClick={() => setOpen(false)}
          className="fixed inset-x-0 top-0 z-30 bg-black/20"
          style={{ bottom: height }}
        />
      )}
      <section
        role="dialog"
        aria-label="运行日志"
        aria-modal={open}
        style={{ height }}
        className={cn(
          'fixed inset-x-0 bottom-0 z-40 flex flex-col border-t bg-background',
          'pb-[var(--safe-bottom)]',
          !dragging && 'transition-[height] duration-200 ease-out',
        )}
      >
        {/* 把手：始终可见（因为顶部留白，最大高度也到不了屏幕外） */}
        <div
          onPointerDown={onPointerDown}
          onPointerMove={onPointerMove}
          onPointerUp={onPointerUp}
          onPointerCancel={onPointerUp}
          className="flex h-7 shrink-0 cursor-grab touch-none select-none items-center justify-center active:cursor-grabbing"
        >
          <span className="h-1 w-12 rounded-full bg-border" />
        </div>
        <div className="flex min-h-0 flex-1 flex-col px-2.5 pb-1.5">
          {/* 收起时隐藏内容（保留 DOM 以免日志滚动位置丢失），并禁止聚焦 */}
          <div className={cn('flex min-h-0 flex-1 flex-col', !open && 'invisible')} inert={!open}>
            <LogConsoleBody />
          </div>
        </div>
      </section>
    </>
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
