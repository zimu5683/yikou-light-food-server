/**
 * 工作台共享视觉件：流程条、折叠高级设置、状态胶囊、提示卡、确认弹窗。
 * 只表达后端真实提供的能力，不做假步骤或百分比。
 */
import { useId, useState, type ReactNode } from 'react'
import { AlertTriangle, Check, ChevronDown, ChevronRight, Circle, Loader2 } from 'lucide-react'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { cn } from '@/lib/utils'
import type { OperationTone } from '@/lib/operationStatus'

export type FlowStepState = 'todo' | 'active' | 'done' | 'warning' | 'error'

export function FlowStrip({
  steps,
  label = '任务流程',
}: {
  steps: Array<{ key: string; label: string; state: FlowStepState; detail?: string }>
  label?: string
}) {
  return (
    <ol aria-label={label} className="flex min-w-0 items-center gap-1">
      {steps.map((step, index) => (
        <li key={step.key} className="flex min-w-0 items-center gap-1">
          <span
            className={cn(
              'flex h-5 min-w-5 shrink-0 items-center justify-center rounded-full border px-1 text-[10px] font-semibold',
              step.state === 'active' && 'border-primary bg-primary-soft text-primary-strong',
              step.state === 'done' && 'border-success/40 bg-success/10 text-success',
              step.state === 'warning' && 'border-warning/50 bg-warning/10 text-warning',
              step.state === 'error' && 'border-destructive/40 bg-destructive/10 text-destructive',
              step.state === 'todo' && 'border-border bg-card text-ink-faint',
            )}
            title={step.detail || step.label}
          >
            {step.state === 'done' ? <Check className="size-3" /> : index + 1}
          </span>
          <span
            className={cn(
              'truncate text-[11px]',
              step.state === 'active' ? 'font-medium text-foreground' : 'text-muted-foreground',
            )}
          >
            {step.label}
          </span>
          {index < steps.length - 1 && <span className="mx-0.5 h-px w-2 shrink-0 bg-border" />}
        </li>
      ))}
    </ol>
  )
}

export function AdvancedSection({
  id,
  title,
  summary,
  open,
  onOpenChange,
  notice,
  children,
}: {
  id: string
  title: string
  summary: string
  open: boolean
  onOpenChange: (v: boolean) => void
  notice?: string
  children: ReactNode
}) {
  return (
    <section className="mb-3.5 rounded-lg border bg-card">
      <button
        type="button"
        aria-expanded={open}
        aria-controls={id}
        onClick={() => onOpenChange(!open)}
        className="flex w-full items-center gap-2 rounded-lg px-3 py-2.5 text-left transition-colors hover:bg-secondary/50"
      >
        {open ? <ChevronDown className="size-4 shrink-0 text-muted-foreground" /> : <ChevronRight className="size-4 shrink-0 text-muted-foreground" />}
        <span className="min-w-0 flex-1">
          <span className="block text-[13px] font-medium">{title}</span>
          <span className="block truncate text-[11px] text-muted-foreground">{notice || summary}</span>
        </span>
        <span className="shrink-0 text-[11px] text-muted-foreground">{open ? '收起' : '展开'}</span>
      </button>
      {open && (
        <div id={id} role="region" aria-label={title} className="border-t px-3 pb-3 pt-3">
          {notice && (
            <p className="mb-2.5 rounded-md border border-warning/40 bg-warning/10 px-2.5 py-1.5 text-[11px] text-warning">
              {notice}
            </p>
          )}
          {children}
        </div>
      )}
    </section>
  )
}

export function Callout({
  tone = 'info',
  title,
  children,
  action,
}: {
  tone?: 'info' | 'warning' | 'danger' | 'success' | 'neutral'
  title?: string
  children?: ReactNode
  action?: ReactNode
}) {
  return (
    <div
      role={tone === 'danger' ? 'alert' : undefined}
      className={cn(
        'mb-3 rounded-lg border px-3 py-2.5 text-[11px] leading-relaxed',
        tone === 'info' && 'border-primary/30 bg-primary-soft/40 text-foreground',
        tone === 'warning' && 'border-warning/45 bg-warning/10 text-foreground',
        tone === 'danger' && 'border-destructive/45 bg-destructive/5 text-foreground',
        tone === 'success' && 'border-success/40 bg-success/5 text-foreground',
        tone === 'neutral' && 'border-border bg-secondary/40 text-muted-foreground',
      )}
    >
      <div className="flex items-start gap-2">
        {tone === 'danger' || tone === 'warning' ? (
          <AlertTriangle className="mt-0.5 size-3.5 shrink-0" />
        ) : (
          <Circle className="mt-0.5 size-2.5 shrink-0" fill="currentColor" />
        )}
        <div className="min-w-0 flex-1">
          {title && <p className="font-medium">{title}</p>}
          {children}
        </div>
        {action && <div className="shrink-0">{action}</div>}
      </div>
    </div>
  )
}

export function StatusPill({
  label,
  tone = 'neutral',
  live = false,
  title,
}: {
  label: string
  tone?: OperationTone | 'success' | 'warning' | 'danger' | 'neutral'
  live?: boolean
  title?: string
}) {
  const toneClass: Record<string, string> = {
    neutral: 'border-border bg-card text-muted-foreground',
    progress: 'border-primary/40 bg-primary-soft text-primary-strong',
    success: 'border-success/40 bg-success/10 text-success',
    warning: 'border-warning/45 bg-warning/10 text-warning',
    danger: 'border-destructive/40 bg-destructive/10 text-destructive',
    info: 'border-primary/40 bg-primary-soft text-primary-strong',
  }
  return (
    <span
      title={title}
      className={cn(
        'inline-flex h-6 max-w-full items-center gap-1.5 rounded-full border px-2 text-[11px] font-medium',
        toneClass[tone] || toneClass.neutral,
      )}
    >
      <span className={cn('size-1.5 shrink-0 rounded-full bg-current', live && 'led-breathe')} />
      <span className="truncate">{label}</span>
    </span>
  )
}

