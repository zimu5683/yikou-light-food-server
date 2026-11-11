/**
 * 云同步「核对并补传」面板。
 *
 * 用途（2026-11-10 需求）：上传完成后云端可能「看不到」这批餐，而账本已经记了
 * 本批已同步锚点，直接重新预览只会得到「计划不变」。这里先**只读核对**最近一次
 * 上传在云端缺哪些订单（不写云端/账本），确认后再调补传接口——后端只补缺失的
 * 格子，已入账的餐不会重复加，协作者写的 0 不会覆盖。
 *
 * 前端职责只有展示与互斥：所有安全闸门在后端（一次性核对令牌、写前只读复核、
 * 逐格回读校验、防重复闸门）。这里绝不解析后端文本、不自造时间/统计。
 */
import { useEffect, useMemo, useRef, useState } from 'react'
import { ChevronDown, ChevronRight, Loader2 } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Callout, ConfirmActionDialog, StatusPill } from '@/components/WorkspaceUI'
import { useApp } from '@/hooks/appContext'
import {
  api,
  type WpsReuploadCheckResult,
  type WpsReuploadOrder,
  type WpsReuploadTable,
  type WpsReuploadUploadResult,
} from '@/lib/bridge'
import { classifyRequestError } from '@/lib/requestError'
import {
  reuploadCanSubmit,
  reuploadHeadline,
  reuploadInitialSelection,
  reuploadOrderLine,
  reuploadOrderMissingText,
  reuploadOrderStatusText,
  reuploadSelectedCount,
  reuploadSelectable,
  reuploadSheetStatusText,
  reuploadSheetTone,
  reuploadSummaryText,
  reuploadTokenFresh,
  reuploadWriteStatusText,
} from '@/lib/reupload'
import { SingleFlightGate } from '@/lib/singleFlight'
import { cn } from '@/lib/utils'

type Tone = 'info' | 'success' | 'warning' | 'danger' | 'neutral'

interface PanelMessage {
  tone: Tone
  text: string
}

