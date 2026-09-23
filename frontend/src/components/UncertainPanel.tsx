/**
 * 闪时送「未解决的不确定记录」只读核对面板。
 *
 * 为什么做成折叠面板：
 * - 未决记录明细只有管理员能看（接口 403 admin_only），普通用户只看得到阻断说明；
 * - 记录可能很多（例如 62 条），默认折叠，**展开时才调一次 sss_uncertain_records**，
 *   之后只有用户显式点「刷新」或只读核对 worker 结束才重新拉取，绝不轮询；
 * - 「只读核对站内订单」需要密码（从闪时送表单传入；为空时禁用并提示先填密码）；
 * - 解除是唯一写入口，但永不发 POST：必须勾选记录、note ≥ 4、confirm 与 decision
 *   完全一致，再经过二次确认弹窗；提交中禁用重复点击（单飞闸门）。
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import { toast } from 'sonner'
import { ChevronDown, ChevronRight } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Callout, ConfirmActionDialog } from '@/components/WorkspaceUI'
import { useApp } from '@/hooks/appContext'
import {
  api,
  isApiReady,
  type SssReviewStartResult,
  type SssUncertainDecision,
  type SssUncertainResolveResult,
  type SssUncertainState,
} from '@/lib/bridge'
import { classifyRequestError } from '@/lib/requestError'
import { SingleFlightGate } from '@/lib/singleFlight'
import {
  SSS_DECISION_SPECS,
  absentEvidence,
  buildUncertainResolvePayload,
  resolveGate,
  reviewStartGate,
  reviewStartView,
  uncertainResolveView,
  uncertainStateView,
  type SssResolveOutcomeView,
} from '@/lib/uncertainReview'
import { cn } from '@/lib/utils'

const PANEL_ID = 'sss-uncertain-panel'

export function UncertainPanel({ password, isAdmin }: { password: string; isAdmin: boolean }) {
  const { operationActive } = useApp()
  const [open, setOpen] = useState(false)
  const [state, setState] = useState<SssUncertainState | null>(null)
  const [loading, setLoading] = useState(false)
  const [loadError, setLoadError] = useState('')
  const [reviewBusy, setReviewBusy] = useState(false)
  const [reviewMessage, setReviewMessage] = useState<{ tone: 'info' | 'warning' | 'danger'; text: string } | null>(null)
  // 只读核对 worker 结束后自动刷新一次（不是轮询：由已有的 operationActive 状态驱动）。
  const awaitingReview = useRef(false)
  const [selected, setSelected] = useState<string[]>([])
  const [decision, setDecision] = useState<SssUncertainDecision | ''>('')
  const [note, setNote] = useState('')
  const [confirmText, setConfirmText] = useState('')
  const [confirmOpen, setConfirmOpen] = useState(false)
  const [resolveBusy, setResolveBusy] = useState(false)
  const [resolveOutcome, setResolveOutcome] = useState<SssResolveOutcomeView | null>(null)
  const [formError, setFormError] = useState('')
  const gateRef = useRef(new SingleFlightGate())

  const view = uncertainStateView(state)
  const startGate = reviewStartGate({ isAdmin, password, busy: reviewBusy, operationActive })
  const decisionSpec = decision ? SSS_DECISION_SPECS[decision] : null
  // 所选记录必须都在新鲜快照里被判成「站内未查到」，才允许选/提交解除。
  const absent = absentEvidence(selected, view.review)
  const resolveState = resolveGate({
    isAdmin: Boolean(isAdmin),
    decision,
    recordIds: selected,
    note,
    confirm: confirmText,
    review: view.review,
    busy: resolveBusy || operationActive,
  })

  const load = useCallback(async () => {
    if (!isAdmin) return
    if (!isApiReady()) {
      setLoadError('后端尚未连接：无法只读读取未决记录。')
      return
    }
    setLoading(true)
    setLoadError('')
    try {
      const next = await api().sss_uncertain_records()
      setState(next)
      const ids = (next.records || []).map((row) => String(row.journal_id || ''))
      setSelected((prev) => prev.filter((id) => ids.includes(id)))
    } catch (error) {
      const issue = classifyRequestError(error)
      const detail = issue.detail || issue.title
      setLoadError(detail)
      // 不静默失败：内联错误 + 全局 toast 各给一次。
      toast.error('未决记录读取失败：' + detail, { duration: 8000 })
    } finally {
      setLoading(false)
    }
  }, [isAdmin])

  const wasActive = useRef(operationActive)
  useEffect(() => {
    const previous = wasActive.current
    wasActive.current = operationActive
    if (!previous || operationActive || !awaitingReview.current) return
    awaitingReview.current = false
    if (!open) return
    // 放到微任务里执行：effect 内同步 setState 会触发 React Compiler 告警。
    queueMicrotask(() => { void load() })
  }, [operationActive, open, load])

  function toggle() {
    const next = !open
    setOpen(next)
    // 只有第一次展开才发请求；之后靠显式刷新或核对结束，绝不轮询。
    if (next && isAdmin && state === null && !loading) void load()
  }

  async function startReview() {
    const gate = reviewStartGate({ isAdmin, password, busy: reviewBusy, operationActive })
    if (!gate.allowed) {
      setReviewMessage({ tone: 'warning', text: gate.reason })
      return
    }
    setReviewBusy(true)
    setReviewMessage(null)
    try {
      // remember 沿用闪时送表单默认行为（保存到系统凭据管理器）；只读核对不会下单。
      const result: SssReviewStartResult = await api().start_sss_review({ password, remember: true })
      const startView = reviewStartView(result)
      const text = [startView.message].concat(startView.fieldMessages).filter(Boolean).join('；')
      setReviewMessage({ tone: startView.tone, text })
      if (startView.ok) {
        awaitingReview.current = true
        toast.info('已启动只读核对（零 POST）；面板打开时会在结束后自动刷新。')
      } else {
        toast.warning(startView.message)
      }
    } catch (error) {
      const issue = classifyRequestError(error)
      const detail = issue.detail || issue.title
      setReviewMessage({ tone: 'danger', text: '只读核对启动失败：' + detail })
      toast.error('只读核对启动失败：' + detail, { duration: 8000 })
    } finally {
      setReviewBusy(false)
    }
  }

  function toggleRow(id: string) {
    setSelected((prev) => (prev.includes(id) ? prev.filter((item) => item !== id) : prev.concat([id])))
  }

  function requestDecision(next: SssUncertainDecision) {
    setDecision(next)
    // 换决策后旧确认串不再有意义：强制重新逐字输入，避免误提交。
    setConfirmText('')
    setFormError('')
    setResolveOutcome(null)
  }

  function openConfirm() {
    if (!resolveState.allowed) {
      setFormError(resolveState.reason)
      return
    }
    setFormError('')
    setConfirmOpen(true)
  }

  async function performResolve() {
    const built = buildUncertainResolvePayload({
      isAdmin: Boolean(isAdmin),
      decision,
      recordIds: selected,
      note,
      confirm: confirmText,
      review: view.review,
      busy: false,
    })
    if (!built.ok) {
      setFormError(built.message)
      setConfirmOpen(false)
      return
    }
    const token = gateRef.current.begin()
    if (token === null) return
    setResolveBusy(true)
    setFormError('')
    try {
      const result: SssUncertainResolveResult = await api().sss_uncertain_resolve(built.payload)
      if (!gateRef.current.isCurrent(token)) return
      setResolveOutcome(uncertainResolveView(result))
      setConfirmOpen(false)
      setDecision('')
      setNote('')
      setConfirmText('')
      setSelected([])
      await load()
    } catch (error) {
      if (!gateRef.current.isCurrent(token)) return
      const issue = classifyRequestError(error)
      const permission = issue.kind === 'permission'
      setResolveOutcome(uncertainResolveView({
        ok: false,
        status: 'error',
        code: permission ? 'forbidden' : 'network_unknown',
        reason: permission
          ? '服务端拒绝了本次请求（该入口仅管理员可用）。'
          : '网络失败：' + (issue.detail || issue.title),
        next_action: permission
          ? '请联系管理员处理。'
          : '不要重复提交：先点「刷新未决记录（只读）」核对本地状态，再决定下一步。',
        changed: false,
      }))
      setConfirmOpen(false)
      toast.error(permission ? '当前账号没有解除权限' : '解除请求结果未知（阻断保持）', { duration: 8000 })
      void load()
    } finally {
      gateRef.current.finish(token)
      setResolveBusy(false)
    }
  }

  const selectable = view.records.filter((row) => row.journalId)
  const allSelected = selectable.length > 0 && selectable.every((row) => selected.includes(row.journalId))

  return (
    <section className="mb-3.5 rounded-lg border border-warning/45 bg-warning/5" aria-label="未决记录与只读核对">
      <button
        type="button"
        aria-expanded={open}
        aria-controls={PANEL_ID}
        onClick={toggle}
        className="flex w-full items-center gap-2 rounded-lg px-3 py-2.5 text-left transition-colors hover:bg-warning/10"
      >
        {open
          ? <ChevronDown className="size-4 shrink-0 text-muted-foreground" />
          : <ChevronRight className="size-4 shrink-0 text-muted-foreground" />}
        <span className="min-w-0 flex-1">
          <span className="block text-[13px] font-medium text-foreground">未决记录与只读核对（阻断中）</span>
          <span className="block truncate text-[11px] text-muted-foreground">
            {open && view.ok
              ? '活跃 ' + view.counts.active + ' 条（inflight ' + view.counts.inflight + ' / unresolved ' + view.counts.unresolved + '）；未确认前不要重跑或补发。'
              : '闪时送任务被阻断：存在未解决的不确定记录；展开后只读核对站内订单与本地记录。'}
          </span>
        </span>
        <span className="shrink-0 text-[11px] text-muted-foreground">{open ? '收起' : '展开核对'}</span>
      </button>

      {open && (
        <div id={PANEL_ID} role="region" aria-label="未决记录与只读核对" className="border-t border-warning/40 px-3 pb-3 pt-3">
          {!isAdmin ? (
            <Callout tone="neutral" title="未决记录明细仅管理员可见">
              当前账号不是管理员：这里不会读取记录明细。请联系管理员用「只读核对站内订单」确认后解除阻断；
              未确认前不要重跑或补发，也不要用其它方式绕过阻断。
            </Callout>
          ) : (
            <>
              <div className="flex flex-wrap items-center gap-1.5">
                <Button
                  size="sm"
                  variant="outline"
                  className="h-8 rounded-[6px] text-xs"
                  disabled={!startGate.allowed}
                  title={startGate.reason || '只读扫描站内订单，零 POST'}
                  onClick={() => void startReview()}
                >
                  {reviewBusy ? '正在启动…' : '只读核对站内订单'}
                </Button>
                <Button
                  size="sm"
                  variant="ghost"
                  className="h-8 text-xs"
                  disabled={loading}
                  onClick={() => void load()}
                >
                  {loading ? '读取中…' : '刷新未决记录（只读）'}
                </Button>
                <span className="text-[11px] text-muted-foreground">两个按钮都不会发送下单 POST。</span>
              </div>
              {!startGate.allowed && startGate.reason && (
                <p className="mt-1 text-[11px] text-warning">{startGate.reason}</p>
              )}
              {reviewMessage && (
                <Callout
                  tone={reviewMessage.tone === 'danger' ? 'danger' : reviewMessage.tone === 'info' ? 'info' : 'warning'}
                  title="只读核对"
                >
                  {reviewMessage.text}
                </Callout>
              )}

              {loading && state === null && (
                <p className="text-[11px] text-muted-foreground">正在只读读取未决记录…</p>
              )}
              {loadError && (
                <Callout tone="danger" title="未决记录读取失败">
                  {loadError}。读取失败不代表没有未决记录：请修复后重新读取，未确认前不要重跑或补发。
                </Callout>
              )}
              {state && !view.ok && (
                <Callout tone="danger" title="未决记录不可读（阻断保持）">
                  {view.reason || '本地记录不可读或损坏。'}
                  {view.code ? '（' + view.code + '）' : ''}
                  {view.nextAction ? ' 下一步：' + view.nextAction : ' 请先修复本地记录后重试。'}
                </Callout>
              )}

              {state && view.ok && (
                <>
                  <p className="text-[11px] text-muted-foreground">
                    以下为本地 journal 的活跃未决记录（只读投影）。闪时送下单 POST 非幂等：
                    未确认前不要重跑或补发；解除只写本地审计，永不发送 POST。
                  </p>
                  {view.nextAction && (
                    <p className="mt-0.5 text-[11px] text-foreground">服务端下一步：{view.nextAction}</p>
                  )}
                  <div className="mt-1.5 flex flex-wrap gap-1.5 text-[11px]">
                    <span className="rounded-full border border-destructive/40 bg-destructive/5 px-2 py-0.5 text-destructive">
                      活跃 {view.counts.active}
                    </span>
                    <span className="rounded-full border bg-card px-2 py-0.5">inflight {view.counts.inflight}</span>
                    <span className="rounded-full border bg-card px-2 py-0.5">unresolved {view.counts.unresolved}</span>
                    <span className="rounded-full border bg-card px-2 py-0.5">已确认 {view.counts.resolved}</span>
                    <span className="rounded-full border bg-card px-2 py-0.5">已排除 {view.counts.discarded}</span>
                  </div>
                  <p className="mt-1 break-all text-[11px] text-muted-foreground">
                    账号 {view.account || '—'} · 批次 {view.batchKey || '—'} · 送达日期 {view.deliveryDate || '—'}
                    {view.journal ? ' · 本地记录 ' + view.journal : ''}
                  </p>

                  <div className="mt-2 rounded-md border bg-card px-2.5 py-2 text-[11px]">
                    <p className="font-medium text-foreground">上一次只读核对（站内订单）</p>
                    {view.review.available ? (
                      <>
                        <p className="mt-0.5 text-muted-foreground">
                          时间 {view.review.checkedAt || '—'}
                          {view.review.ageS !== null ? '（' + Math.round(view.review.ageS) + ' 秒前）' : ''}
                          {' · '}{view.review.stale ? '已过期' : '有效'}
                          {' · '}指纹 {view.review.journalFingerprint || '—'}
                          {' · '}{view.review.journalMatches ? '与当前记录一致' : '与当前记录不一致'}
                          {' · '}核对窗口 {view.review.wideWindowDays} 天
                        </p>
                        <div className="mt-1 flex flex-wrap gap-1.5">
                          <span className="rounded-full border bg-secondary/40 px-2 py-0.5">站内未查到 {view.review.counts.station_missing}</span>
                          <span className="rounded-full border bg-secondary/40 px-2 py-0.5">只查到其他日期 {view.review.counts.station_found_other_day}</span>
                          <span className="rounded-full border bg-secondary/40 px-2 py-0.5">站内已确认 {view.review.counts.station_confirmed}</span>
                          <span className="rounded-full border bg-secondary/40 px-2 py-0.5">扫描失败 {view.review.counts.scan_failed}</span>
                        </div>
                        {!view.review.usableForAbsent && (
                          <p className="mt-1 text-warning">「解除阻断」暂不可用：{view.review.unavailableReason}</p>
                        )}
                      </>
                    ) : (
                      <p className="mt-0.5 text-warning">
                        还没有核对快照：解除阻断前必须先做一次「只读核对站内订单」；
                        「已在站内找到 → 标记为已确认」不受此限制。
                      </p>
                    )}
                  </div>

                  {view.records.length === 0 ? (
                    <p className="mt-2 rounded-md border bg-card px-2.5 py-2 text-[11px] text-muted-foreground">
                      当前批次没有活跃未决记录（下方计数里的已确认/已排除是历史审计记录）。
                    </p>
                  ) : (
                    <div className="mt-2 overflow-x-auto rounded-md border bg-card">
                      <table className="w-full min-w-[760px] text-left text-[11px]">
                        <caption className="sr-only">未决记录列表</caption>
                        <thead className="bg-secondary/40 text-muted-foreground">
                          <tr>
                            <th className="w-8 px-2 py-1.5">
                              <input
                                type="checkbox"
                                checked={allSelected}
                                onChange={() => setSelected(allSelected ? [] : selectable.map((row) => row.journalId))}
                                aria-label="全选未决记录"
                              />
                            </th>
                            <th className="px-2 py-1.5 font-medium">姓名</th>
                            <th className="px-2 py-1.5 font-medium">手机（掩码）</th>
                            <th className="px-2 py-1.5 font-medium">送达时间</th>
                            <th className="px-2 py-1.5 font-medium">表</th>
                            <th className="px-2 py-1.5 font-medium">状态</th>
                            <th className="px-2 py-1.5 font-medium">错误</th>
                            <th className="px-2 py-1.5 font-medium">创建时间</th>
                            <th className="px-2 py-1.5 font-medium">核对分类</th>
                          </tr>
                        </thead>
                        <tbody>
                          {view.records.map((row, index) => (
                            <tr key={row.journalId || row.identifier || String(index)} className="border-t border-border/60 align-top">
                              <td className="px-2 py-1.5">
                                <input
                                  type="checkbox"
                                  checked={selected.includes(row.journalId)}
                                  disabled={!row.journalId}
                                  onChange={() => toggleRow(row.journalId)}
                                  aria-label={'选择 ' + (row.name || row.journalId)}
                                />
                              </td>
                              <td className="px-2 py-1.5 text-foreground">{row.name || '—'}</td>
                              <td className="tabular px-2 py-1.5">{row.phone || '—'}</td>
                              <td className="tabular px-2 py-1.5">{row.deliveryTime || '—'}</td>
                              <td className="px-2 py-1.5">{row.sheet || '—'}</td>
                              <td className="px-2 py-1.5">{row.statusLabel}</td>
                              <td className="max-w-[16rem] px-2 py-1.5 text-muted-foreground">{row.error || '—'}</td>
                              <td className="tabular px-2 py-1.5">{row.createdAt || '—'}</td>
                              <td className="px-2 py-1.5">{row.classificationLabel}</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  )}

                  {view.records.length > 0 && (
                    <div className="mt-3 rounded-md border border-destructive/40 bg-destructive/5 px-2.5 py-2.5">
                      <p className="text-[11px] font-medium text-foreground">管理员解除（唯一写入口，永不发送 POST）</p>
                      <p className="mt-0.5 text-[11px] text-muted-foreground">
                        已勾选 {selected.length} / {view.records.length} 条。处置只写本地 journal 审计（含操作人、备注、核对指纹）；
                        未确认前不要重跑或补发。
                      </p>
                      <div className="mt-1.5 flex flex-wrap gap-1.5">
                        <Button
                          size="sm"
                          variant={decision === 'station_present' ? 'default' : 'outline'}
                          className="h-8 rounded-[6px] text-xs"
                          onClick={() => requestDecision('station_present')}
                        >
                          已在站内找到 → 标记为已确认
                        </Button>
                        <Button
                          size="sm"
                          variant={decision === 'station_absent' ? 'default' : 'outline'}
                          className="h-8 rounded-[6px] text-xs"
                          disabled={!absent.allowed}
                          title={absent.allowed ? SSS_DECISION_SPECS.station_absent.risk : absent.reason}
                          onClick={() => requestDecision('station_absent')}
                        >
                          已确认站内无这些订单 → 解除阻断
                        </Button>
                        <Button
                          size="sm"
                          variant={decision === 'keep' ? 'default' : 'ghost'}
                          className="h-8 rounded-[6px] text-xs"
                          onClick={() => requestDecision('keep')}
                        >
                          保持阻断（不做任何改动）
                        </Button>
                      </div>
                      {decisionSpec && (
                        <div className="mt-2 space-y-1.5">
                          <p className="text-[11px] text-warning">风险：{decisionSpec.risk}</p>
                          <label className="block text-[11px] text-muted-foreground">
                            人工核对说明（至少 4 个字符，写入本地审计）
                            <textarea
                              value={note}
                              rows={2}
                              maxLength={200}
                              onChange={(event) => setNote(event.target.value)}
                              placeholder="例如：已在闪时送订单列表按送达日期核对，这 62 条站内均无记录"
                              className="mt-1 w-full rounded-[6px] border border-border bg-secondary px-2.5 py-2 text-[12px] text-foreground outline-none focus:border-primary focus:ring-2 focus:ring-primary/30"
                            />
                          </label>
                          <label className="block text-[11px] text-muted-foreground">
                            逐字输入 <code className="rounded bg-secondary px-1 py-0.5 text-foreground">{decisionSpec.decision}</code> 确认
                            <input
                              value={confirmText}
                              onChange={(event) => setConfirmText(event.target.value)}
                              placeholder={decisionSpec.decision}
                              autoComplete="off"
                              className="mt-1 h-9 w-full rounded-[6px] border border-border bg-secondary px-2.5 text-[12px] text-foreground outline-none focus:border-primary focus:ring-2 focus:ring-primary/30"
                            />
                          </label>
                          <div className="flex flex-wrap items-center gap-2">
                            <Button
                              size="sm"
                              className={cn('h-8 rounded-[6px] text-xs', decision === 'station_absent' && 'bg-destructive text-white hover:bg-destructive/90')}
                              disabled={!resolveState.allowed || resolveBusy}
                              onClick={openConfirm}
                            >
                              {resolveBusy ? '提交中…' : '提交处置…'}
                            </Button>
                            {!resolveState.allowed && resolveState.reason && (
                              <span className="text-[11px] text-warning">{resolveState.reason}</span>
                            )}
                          </div>
                          {formError && (
                            <p role="alert" className="rounded-md border border-destructive/40 bg-destructive/5 px-2.5 py-1.5 text-[11px] text-destructive">
                              {formError}
                            </p>
                          )}
                        </div>
                      )}
                    </div>
                  )}

                  {resolveOutcome && (
                    <Callout
                      tone={resolveOutcome.tone === 'success' ? 'success' : resolveOutcome.tone === 'danger' ? 'danger' : resolveOutcome.tone === 'info' ? 'info' : 'warning'}
                      title={resolveOutcome.title}
                    >
                      <p>{resolveOutcome.detail}</p>
                      <p className="mt-0.5">下一步：{resolveOutcome.nextStep}</p>
                      <p className="mt-0.5 text-muted-foreground">
                        本次没有发送 POST、没有写云端
                        {resolveOutcome.remaining >= 0 ? '；剩余未决 ' + resolveOutcome.remaining + ' 条' : ''}
                        {resolveOutcome.blockingRetained ? '；阻断保持。' : '；阻断已解除。'}
                      </p>
                    </Callout>
                  )}
                </>
              )}
            </>
          )}
        </div>
      )}

      <ConfirmActionDialog
        open={confirmOpen}
        onOpenChange={setConfirmOpen}
        title={decision === 'station_absent' ? '确认解除未决记录阻断' : decision === 'station_present' ? '确认标记为站内已确认' : '确认记录审计备注'}
        description={decisionSpec
          ? decisionSpec.effect + ' 本入口不会发送任何下单 POST。'
          : '本次处置只写本地审计。'}
        details={decisionSpec ? (
          <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 rounded-md border bg-secondary/40 px-3 py-2 text-[11px]">
            <div className="contents">
              <dt className="text-muted-foreground">决策</dt>
              <dd className="text-foreground">{decisionSpec.decision}</dd>
            </div>
            <div className="contents">
              <dt className="text-muted-foreground">记录数</dt>
              <dd className="text-foreground">{selected.length} 条</dd>
            </div>
            <div className="contents">
              <dt className="text-muted-foreground">核对快照</dt>
              <dd className="text-foreground">
                {view.review.available
                  ? view.review.checkedAt + (view.review.stale ? '（已过期）' : '') + (view.review.journalMatches ? ' · 指纹一致' : ' · 指纹不一致')
                  : '无（该决策不需要快照）'}
              </dd>
            </div>
          </dl>
        ) : null}
        acknowledge="我已人工只读核对站内订单，并了解本次处置只写本地审计、不会发送任何 POST"
        confirmLabel={decision === 'station_absent' ? '确认解除阻断' : decision === 'station_present' ? '确认标记为已确认' : '确认记录备注'}
        busy={resolveBusy}
        danger={decision === 'station_absent'}
        onConfirm={performResolve}
      />
    </section>
  )
}