export function SegmentedControl<T extends string>({
  value,
  onChange,
  options,
  label,
  disabled,
}: {
  value: T
  onChange: (v: T) => void
  options: Array<{ value: T; label: string; description?: string; tone?: 'danger' | 'warning' | 'neutral' }>
  label: string
  disabled?: boolean
}) {
  return (
    <div role="group" aria-label={label} className="grid gap-1.5 sm:grid-cols-3">
      {options.map((option) => {
        const active = option.value === value
        return (
          <button
            key={option.value}
            type="button"
            aria-pressed={active}
            disabled={disabled}
            onClick={() => onChange(option.value)}
            className={cn(
              'min-h-[44px] rounded-md border px-2.5 py-1.5 text-left transition-colors disabled:opacity-50 sm:min-h-[40px]',
              active
                ? option.tone === 'danger'
                  ? 'border-destructive/60 bg-destructive/10 text-destructive'
                  : option.tone === 'warning'
                    ? 'border-warning/50 bg-warning/10 text-warning'
                    : 'border-primary bg-primary-soft text-primary-strong'
                : 'border-border bg-card text-muted-foreground hover:border-primary/60 hover:text-foreground',
            )}
          >
            <span className="block text-xs font-medium">{option.label}</span>
            {option.description && (
              <span className="mt-0.5 block text-[10px] leading-snug opacity-80">{option.description}</span>
            )}
          </button>
        )
      })}
    </div>
  )
}

export function SummaryTile({
  label,
  value,
  tone = 'neutral',
  hint,
}: {
  label: string
  value: number | string
  tone?: 'neutral' | 'success' | 'warning' | 'danger' | 'progress'
  hint?: string
}) {
  return (
    <div className="min-w-0 rounded-md border bg-card px-2.5 py-2">
      <div className="truncate text-[10px] text-muted-foreground" title={hint || label}>{label}</div>
      <div
        className={cn(
          'tabular mt-0.5 text-lg font-semibold leading-none',
          tone === 'success' && 'text-success',
          tone === 'warning' && 'text-warning',
          tone === 'danger' && 'text-destructive',
          tone === 'progress' && 'text-primary',
        )}
      >
        {value}
      </div>
    </div>
  )
}

/** 明确文字确认：只有勾选确认句后主按钮才可用，不靠颜色表达风险。 */
interface ConfirmActionDialogProps {
  open: boolean
  onOpenChange: (v: boolean) => void
  title: string
  description: ReactNode
  /** 必须勾选的明确文字确认句。 */
  acknowledge: string
  confirmLabel: string
  busy?: boolean
  danger?: boolean
  onConfirm: () => void | Promise<void>
  details?: ReactNode
}

export function ConfirmActionDialog(props: ConfirmActionDialogProps) {
  const { open, onOpenChange, busy = false } = props
  return (
    <Dialog open={open} onOpenChange={(next) => { if (!busy) onOpenChange(next) }}>
      {/* key 让每次打开都从“未勾选”开始，不在 effect 里同步 state。 */}
      <ConfirmActionBody key={open ? 'open' : 'closed'} {...props} />
    </Dialog>
  )
}

function ConfirmActionBody({
  onOpenChange, title, description, acknowledge, confirmLabel, busy = false, danger = false, onConfirm, details,
}: ConfirmActionDialogProps) {
  const checkboxId = useId()
  const [checked, setChecked] = useState(false)
  return (
    <DialogContent className="sm:max-w-md rounded-lg">
      <DialogHeader>
        <DialogTitle className="font-serif">{title}</DialogTitle>
        <DialogDescription asChild>
          <div className="space-y-2 text-xs leading-relaxed">
            <div>{description}</div>
            {details}
          </div>
        </DialogDescription>
      </DialogHeader>
      <label
        htmlFor={checkboxId}
        className={cn(
          'flex cursor-pointer items-start gap-2 rounded-md border px-3 py-2.5 text-xs leading-relaxed',
          checked ? 'border-primary bg-primary-soft/50' : 'border-border bg-secondary/40',
        )}
      >
        <input
          id={checkboxId}
          type="checkbox"
          checked={checked}
          disabled={busy}
          onChange={(e) => setChecked(e.target.checked)}
          className="mt-0.5 size-4 shrink-0 accent-[var(--primary)]"
        />
        <span>{acknowledge}</span>
      </label>
      <DialogFooter className="gap-2">
        <Button variant="ghost" className="h-9 text-xs" disabled={busy} onClick={() => onOpenChange(false)}>
          返回检查
        </Button>
        <Button
          className={cn(
            'h-9 rounded-[6px] text-xs',
            danger && 'bg-destructive text-white hover:bg-destructive/90',
          )}
          disabled={!checked || busy}
          onClick={() => void onConfirm()}
        >
          {busy ? <Loader2 className="mr-1 size-3.5 animate-spin" /> : null}
          {busy ? '提交中…' : confirmLabel}
        </Button>
      </DialogFooter>
    </DialogContent>
  )
}