export function ReuploadPanel({ disabled = false }: { disabled?: boolean }) {
  const { operationActive } = useApp()
  const [phase, setPhase] = useState<'idle' | 'checking' | 'checked' | 'uploading' | 'done'>('idle')
  const [check, setCheck] = useState<WpsReuploadCheckResult | null>(null)
  const [selected, setSelected] = useState<string[]>([])
  const [result, setResult] = useState<WpsReuploadUploadResult | null>(null)
  const [message, setMessage] = useState<PanelMessage | null>(null)
  const [expanded, setExpanded] = useState<Record<string, boolean>>({})
  const [confirmOpen, setConfirmOpen] = useState(false)
  const [now, setNow] = useState(() => Date.now())
  const checkGate = useRef(new SingleFlightGate())
  const uploadGate = useRef(new SingleFlightGate())

  // 令牌有效期按服务端 expires_at 计算；定时刷新让"已过期"及时反映到按钮上。
  useEffect(() => {
    if (!check?.check_id) return undefined
    const timer = setInterval(() => setNow(Date.now()), 10_000)
    return () => clearInterval(timer)
  }, [check?.check_id])

  const busy = phase === 'checking' || phase === 'uploading'
  const locked = disabled || operationActive
  const selectedCount = useMemo(
    () => reuploadSelectedCount(check, selected),
    [check, selected],
  )
  const freshness = reuploadTokenFresh(check, now)
  const canSubmit = reuploadCanSubmit(check, selected, now)
  const writeableCheck = check?.status === 'check_ready'

  async function onCheck() {
    if (locked || busy) return
    const token = checkGate.current.begin()
    if (token === null) return
    setPhase('checking')
    setMessage(null)
    setResult(null)
    try {
      const data = await api().wps_reupload_check()
      if (!checkGate.current.isCurrent(token)) return
      setCheck(data)
      setSelected(reuploadInitialSelection(data))
      setExpanded({})
      setPhase('checked')
      if (!data.ok) {
        setMessage({ tone: 'warning', text: data.reason || '核对未完成，请查看下方说明' })
      } else if (data.status === 'noop') {
        setMessage({ tone: 'success', text: '上次上传的订单都已在云端，无需补传。' })
      }
    } catch (error) {
      if (!checkGate.current.isCurrent(token)) return
      setCheck(null)
      setResult(null)
      setPhase('idle')
      setMessage({
        tone: 'danger',
        text: `核对失败：${classifyRequestError(error).detail}（只读入口，失败不代表云端被改动）`,
      })
    } finally {
      checkGate.current.finish(token)
      setPhase((current) => (current === 'checking' ? 'idle' : current))
    }
  }

  function toggleSheet(sheet: string) {
    setSelected((current) =>
      current.includes(sheet) ? current.filter((item) => item !== sheet) : [...current, sheet],
    )
  }

  function requestUpload() {
    if (!canSubmit) {
      setMessage({
        tone: 'warning',
        text: freshness.fresh ? '请先勾选至少一张需要补传的表。' : freshness.reason,
      })
      return
    }
    setConfirmOpen(true)
  }

  async function performUpload() {
    const checkId = check?.check_id
    if (!checkId) return
    const token = uploadGate.current.begin()
    if (token === null) return
    setPhase('uploading')
    setMessage(null)
    try {
      const data = await api().wps_reupload_upload(checkId, selected)
      if (!uploadGate.current.isCurrent(token)) return
      setResult(data)
      setPhase('done')
      const headline = reuploadHeadline(data)
      setMessage({
        tone: headline.tone,
        text: data.reason || headline.nextStep || headline.title,
      })
      // 核对令牌已消费：清掉旧核对结果，避免继续展示过期的「需补传」；
      // 结果卡里的「只读核对」会拿最新云端状态。
      setCheck(null)
      setSelected([])
    } catch (error) {
      if (!uploadGate.current.isCurrent(token)) return
      setMessage({
        tone: 'warning',
        text: `补传请求结果未知：${classifyRequestError(error).detail}。请先看下方结果，必要时重新核对云端后再补。`,
      })
    } finally {
      uploadGate.current.finish(token)
      setPhase((current) => (current === 'uploading' ? 'done' : current))
      setConfirmOpen(false)
    }
  }

  const headline = reuploadHeadline(result)
  const tone: Tone = message?.tone ?? 'neutral'

  return (
    <section className="mb-3.5 rounded-lg border bg-card p-3" aria-label="核对并补传">
      <div className="flex flex-wrap items-center gap-2">
        <h3 className="text-[13px] font-semibold">核对并补传</h3>
        <StatusPill
          label={headline.title}
          tone={result ? (headline.tone === 'success' ? 'success' : headline.tone === 'danger' ? 'danger' : 'warning') : check ? 'info' : 'neutral'}
        />
        {freshness.fresh && check?.expires_at && (
          <span className="text-[10px] text-muted-foreground">
            核对有效期至 {String(check.expires_at).replace('T', ' ')}
          </span>
        )}
      </div>
      <p className="mt-1 text-[11px] leading-relaxed text-muted-foreground">
        上传后可以选表再传：先只读核对云端缺哪些订单，确认后<strong className="text-foreground">只补缺失的格子</strong>。已入账的餐不会重复加，协作者写进日期格的 0 不会覆盖。
      </p>

      {message && (
        <div className="mt-2">
          <Callout tone={tone}>{message.text}</Callout>
        </div>
      )}

      <div className="mt-2 flex flex-wrap gap-2">
        <Button
          variant={check ? 'outline' : 'default'}
          size="sm"
          disabled={locked || busy}
          onClick={() => void onCheck()}
        >
          {phase === 'checking' ? <Loader2 className="mr-1 size-3.5 animate-spin" /> : null}
          {phase === 'checking' ? '核对中…' : check ? '重新核对' : '只读核对'}
        </Button>
        {writeableCheck && (
          <Button size="sm" disabled={locked || busy || !canSubmit} onClick={requestUpload}>
            {phase === 'uploading' ? <Loader2 className="mr-1 size-3.5 animate-spin" /> : null}
            {phase === 'uploading' ? '补传中…' : `补传所选表（${selectedCount} 单）`}
          </Button>
        )}
      </div>

      {check && (
        <div className="mt-2.5">
          <p className="text-[11px] font-medium text-foreground">{reuploadSummaryText(check)}</p>
          {!freshness.fresh && check.status === 'check_ready' && (
            <p className="mt-1 text-[11px] text-warning">{freshness.reason}</p>
          )}
          <div className="mt-1.5 space-y-1.5">
            {(check.tables ?? []).map((table) => (
              <TableRow
                key={table.sheet}
                table={table}
                selected={selected.includes(table.sheet)}
                locked={locked || busy}
                expanded={Boolean(expanded[table.sheet])}
                onToggle={() => toggleSheet(table.sheet)}
                onExpand={() => setExpanded((current) => ({
                  ...current, [table.sheet]: !current[table.sheet],
                }))}
              />
            ))}
          </div>
        </div>
      )}

      {result && (
        <div className="mt-2.5 rounded-md border bg-secondary/20 px-2.5 py-2">
          <p className="text-[11px] font-medium text-foreground">{headline.title}</p>
          {headline.nextStep && (
            <p className="mt-0.5 text-[11px] text-muted-foreground">{headline.nextStep}</p>
          )}
          <div className="mt-1 space-y-0.5">
            {(result.tables ?? []).map((table) => (
              <p key={table.sheet} className="text-[11px] text-muted-foreground">
                <span className="text-foreground">{table.sheet}</span>：{reuploadWriteStatusText(table)}
                {Number(table.counts?.to_resend ?? 0) > 0
                  ? `（计划补 ${table.counts.to_resend} 单）`
                  : ''}
              </p>
            ))}
          </div>
          {result.failed_sheets?.length ? (
            <p className="mt-1 text-[11px] text-destructive">
              未完成的表：{result.failed_sheets.join('、')}（可看后端日志或重新核对）
            </p>
          ) : null}
        </div>
      )}

      <ConfirmActionDialog
        open={confirmOpen}
        onOpenChange={setConfirmOpen}
        title="确认补传云文档"
        description="服务端会再次只读核对云端与上传记录；任何变化都会拒绝补传且一个格子都不写。"
        details={check ? (
          <div className="space-y-1 text-[11px]">
            <p>目标日期：{check.target_date || '—'}</p>
            <p>写入目标：{check.test_mode ? '测试副本' : '正式目标'}</p>
            {(check.tables ?? [])
              .filter((table) => selected.includes(table.sheet))
              .map((table) => (
                <p key={table.sheet}>
                  {table.sheet}：补 {table.counts?.to_resend ?? 0} 单
                  （已在云端 {table.counts?.present ?? 0} 单
                  {Number(table.counts?.manual ?? 0) > 0
                    ? `，无法判定 ${table.counts.manual} 单不写`
                    : ''}
                  ）
                </p>
              ))}
          </div>
        ) : null}
        acknowledge="我确认只补缺失的格子，并已核对目标日期与测试/正式目标"
        confirmLabel="确认补传"
        busy={phase === 'uploading'}
        danger={check?.test_mode !== true}
        onConfirm={performUpload}
      />
    </section>
  )
}

