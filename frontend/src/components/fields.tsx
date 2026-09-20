/**
 * 表单基础件：Field（错误/辅助文案与控件 ARIA 关联）、Stepper、DateField。
 * 三态语义沿用旧版 FormField：neutral / valid / invalid。
 */
import {
  createContext,
  useContext,
  useId,
  useMemo,
  useState,
  type ButtonHTMLAttributes,
  type InputHTMLAttributes,
  type ReactNode,
} from 'react'
import { CalendarIcon, Minus, Plus } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'
import { cn } from '@/lib/utils'
import { formatISO, sameDay, startOfMonth } from '@/lib/format'

export interface FieldState {
  state?: 'neutral' | 'valid' | 'invalid'
  message?: string
}

interface FieldA11y {
  id?: string
  error: boolean
  describedBy?: string
}

const FieldA11yContext = createContext<FieldA11y>({ error: false })

export function Field({
  label,
  htmlFor,
  error,
  okMessage,
  helper,
  children,
  className,
}: {
  label: string
  htmlFor?: string
  error?: string
  okMessage?: string
  /** 常驻辅助文案，保持一句话。 */
  helper?: string
  children: ReactNode
  className?: string
}) {
  const autoId = useId()
  const errorId = `${autoId}-error`
  const helperId = `${autoId}-help`
  const showHelper = Boolean(error || okMessage || helper)
  const describedBy = error ? errorId : (okMessage || helper) ? helperId : undefined
  return (
    <div className={cn('mb-3', className)}>
      <label htmlFor={htmlFor} className="mb-1 block text-xs font-medium">
        {label}
        {helper && !error ? <span className="sr-only">，{helper}</span> : null}
      </label>
      <FieldA11yContext.Provider value={{ id: htmlFor, error: Boolean(error), describedBy }}>
        {children}
      </FieldA11yContext.Provider>
      {error ? (
        <p id={errorId} role="alert" className="mt-1 text-[11px] text-destructive">
          {error}
        </p>
      ) : showHelper && (
        <p id={helperId} className="mt-1 text-[11px] text-muted-foreground">
          {okMessage ?? helper}
        </p>
      )}
    </div>
  )
}

function useFieldA11y() {
  return useContext(FieldA11yContext)
}

export function TextInput({
  state,
  className,
  ...props
}: InputHTMLAttributes<HTMLInputElement> & { state?: FieldState['state'] }) {
  const a11y = useFieldA11y()
  const invalid = Boolean(a11y.error) || state === 'invalid'
  return (
    <Input
      aria-invalid={invalid || undefined}
      aria-describedby={a11y.describedBy}
      className={cn(
        'h-[42px] rounded-[6px] border-transparent bg-secondary text-[13px] transition-colors focus-visible:bg-card sm:h-[36px]',
        state === 'valid' &&
          'border-success/50 bg-success/5 focus-visible:border-success focus-visible:ring-success/20',
        invalid && 'border-destructive bg-destructive/5',
        className,
      )}
      {...props}
    />
  )
}