function TableRow({
  table, selected, locked, expanded, onToggle, onExpand,
}: {
  table: WpsReuploadTable
  selected: boolean
  locked: boolean
  expanded: boolean
  onToggle: () => void
  onExpand: () => void
}) {
  const selectable = reuploadSelectable(table)
  const counts = table.counts
  return (
    <div className={cn(
      'rounded-md border px-2.5 py-1.5 text-[11px]',
      table.status === 'blocked' ? 'border-destructive/30 bg-destructive/5' : 'bg-secondary/20',
    )}>
      <div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1">
        <input
          type="checkbox"
          className="size-3.5 shrink-0 accent-primary"
          checked={selectable && selected}
          disabled={!selectable || locked}
          onChange={onToggle}
          aria-label={`选择 ${table.sheet}`}
        />
        <span className="font-medium text-foreground">{table.sheet}</span>
        <StatusPill
          label={reuploadSheetStatusText(table.status)}
          tone={reuploadSheetTone(table.status) === 'info' ? 'progress' : reuploadSheetTone(table.status) as 'success' | 'warning' | 'danger' | 'neutral'}
        />
        <span className="text-muted-foreground">
          需补传 {counts?.to_resend ?? 0} · 已在云端 {counts?.present ?? 0}
          {Number(counts?.manual ?? 0) > 0 ? ` · 无法判定 ${counts.manual}` : ''}
        </span>
        <button
          type="button"
          className="ml-auto flex items-center gap-0.5 text-[10px] text-muted-foreground hover:text-foreground"
          onClick={onExpand}
        >
          {expanded ? <ChevronDown className="size-3.5" /> : <ChevronRight className="size-3.5" />}
          {expanded ? '收起订单' : `查看订单（${counts?.total ?? table.orders.length}）`}
        </button>
      </div>
      {table.reason && (
        <p className={cn('mt-1', table.status === 'blocked' ? 'text-destructive' : 'text-warning')}>
          {table.reason}
        </p>
      )}
      {expanded && (
        <ul className="mt-1.5 max-h-56 space-y-1 overflow-auto border-t pt-1.5">
          {table.orders.map((order, index) => (
            <OrderLine key={`${order.order_no}-${order.slot}-${index}`} order={order} />
          ))}
          {table.orders.length === 0 && (
            <li className="text-muted-foreground">这次上传没有订单明细</li>
          )}
        </ul>
      )}
      {expanded && table.warnings.length > 0 && (
        <ul className="mt-1 list-disc space-y-0.5 pl-4 text-warning">
          {table.warnings.map((warning, index) => (
            <li key={`${warning}-${index}`}>{warning}</li>
          ))}
        </ul>
      )}
    </div>
  )
}

function OrderLine({ order }: { order: WpsReuploadOrder }) {
  const isManual = order.status === 'manual'
  return (
    <li className={cn('break-words', isManual ? 'text-warning' : 'text-muted-foreground')}>
      <span className="text-foreground">{reuploadOrderLine(order) || '（缺订单号/姓名）'}</span>
      <span className="ml-1">{reuploadOrderStatusText(order.status)}</span>
      {order.status !== 'present' && <span className="ml-1">· 缺：{reuploadOrderMissingText(order)}</span>}
      <span className="ml-1">
        （云端 {order.actual_total === null ? '无此单' : order.actual_total} / 期望 {order.expected_total}
        {order.actual_row > 0 ? `，第 ${order.actual_row} 行` : ''}）
      </span>
      {order.reason && <span className="ml-1 text-warning">：{order.reason}</span>}
    </li>
  )
}