export function GhostButton({
  className,
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement>) {
  return (
    <Button
      variant="outline"
      className={cn(
        'h-[42px] shrink-0 rounded-[6px] border-border bg-card px-3 text-xs text-foreground hover:border-primary hover:bg-card hover:text-primary-strong sm:h-[36px]',
        className,
      )}
      {...props}
    />
  )
}

/** 数字步进器：可留空（留空表示“全部”），左右 − ＋，中间等宽数字。 */
export function Stepper({
  value,
  onChange,
  min = 1,
  max = 9999,
  invalid,
  allowEmpty = true,
  ariaLabel = '待处理订单数',
  id,
}: {
  value: number | null
  onChange: (v: number | null) => void
  min?: number
  max?: number
  invalid?: boolean
  allowEmpty?: boolean
  ariaLabel?: string
  id?: string
}) {
  const a11y = useFieldA11y()
  const [draft, setDraft] = useState<string | null>(null)
  const clamp = (v: number) => Math.min(max, Math.max(min, Math.trunc(v) || min))
  const commit = (raw: string) => {
    const parsed = Number.parseInt(raw, 10)
    if ((raw.trim() === '' || Number.isNaN(parsed)) && allowEmpty) {
      onChange(null)
    } else {
      onChange(clamp(Number.isNaN(parsed) ? min : parsed))
    }
    setDraft(null)
  }
  const step = (delta: number) => {
    const base = draft !== null ? Number.parseInt(draft, 10) : value
    if (delta < 0 && (base === null || base === undefined || Number.isNaN(base))) {
      onChange(null)
      setDraft(null)
      return
    }
    const numeric = base && !Number.isNaN(base) ? base : min
    let next: number | null
    if (base === null || base === undefined || Number.isNaN(base)) {
      next = delta > 0 ? min : null
    } else if (delta < 0 && allowEmpty && numeric <= min) {
      next = null
    } else {
      next = clamp(numeric + delta)
    }
    onChange(next)
    setDraft(null)
  }
  const invalidState = Boolean(a11y.error) || Boolean(invalid)
  return (
    <div
      className={cn(
        'inline-flex items-center overflow-hidden rounded-[6px] border bg-card',
        invalidState && 'border-destructive',
      )}
    >
      <button
        type="button"
        aria-label="减少"
        className="h-[42px] w-[42px] text-[15px] text-muted-foreground hover:bg-secondary hover:text-foreground sm:h-[36px] sm:w-[34px]"
        onClick={() => step(-1)}
      >
        <Minus className="mx-auto size-3.5" />
      </button>
      <input
        id={id}
        aria-label={ariaLabel}
        aria-invalid={invalidState || undefined}
        aria-describedby={a11y.describedBy}
        inputMode="numeric"
        className="tabular h-[42px] w-[42px] border-x bg-transparent text-center font-mono text-[13px] outline-none sm:h-[36px]"
        value={draft ?? (value === null ? '' : String(value))}
        onChange={(e) => {
          const raw = e.target.value.replace(/[^0-9]/g, '').slice(0, 4)
          setDraft(raw)
          if (raw !== '') onChange(clamp(Number.parseInt(raw, 10)))
          else if (allowEmpty) onChange(null)
        }}
        onBlur={() => {
          if (draft !== null) commit(draft)
        }}
        onKeyDown={(e) => {
          if (e.key === 'Enter') {
            if (draft !== null) commit(draft)
            ;(e.target as HTMLInputElement).blur()
          }
        }}
      />
      <button
        type="button"
        aria-label="增加"
        className="h-[42px] w-[42px] text-[15px] text-muted-foreground hover:bg-secondary hover:text-foreground sm:h-[36px] sm:w-[34px]"
        onClick={() => step(1)}
      >
        <Plus className="mx-auto size-3.5" />
      </button>
    </div>
  )
}

const WEEKDAYS = ['日', '一', '二', '三', '四', '五', '六'] as const

/** 日期选择：只允许今天或过去日期，可清空；小屏自动避免溢出屏幕。 */
export function DateField({
  value,
  onChange,
  invalid,
  label = '选择日期',
}: {
  value: string
  onChange: (iso: string) => void
  invalid?: boolean
  label?: string
}) {
  const a11y = useFieldA11y()
  const today = useMemo(() => {
    const now = new Date()
    return new Date(now.getFullYear(), now.getMonth(), now.getDate())
  }, [])
  const [viewMonth, setViewMonth] = useState<Date>(() => {
    const parsed = value ? new Date(`${value}T00:00:00`) : today
    return Number.isNaN(parsed.getTime()) ? today : startOfMonth(parsed)
  })
  const [open, setOpen] = useState(false)

  const selected = useMemo(() => {
    const parsed = value ? new Date(`${value}T00:00:00`) : null
    return parsed && !Number.isNaN(parsed.getTime()) ? parsed : null
  }, [value])

  const cells = useMemo(() => {
    const first = startOfMonth(viewMonth)
    const startWeekday = first.getDay()
    const daysInMonth = new Date(viewMonth.getFullYear(), viewMonth.getMonth() + 1, 0).getDate()
    const list: (Date | null)[] = Array.from({ length: startWeekday }, () => null)
    for (let day = 1; day <= daysInMonth; day += 1) {
      list.push(new Date(viewMonth.getFullYear(), viewMonth.getMonth(), day))
    }
    return list
  }, [viewMonth])

  const canGoNext = startOfMonth(viewMonth) < startOfMonth(today)
  const invalidState = Boolean(a11y.error) || Boolean(invalid)

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <div className="flex flex-1 gap-1.5">
        <PopoverTrigger asChild>
          <button
            type="button"
            aria-label={label}
            aria-haspopup="dialog"
            aria-expanded={open}
            aria-invalid={invalidState || undefined}
            aria-describedby={a11y.describedBy}
            className={cn(
              'flex h-[42px] flex-1 items-center rounded-[6px] border border-transparent bg-secondary px-2.5 text-left text-[13px] transition-colors hover:bg-secondary/80 sm:h-[36px]',
              invalidState && 'border-destructive bg-destructive/5',
              open && 'border-primary bg-card',
            )}
          >
            <CalendarIcon className="mr-2 size-3.5 text-muted-foreground" />
            <span className={cn('tabular', !value && 'text-ink-faint')}>
              {value || '留空默认今天'}
            </span>
          </button>
        </PopoverTrigger>
        {/* 避免旧版“强制向下”溢出屏幕：允许碰撞翻转；内容自身可滚动，键盘弹出时仍可操作。 */}
        <PopoverContent
          className="max-h-[min(70dvh,420px)] w-[min(280px,calc(100vw-24px))] overflow-y-auto rounded-lg border bg-card p-3"
          align="start"
          side="bottom"
          sideOffset={4}
          avoidCollisions
          collisionPadding={8}
        >
          <div className="mb-2 flex items-center justify-between">
            <button
              type="button"
              aria-label="上一月"
              className="grid size-8 place-items-center rounded text-muted-foreground hover:bg-secondary hover:text-foreground"
              onClick={() => setViewMonth(new Date(viewMonth.getFullYear(), viewMonth.getMonth() - 1, 1))}
            >
              ‹
            </button>
            <span className="text-[13px] font-medium">
              {viewMonth.getFullYear()} 年 {viewMonth.getMonth() + 1} 月
            </span>
            <button
              type="button"
              aria-label="下一月"
              disabled={!canGoNext}
              className="grid size-8 place-items-center rounded text-muted-foreground hover:bg-secondary hover:text-foreground disabled:opacity-30"
              onClick={() => setViewMonth(new Date(viewMonth.getFullYear(), viewMonth.getMonth() + 1, 1))}
            >
              ›
            </button>
          </div>
          <div className="grid grid-cols-7 gap-y-1 text-center">
            {WEEKDAYS.map((d) => (
              <span key={d} className="py-1 text-[11px] text-muted-foreground">
                {d}
              </span>
            ))}
            {cells.map((date, i) => {
              if (!date) return <span key={`empty-${i}`} />
              const future = date > today
              const isSelected = selected ? sameDay(date, selected) : false
              return (
                <button
                  key={date.toISOString()}
                  type="button"
                  disabled={future}
                  aria-label={`${date.getFullYear()}-${date.getMonth() + 1}-${date.getDate()}`}
                  aria-pressed={isSelected}
                  onClick={() => {
                    onChange(formatISO(date))
                    setOpen(false)
                  }}
                  className={cn(
                    'tabular mx-auto grid size-8 place-items-center rounded-[6px] font-mono text-xs',
                    future && 'text-ink-faint/40',
                    !future && !isSelected && 'hover:bg-secondary',
                    isSelected && 'bg-primary font-semibold text-primary-foreground',
                  )}
                >
                  {date.getDate()}
                </button>
              )
            })}
          </div>
          <div className="mt-2 flex flex-wrap justify-between gap-2 border-t pt-2">
            <Button
              variant="ghost"
              size="sm"
              className="h-8 text-xs text-muted-foreground"
              onClick={() => {
                onChange('')
                setOpen(false)
              }}
            >
              清空
            </Button>
            <span className="self-center text-[11px] text-muted-foreground">
              仅可选择今天或过去日期
            </span>
          </div>
        </PopoverContent>
      </div>
    </Popover>
  )
}
