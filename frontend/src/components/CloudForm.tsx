/**
 * 云文档同步工作台：准备 → 校验/预览 → 确认 → 执行 → 结果。
 *
 * 严格按 docs/OPTIMIZATION-PROGRESS.md 接入：
 * - wps_preview() 返回 preview_id / created_at / expires_at / tables / stats / blocked / warnings /
 *   target_date / test_mode / target_tables；
 * - wps_upload(preview_id) 必须传令牌，前端在无有效 token、未授权、预览缺失/过期/本地变化时禁用上传；
 * - 统计只来自结构化字段，不解析 text、不前端自造时间；
 * - 后端上传前复核仍是最终闸门，前端变化检测只是提前作废。
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { ChevronDown, ChevronRight, Loader2 } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Switch } from '@/components/ui/switch'
import { Field, TextInput } from '@/components/fields'
import {
  AdvancedSection,
  Callout,
  ConfirmActionDialog,
  FlowStrip,
  StatusPill,
  SummaryTile,
} from '@/components/WorkspaceUI'
import { useApp } from '@/hooks/appContext'
import { useConfigSave } from '@/hooks/useConfigSave'
import {
  api,
  isApiReady,
  isWebTransport,
  type WpsCopyCheck,
  type WpsPreviewResult,
  type WpsRecoveryDecision,
  type WpsRecoveryOperation,
  type WpsRecoveryStatus,
  type WpsStatus,
  type WpsUploadResult,
} from '@/lib/bridge'
import {
  blockedItems,
  previewCounts,
  previewFreshness,
  previewLocalKey,
  previewWarnings,
  uploadGate,
  uploadNextAction,
  type PreviewHandle,
} from '@/lib/preview'
import {
  UNKNOWN_TEXT,
  previewPlanText,
  uploadCountLines,
  uploadCountView,
  uploadHeadline,
  uploadResultStep,
} from '@/lib/resultCounts'
import { SingleFlightGate } from '@/lib/singleFlight'
import {
  DECISION_SPECS,
  buildResolvePayload,
  recoveryOperationView,
  recoveryResolveView,
  retiredGuardedCount,
  type RecoveryOperationView,
  type ResolveOutcomeView,
} from '@/lib/recoveryResolve'
import {
  journalIssueOf,
  previewFailureView,
  uploadFailureView,
  type FailureView,
} from '@/lib/wpsFailure'
import { classifyRequestError } from '@/lib/requestError'
import { splitAddressLines } from '@/lib/format'
import { pointFromEvent } from '@/lib/reveal'
import type { LogReveal } from '@/lib/useLogReveal'
import { saveStateView } from '@/lib/saveState'
import { cn } from '@/lib/utils'

const ADDRESS_SHEETS = [
  '东湖中餐',
  '衣锦中餐',
  '医学院中餐',
  '东湖晚餐',
  '衣锦晚餐',
  '医学院晚餐',
] as const

const ADDRESS_PLACEHOLDER = '一行一个地址，从上到下就是排列顺序；留空 = 按地址升序排列'

const addressTextareaClass = cn(
  'mt-1 w-full resize-y rounded-[6px] border border-transparent bg-secondary px-2.5 py-1.5',
  'font-mono text-xs leading-relaxed transition-colors outline-none',
  'placeholder:text-ink-faint focus-visible:border-ring focus-visible:bg-card',
  'focus-visible:ring-[3px] focus-visible:ring-ring/50',
)

type Tone = 'info' | 'success' | 'warning' | 'danger' | 'neutral'

interface Message {
  tone: Tone
  text: string
}

export function CloudForm({ logReveal }: { logReveal?: LogReveal }) {
  const {
    config, isAdmin, hasValidToken, authError, operationActive, operationView, reconnect,
  } = useApp()

  const [status, setStatus] = useState<WpsStatus | null>(null)
  const [recoveryStatus, setRecoveryStatus] = useState<WpsRecoveryStatus | null>(null)
  const [recoveryError, setRecoveryError] = useState<{ title: string; detail: string; nextStep: string } | null>(null)
  const [previewHandle, setPreviewHandle] = useState<PreviewHandle | null>(null)
  const [previewError, setPreviewError] = useState('')
  const [previewFailure, setPreviewFailure] = useState<FailureView | null>(null)
  const [uploadResult, setUploadResult] = useState<WpsUploadResult | null>(null)
  const [networkUnknown, setNetworkUnknown] = useState(false)
  const [copyCheck, setCopyCheck] = useState<WpsCopyCheck | null>(null)
  const [message, setMessage] = useState<Message | null>(null)
  const [busy, setBusy] = useState<'' | 'preview' | 'upload' | 'auth' | 'refresh' | 'check'>('')
  // W3 管理员恢复流程：刷新只读，只有用户在弹窗里显式确认才会调 wps_recovery_resolve。
  const [resolveTarget, setResolveTarget] = useState<RecoveryOperationView | null>(null)
  const [resolveDecision, setResolveDecision] = useState<WpsRecoveryDecision | ''>('')
  const [resolveNote, setResolveNote] = useState('')
  const [resolveConfirm, setResolveConfirm] = useState('')
  const [resolveStructure, setResolveStructure] = useState(false)
  const [resolveError, setResolveError] = useState('')
  const [resolveBusy, setResolveBusy] = useState(false)
  const [resolveOutcome, setResolveOutcome] = useState<ResolveOutcomeView | null>(null)
  const resolveGate = useRef(new SingleFlightGate())
  const uploadGateRef = useRef(new SingleFlightGate())
  const [enabled, setEnabled] = useState(config?.wps_enabled ?? false)
  const [testMode, setTestMode] = useState(config?.wps_test_mode ?? true)
  const [cliPath, setCliPath] = useState(config?.wps_cli_path ?? '')
  const [marker, setMarker] = useState(config?.wps_marker_enabled ?? true)
  const [sortEnabled, setSortEnabled] = useState(true)
  const [addressOrder, setAddressOrder] = useState<Record<string, string[]>>({})
  const [orderDraft, setOrderDraft] = useState<Record<string, string>>({})
  const [advancedOverride, setAdvancedOverride] = useState<boolean | null>(null)
  const [addressOpen, setAddressOpen] = useState(false)
  const [confirmOpen, setConfirmOpen] = useState(false)
  const [detailOpen, setDetailOpen] = useState(false)
  const orderDirty = useRef(false)
  const loaded = useRef(false)

  // 草稿指纹：保存队列和刷新都只认当前草稿版本，刷新回填不能覆盖请求发出后的编辑。
  const draftSignature = useMemo(() => previewLocalKey({
    enabled, testMode, cliPath, marker, sortEnabled, addressOrder,
  }), [addressOrder, cliPath, enabled, marker, sortEnabled, testMode])
  const draftSignatureRef = useRef(draftSignature)
  useEffect(() => {
    draftSignatureRef.current = draftSignature
  }, [draftSignature])
  const hydratedSignatureRef = useRef(draftSignature)

  const saveGetDraftRevisionRef = useRef<() => number>(() => 0)
  const saveGetSavedRevisionRef = useRef<() => number>(() => 0)
  const saveHasUnsavedRef = useRef<() => boolean>(() => false)

  const needSetupNotice = useMemo(() => {
    if (!status) return '正在读取组件与授权状态'
    if (!status.cli_found) return '未找到云同步组件，请展开高级设置检查组件路径'
    if (!status.authenticated) return '尚未完成 WPS 授权，上传被禁用'
    if (!status.excel_path) return '尚未选择本地排单表，请先到「订单处理」选择'
    if (!status.tables.length) return '尚未配置云端排单表，请联系管理员完成配置'
    return ''
  }, [status])

  const refresh = useCallback(async (): Promise<void> => {
    if (!isWebTransport() || !isApiReady()) return
    const startSignature = draftSignatureRef.current
    const startDraftRevision = saveGetDraftRevisionRef.current()
    const startSavedRevision = saveGetSavedRevisionRef.current()
    setBusy((prev) => (prev === '' ? 'refresh' : prev))
    // 只读刷新：wps_status + wps_recovery_status 都不触发上传/解绑阻断/自动重试。
    try {
      const next = await api().wps_status()
      setStatus(next)
      setNetworkUnknown(false)
      // 服务端值只在“没有未落盘草稿、刷新期间没有新编辑、也没有保存刚完成”时回填。
      const canApplyServerValues = draftSignatureRef.current === startSignature
        && !saveHasUnsavedRef.current()
        && saveGetDraftRevisionRef.current() === startDraftRevision
        && saveGetSavedRevisionRef.current() === startSavedRevision
      if (canApplyServerValues) {
        const nextOrder = next.address_order ?? {}
        const nextSignature = previewLocalKey({
          enabled: next.enabled,
          testMode: next.test_mode,
          cliPath: next.cli_path,
          marker: next.marker_enabled,
          sortEnabled: next.sort_enabled,
          addressOrder: nextOrder,
        })
        hydratedSignatureRef.current = nextSignature
        setSortEnabled(next.sort_enabled)
        if (!orderDirty.current) {
          setAddressOrder((prev) => (
            previewLocalKey(prev) === previewLocalKey(nextOrder) ? prev : nextOrder
          ))
        }
        setEnabled(next.enabled)
        setTestMode(next.test_mode)
        setCliPath(next.cli_path)
        setMarker(next.marker_enabled)
      }
    } catch (error) {
      setMessage({ tone: 'danger', text: `状态刷新失败：${classifyRequestError(error).detail}` })
    }
    try {
      const recovery = await api().wps_recovery_status()
      setRecoveryStatus(recovery)
      setRecoveryError(null)
    } catch (error) {
      const view = classifyRequestError(error)
      setRecoveryError({ title: view.title, detail: view.detail, nextStep: view.nextStep })
    } finally {
      setBusy((prev) => (prev === 'refresh' ? '' : prev))
    }
  }, [])

  const save = useCallback(async () => {
    if (!isWebTransport() || !isApiReady()) {
      return { ok: false, reason: '后端未连接，配置尚未保存' }
    }
    const result = await api().save_wps_config({
      enabled,
      test_mode: testMode,
      cli_path: cliPath,
      drive_id: config?.wps_drive_id ?? '',
      test_file_id: config?.wps_test_file_id ?? '',
      test_drive_id: config?.wps_test_drive_id ?? '',
      marker_enabled: marker,
      sort_enabled: sortEnabled,
      address_order: addressOrder,
      tables: config?.wps_tables ?? {},
      test_tables: config?.wps_test_tables ?? {},
    })
    // 保存成功后不再自动 refresh：旧刷新可能把此刻之后的新编辑覆盖掉。
    // 状态由本地草稿/保存队列和随后的显式只读刷新负责。
    return result?.ok === false
      ? { ok: false, reason: result.reason || '配置保存失败' }
      : { ok: true }
  }, [enabled, testMode, cliPath, marker, sortEnabled, addressOrder, config])

  const configSave = useConfigSave(save)
  const {
    state: saveState, schedule: scheduleSave, submit: submitSave, retry,
  } = configSave
  useEffect(() => {
    saveGetDraftRevisionRef.current = configSave.getDraftRevision
    saveGetSavedRevisionRef.current = configSave.getSavedRevision
    saveHasUnsavedRef.current = configSave.hasUnsaved
  }, [configSave.getDraftRevision, configSave.getSavedRevision, configSave.hasUnsaved])
  const firstSave = useRef(true)
  useEffect(() => {
    if (firstSave.current) {
      firstSave.current = false
      hydratedSignatureRef.current = draftSignature
      return
    }
    // 服务端刷新回填后的 draftSignature 等于 hydratedSignature，跳过无意义的自动保存。
    if (draftSignature === hydratedSignatureRef.current) return
    scheduleSave()
  }, [draftSignature, scheduleSave])

  useEffect(() => {
    if (loaded.current || !config) return
    loaded.current = true
    setEnabled(config.wps_enabled)
    setTestMode(config.wps_test_mode)
    setCliPath(config.wps_cli_path)
    setMarker(config.wps_marker_enabled)
    void refresh()
  }, [config, refresh])

  const currentLocalKey = useMemo(() => previewLocalKey({
    enabled,
    testMode,
    cliPath,
    marker,
    sortEnabled,
    addressOrder,
    targetDate: status?.target_date ?? '',
    // 写入目标/时间窗/驱动等任何后端配置变化都让预览立即作废。
    targetTables: config?.wps_tables ?? {},
    testTables: config?.wps_test_tables ?? {},
    driveId: config?.wps_drive_id ?? '',
    testFileId: config?.wps_test_file_id ?? '',
    testDriveId: config?.wps_test_drive_id ?? '',
    targetHourStart: config?.wps_target_hour_start,
    targetHourEnd: config?.wps_target_hour_end,
    sortDefaults: status?.address_order_defaults ?? {},
  }), [enabled, testMode, cliPath, marker, sortEnabled, addressOrder, status?.target_date,
    status?.address_order_defaults, config?.wps_tables, config?.wps_test_tables,
    config?.wps_drive_id, config?.wps_test_file_id, config?.wps_test_drive_id,
    config?.wps_target_hour_start, config?.wps_target_hour_end])

  const freshness = previewFreshness(previewHandle, currentLocalKey)
  const preview = previewHandle?.preview ?? null
  const counts = previewCounts(preview)
  const blocked = blockedItems(preview)
  const warnings = previewWarnings(preview)

  const gate = uploadGate({
    hasValidToken,
    wpsEnabled: enabled,
    cliFound: Boolean(status?.cli_found),
    authenticated: Boolean(status?.authenticated),
    busy: busy !== '' || operationActive || networkUnknown,
    handle: previewHandle,
    currentLocalKey,
  })

  const previewDisabled = !enabled || !status?.cli_found || !status?.authenticated
    || operationActive || busy !== ''

  async function onPreview() {
    if (previewDisabled) {
      setPreviewError(!enabled
        ? '云文档同步未启用'
        : !status?.cli_found
          ? '未找到云同步组件，请检查高级设置'
          : !status?.authenticated
            ? '尚未授权云文档，请先完成授权'
            : '已有操作进行中，请等待结束')
      return
    }
    setBusy('preview')
    setPreviewError('')
    setPreviewFailure(null)
    setPreviewHandle(null)
    setUploadResult(null)
    setNetworkUnknown(false)
    setMessage(null)
    try {
      const result: WpsPreviewResult = await api().wps_preview()
      if (result.ok && result.preview_id) {
        setPreviewHandle({ preview: result, localKey: currentLocalKey })
        setMessage({ tone: 'success', text: `预览已生成（${previewPlanText(result)}），请在有效期内核对后确认上传` })
      } else {
        // wps_disabled / journal 损坏或版本不支持 / 并发 / 文件变化 都有各自的下一步。
        const failure = previewFailureView(result)
        setPreviewFailure(failure)
        setPreviewError('')
        setMessage(null)
      }
    } catch (error) {
      // 网络未知：预览是只读入口，结果未知不等于写入；提示重试而不是猜测。
      setPreviewFailure(null)
      setPreviewError(`${classifyRequestError(error).detail}（预览是只读入口，失败不代表写入了任何内容；可重试。）`)
    } finally {
      setBusy('')
    }
  }

  function requestUpload() {
    if (!gate.ok) {
      setMessage({ tone: 'warning', text: gate.reason })
      return
    }
    setConfirmOpen(true)
  }

  async function performUpload() {
    if (!previewHandle?.preview.preview_id || !gate.ok) return
    // 上传用一次性令牌：连点会在 React 重新渲染（disabled 生效）之前重复进入这里，
    // 必须用单飞闸门挡住，否则第 2/3 次会带着已消费的令牌返回 preview_consumed，
    // 把"已经成功"的结果卡片覆盖成"被拒绝（未写入）"。
    const token = uploadGateRef.current.begin()
    if (token === null) return
    setBusy('upload')
    setMessage(null)
    setUploadResult(null)
    try {
      const result = await api().wps_upload(previewHandle.preview.preview_id)
      if (!uploadGateRef.current.isCurrent(token)) return
      setUploadResult(result)
      if (result.code !== 'operation_conflict') setPreviewHandle(null)
      setNetworkUnknown(false)
      // W6：只有明确 success 且行数可核实才允许成功文案；uncertain/partial/blocked/noop 一律按待核对或无需写入展示。
      const headline = uploadHeadline(result)
      if (headline.impliesComplete && headline.tone === 'success') {
        setMessage({ tone: 'success', text: `上传完成（目标日期 ${result.target_date ?? preview?.target_date ?? ''}）` })
      } else {
        const failure = uploadFailureView(result)
        setMessage({
          tone: headline.tone === 'danger' ? 'danger' : 'warning',
          text: failure?.nextStep || headline.nextStep || headline.title,
        })
      }
      await refresh()
    } catch (error) {
      if (!uploadGateRef.current.isCurrent(token)) return
      // 网络未知：不当作失败/成功；保留预览句柄，提示先查权威状态。
      setNetworkUnknown(true)
      setMessage({
        tone: 'warning',
        text: `上传请求结果未知：${classifyRequestError(error).detail}。请先查询操作状态或重新核对，不要直接重复上传。`,
      })
      void refresh()
    } finally {
      uploadGateRef.current.finish(token)
      setBusy('')
      setConfirmOpen(false)
    }
  }

  // ---------- W3：管理员恢复/退场（永不写云端，失败保持阻断） ----------

  function openResolve(operation: RecoveryOperationView) {
    setResolveTarget(operation)
    setResolveDecision(operation.decisions.length === 1 ? operation.decisions[0] : '')
    setResolveNote('')
    setResolveConfirm('')
    setResolveStructure(false)
    setResolveError('')
  }

  function closeResolve() {
    setResolveTarget(null)
    setResolveError('')
  }

  async function submitResolve() {
    // 双击/并发保护：同一时刻只允许一个恢复请求在途。
    if (!resolveTarget) return
    const built = buildResolvePayload({
      operationId: resolveTarget.operationId,
      decision: resolveDecision,
      confirm: resolveConfirm,
      note: resolveNote,
      structureChecked: resolveStructure,
      allowedDecisions: resolveTarget.decisions,
    })
    if (!built.ok) {
      setResolveError(built.message)
      return
    }
    const token = resolveGate.current.begin()
    if (token === null) return
    setResolveBusy(true)
    setResolveError('')
    try {
      const result = await api().wps_recovery_resolve(built.payload)
      // 乱序响应保护：更晚的请求已经发出时，丢弃这个过期结果。
      if (!resolveGate.current.isCurrent(token)) return
      setResolveOutcome(recoveryResolveView(result))
      setResolveTarget(null)
      await refresh()
    } catch (error) {
      if (!resolveGate.current.isCurrent(token)) return
      const view = classifyRequestError(error)
      const permission = view.kind === 'permission'
      setResolveOutcome({
        ok: false,
        tone: permission ? 'warning' : 'danger',
        code: permission ? 'forbidden' : 'network_unknown',
        title: permission ? '当前账号没有该权限' : '恢复请求结果未知（阻断保持）',
        detail: permission
          ? '该恢复入口仅管理员可用；服务端拒绝了本次请求，没有改任何文件。'
          : `网络失败：${view.detail}。服务端可能没有收到这次请求，也可能已经处理；本地状态未知。`,
        nextStep: permission
          ? '请联系管理员处理；普通用户不需要也不应该能解除阻断。'
          : '不要重复提交：先点「刷新状态（只读）」核对本地日志与阻断状态，再决定下一步。',
        blockingRetained: true,
        cloudWrite: false,
        changed: false,
        duplicate: false,
        autoRetryAllowed: false,
      })
      // 网络未知时不自动重试，也不假装成功；保持阻断并提示只读核对。
      // 同时关闭弹窗：避免用户在没有新结果的情况下连点「提交处置」造成重复提交。
      setResolveTarget(null)
      void refresh()
    } finally {
      resolveGate.current.finish(token)
      setResolveBusy(false)
    }
  }

  async function onCheckCopies() {
    if (busy !== '' || operationActive) return
    setBusy('check')
    setCopyCheck(null)
    try {
      setCopyCheck(await api().wps_check_copies())
    } catch (error) {
      setCopyCheck({ ok: false, reason: classifyRequestError(error).detail })
    } finally {
      setBusy('')
    }
  }

  async function onAuthorize() {
    if (busy !== '' || operationActive) return
    setBusy('auth')
    setMessage(null)
    try {
      const result = await api().wps_authorize()
      setMessage({
        tone: result.ok ? 'info' : 'danger',
        text: result.ok ? (result.hint || '已启动授权，请在浏览器中完成确认') : (result.reason || '授权启动失败'),
      })
      await refresh()
    } catch (error) {
      setMessage({ tone: 'danger', text: classifyRequestError(error).detail })
    } finally {
      setBusy('')
    }
  }

  async function onLogout() {
    if (busy !== '' || operationActive) return
    setBusy('auth')
    setMessage(null)
    try {
      const result = await api().wps_logout()
      setMessage({
        tone: result.ok ? 'info' : 'danger',
        text: result.ok ? '已退出 WPS 授权' : (result.reason || '退出授权失败'),
      })
      await refresh()
    } catch (error) {
      setMessage({ tone: 'danger', text: classifyRequestError(error).detail })
    } finally {
      setBusy('')
    }
  }

  function onRestoreOrderDefaults() {
    const next: Record<string, string[]> = {}
    for (const sheet of ADDRESS_SHEETS) {
      const fallback = status?.address_order_defaults?.[sheet] ?? addressOrder[sheet] ?? []
      next[sheet] = [...fallback]
    }
    setAddressOrder(next)
    setOrderDraft({})
    orderDirty.current = true
  }

  const advancedConfigured = Boolean(status?.cli_found && status?.authenticated
    && (status?.tables.length ?? 0) > 0 && status?.excel_path)
  const advancedOpen = advancedOverride ?? !advancedConfigured
  async function refreshAuthority() {
    await reconnect()
    await refresh()
  }

  const effectiveTestTarget = preview?.test_mode === true || (!preview && testMode)
  const primaryLabel = networkUnknown
    ? (operationActive ? '查询操作状态' : '重新确认状态')
    : gate.ok
      ? '确认上传'
      : preview && !freshness.fresh
        ? '重新预览'
        : '生成只读预览'
  const primaryDisabled = networkUnknown ? false : gate.ok ? false : previewDisabled
  const primaryOnClick = networkUnknown || !gate.ok ? () => { void (networkUnknown ? refreshAuthority() : onPreview()) } : requestUpload

  const resultAction = uploadResult ? uploadNextAction(uploadResult) : null

  return (
    <div className="flex min-w-0 min-h-0 flex-1 flex-col">
      <div className="scroll-contain min-h-0 flex-1 overflow-y-auto px-3 pb-4 pt-3 sm:px-5">
        <FlowStrip
          steps={[
            { key: 'prepare', label: '准备', state: 'done' },
            { key: 'preview', label: '预览', state: preview ? 'done' : 'active' },
            { key: 'confirm', label: '确认', state: confirmOpen ? 'active' : gate.ok ? 'todo' : 'todo' },
            { key: 'run', label: '执行', state: busy === 'upload' || operationActive ? 'active' : uploadResult ? 'done' : 'todo' },
            {
              key: 'result',
              label: '结果',
              state: uploadResultStep(uploadResult),
            },
          ]}
          label="云同步流程"
        />

        {!hasValidToken && (
          <Callout tone="danger" title="登录令牌无效，云上传已禁用">
            {authError || '请用带有效 ?token= 的完整网址重新打开页面；无有效令牌时前端与后端都不会允许上传。'}
          </Callout>
        )}

        <Callout tone={operationView.tone === 'danger' ? 'danger' : operationView.tone === 'warning' ? 'warning' : 'neutral'} title={operationView.label}>
          {operationView.detail}
        </Callout>

        {needSetupNotice && (
          <Callout tone="warning" title="先完成云同步准备">
            {needSetupNotice}
            <button type="button" className="mt-1.5 block font-medium text-primary underline" onClick={() => setAdvancedOverride(true)}>
              展开高级设置检查
            </button>
          </Callout>
        )}

        {message && (
          <Callout tone={message.tone} title={message.tone === 'danger' ? '操作未完成' : message.tone === 'warning' ? '需要核对' : undefined}>
            {message.text}
          </Callout>
        )}
        {previewFailure && (
          <FailureCallout view={previewFailure} fallbackTitle="预览失败" />
        )}
        {previewError && !message && <Callout tone="danger" title="预览失败">{previewError}</Callout>}

        <div className="mb-3.5 grid gap-2 sm:grid-cols-2">
          <div className="flex items-center justify-between gap-3 rounded-lg border bg-card px-3 py-2.5">
            <div className="min-w-0">
              <p className="text-[13px] font-medium">启用云文档同步</p>
              <p className="text-[11px] text-muted-foreground">关闭时预览与上传都会被拒绝</p>
            </div>
            <Switch
              checked={enabled}
              onCheckedChange={setEnabled}
              disabled={busy !== '' || operationActive}
              aria-label="启用云文档同步"
            />
          </div>
          <div className={cn('flex items-center justify-between gap-3 rounded-lg border bg-card px-3 py-2.5', !testMode && 'border-warning/50 bg-warning/5')}>
            <div className="min-w-0">
              <p className="text-[13px] font-medium">{testMode ? '测试模式' : '正式模式'}</p>
              <p className="text-[11px] text-muted-foreground">
                {testMode ? '写入测试副本，不碰正式排单表' : '会写入正式目标；请先预览并核对副本'}
              </p>
            </div>
            <Switch
              checked={testMode}
              onCheckedChange={setTestMode}
              disabled={busy !== '' || operationActive}
              aria-label="切换测试或正式模式"
            />
          </div>
        </div>

        <div className="mb-3.5 flex flex-wrap items-center gap-2 text-[11px]">
          <StatusPill label={!status ? '状态未知' : !status.cli_found ? '未找到组件' : status.authenticated ? '已授权' : '未授权'} tone={status?.authenticated ? 'success' : 'warning'} />
          <span className="text-muted-foreground">目标日期：<b className="tabular text-foreground">{status?.target_date || '—'}</b></span>
          <span className="text-muted-foreground">当前写入：<b className="text-foreground">{status?.writing_test_copies ? '测试副本' : '正式/未确认'}</b></span>
        </div>

        <AdvancedSection
          id="cloud-advanced"
          title="高级设置与写入目标"
          summary={needSetupNotice || `组件：${status?.cli_path || '自动查找'}；子表：${status?.tables.length ?? 0} 张`}
          open={advancedOpen}
          onOpenChange={setAdvancedOverride}
          notice={needSetupNotice || undefined}
        >
          <Field label="云同步组件路径" htmlFor="wps-cli-path" helper="留空则自动查找内置组件">
            <TextInput id="wps-cli-path" value={cliPath} onChange={(e) => setCliPath(e.target.value)} placeholder="自动查找" />
          </Field>
          <div className="mb-3.5 flex items-center gap-2 text-[12px] text-muted-foreground">
            <Switch checked={marker} onCheckedChange={setMarker} aria-label="写入协作者通讯记号" disabled={busy !== '' || operationActive} />
            <span>写入协作者通讯记号（测试模式不写）</span>
          </div>

          <div className="mb-3.5 rounded-lg border px-3 py-2.5">
            <div className="flex items-center justify-between gap-3">
              <div>
                <p className="text-[13px] font-medium">地址排序{sortEnabled ? '（已开启）' : ''}</p>
                <p className="text-[11px] text-muted-foreground">按下方顺序重排云端表；关闭后只增量写入</p>
              </div>
              <Switch checked={sortEnabled} onCheckedChange={setSortEnabled} disabled={busy !== '' || operationActive} aria-label="开启地址排序" />
            </div>
            <button type="button" className="mt-2 flex items-center gap-1 text-[11px] text-muted-foreground hover:text-foreground" onClick={() => setAddressOpen((v) => !v)}>
              {addressOpen ? <ChevronDown className="size-3.5" /> : <ChevronRight className="size-3.5" />}
              {addressOpen ? '收起地址顺序' : `展开地址顺序（已指定 ${ADDRESS_SHEETS.filter((s) => (addressOrder[s] ?? []).length > 0).length}/6 张）`}
            </button>
            {addressOpen && (
              <div className="mt-2.5">
                {ADDRESS_SHEETS.map((sheet) => {
                  const items = addressOrder[sheet] ?? []
                  return (
                    <div key={sheet} className="mb-2.5 last:mb-0">
                      <label className="block text-[11px] font-medium" htmlFor={`wps-order-${sheet}`}>
                        {sheet}
                        <span className="ml-1 font-normal text-muted-foreground">{items.length > 0 ? `${items.length} 个地址` : '留空 = 按地址升序'}</span>
                      </label>
                      <textarea
                        id={`wps-order-${sheet}`}
                        rows={3}
                        spellCheck={false}
                        placeholder={ADDRESS_PLACEHOLDER}
                        className={addressTextareaClass}
                        value={orderDraft[sheet] ?? items.join('\n')}
                        onChange={(e) => {
                          const raw = e.target.value
                          setOrderDraft((prev) => ({ ...prev, [sheet]: raw }))
                          setAddressOrder((prev) => ({ ...prev, [sheet]: splitAddressLines(raw) }))
                          orderDirty.current = true
                        }}
                        onBlur={() => setOrderDraft((prev) => {
                          if (!(sheet in prev)) return prev
                          const next = { ...prev }
                          delete next[sheet]
                          return next
                        })}
                      />
                    </div>
                  )
                })}
                <div className="mt-2.5 flex flex-wrap gap-2">
                  <Button variant="outline" size="sm" onClick={onRestoreOrderDefaults}>恢复默认</Button>
                  <Button
                    size="sm"
                    disabled={saveState.phase === 'saving' || saveState.pending}
                    onClick={() => void submitSave()}
                  >
                    立即保存
                  </Button>
                </div>
              </div>
            )}
          </div>

          {status?.tables && status.tables.length > 0 && (
            <div className="mb-3.5 rounded-lg border px-3 py-2.5 text-[11px] text-muted-foreground">
              <p className="mb-1 font-medium text-foreground">写入对应关系</p>
              {status.tables.map((table) => (
                <p key={table.sheet} className="truncate">
                  {table.sheet} → {table.file_id.slice(0, 12)}…{table.last_sync ? `（上次同步 ${table.last_sync.replace('T', ' ')}）` : ''}
                </p>
              ))}
            </div>
          )}

          <div className="flex flex-wrap gap-2">
            <Button variant="outline" size="sm" disabled={busy !== '' || operationActive} onClick={() => void refresh()} title="只读刷新：不会上传、不会解除阻断、不会自动重试">
              {busy === 'refresh' ? '刷新中…' : '刷新状态（只读）'}
            </Button>
            {isAdmin && (
              <Button variant="outline" size="sm" disabled={busy !== '' || operationActive} onClick={() => void onAuthorize()}>
                {busy === 'auth' ? '处理中…' : '去授权'}
              </Button>
            )}
            {isAdmin && status?.authenticated && (
              <Button variant="outline" size="sm" disabled={busy !== '' || operationActive} onClick={() => void onLogout()}>退出授权</Button>
            )}
            <Button variant="outline" size="sm" disabled={busy !== '' || operationActive} onClick={() => void onCheckCopies()}>
              {busy === 'check' ? '核对中…' : '重新核对副本一致性'}
            </Button>
          </div>
        </AdvancedSection>

        {resolveOutcome && (
          <Callout
            tone={resolveOutcome.tone === 'danger' ? 'danger' : resolveOutcome.tone === 'success' ? 'success' : 'warning'}
            title={`恢复处置：${resolveOutcome.title}`}
          >
            <p>{resolveOutcome.detail}</p>
            <p className="mt-1 font-medium text-foreground">下一步：{resolveOutcome.nextStep}</p>
            <p className="mt-1 text-muted-foreground">
              {resolveOutcome.blockingRetained
                ? '阻断仍然保留：未完成人工只读核对前，不允许重新上传或补发。'
                : '阻断已按服务端证据解除；仍不需要强制重传。'}
              {resolveOutcome.duplicate ? ' 本次是重复提交，服务端没有再次写盘。' : ''}
              {' '}本入口不会写云端（cloud_write=false）。
            </p>
            <button type="button" className="mt-1.5 font-medium text-primary underline" onClick={() => setResolveOutcome(null)}>
              知道了，收起
            </button>
          </Callout>
        )}

        <WpsRecoveryCard
          status={recoveryStatus}
          error={recoveryError}
          isAdmin={Boolean(isAdmin)}
          busy={busy !== '' || operationActive || resolveBusy}
          onResolve={openResolve}
        />

        {preview ? (
          <StructuredPreview
            preview={preview}
            freshness={freshness}
            counts={counts}
            blocked={blocked}
            warnings={warnings}
            detailOpen={detailOpen}
            onToggleDetail={() => setDetailOpen((v) => !v)}
          />
        ) : (
          <Callout tone="neutral" title="尚无预览">
            先点底部「生成只读预览」。预览成功后会拿到 preview_id，并显示目标日期、测试/正式目标、统计、阻断与警告；预览过期或任一配置变化后上传会立即禁用。
          </Callout>
        )}

        {uploadResult && (
          <ResultCard result={uploadResult} action={resultAction} onAction={() => {
            if (resultAction?.label === '重新预览') void onPreview()
            else if (resultAction?.label === '重新核对') void onCheckCopies()
            else void refreshAuthority()
          }} />
        )}

        {copyCheck && <CopyCheckCard check={copyCheck} />}

        {preview?.text && (
          <details className="mt-3 rounded-lg border bg-muted/40 p-2.5">
            <summary className="cursor-pointer text-[11px] font-medium text-muted-foreground">查看后端预览全文（兼容字段，默认折叠）</summary>
            <pre className="mt-2 max-h-64 overflow-auto whitespace-pre-wrap break-all font-mono text-[11px] leading-relaxed">{preview.text}</pre>
          </details>
        )}
      </div>

      <div
        className="shrink-0 border-t bg-background/95 px-3 pt-2.5 backdrop-blur sm:px-5"
        style={{ paddingBottom: 'calc(0.75rem + var(--safe-bottom) + var(--keyboard-inset, 0px))' }}
      >
        <div className="mb-2 flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1 text-[11px]">
          <SaveIndicator state={saveState} onRetry={retry} onSaveNow={() => void submitSave()} />
          <span className="text-muted-foreground">{preview ? `预览编号 ${preview.preview_id.slice(0, 14)}…` : '尚无 preview_id'}</span>
          {networkUnknown && <span className="font-medium text-warning">上传结果未知，请先查询权威状态</span>}
        </div>
        <div className="flex gap-2">
          <Button
            className="btn-serif-primary h-10 flex-1 rounded-[8px] text-sm"
            disabled={primaryDisabled || busy === 'upload'}
            onClick={primaryOnClick}
          >
            {busy === 'upload' ? <Loader2 className="mr-1 size-4 animate-spin" /> : null}
            {busy === 'upload' ? '上传中…' : primaryLabel}
          </Button>
          {logReveal && (
            <Button
              variant="outline"
              className="h-10 w-14 rounded-[8px] px-0 text-xs"
              aria-label="打开或收起运行日志"
              aria-controls="phone-log-sheet"
              onClick={(event) => logReveal.toggleFrom(pointFromEvent(event))}
            >
              日志
            </Button>
          )}
        </div>
      </div>

      <ConfirmActionDialog
        open={confirmOpen}
        onOpenChange={setConfirmOpen}
        title="确认上传云文档"
        description="服务端会再次只读复核本地文件、目标表与计划指纹；任何变化都会拒绝上传且一个格子都不写。"
        details={preview ? (
          <div className="space-y-2">
            <PreviewHeader preview={preview} effectiveTestTarget={effectiveTestTarget} />
            <PreviewCounts counts={counts} blocked={blocked} compact />
          </div>
        ) : null}
        acknowledge="我确认按此次预览结果写入云端，并已核对测试/正式目标与统计"
        confirmLabel="确认上传"
        busy={busy === 'upload'}
        danger={!effectiveTestTarget}
        onConfirm={performUpload}
      />

      <RecoveryResolveDialog
        target={resolveTarget}
        decision={resolveDecision}
        note={resolveNote}
        confirm={resolveConfirm}
        structureChecked={resolveStructure}
        error={resolveError}
        busy={resolveBusy}
        onDecision={(next) => {
          setResolveDecision(next)
          setResolveConfirm('')
          setResolveStructure(false)
          setResolveError('')
        }}
        onNote={setResolveNote}
        onConfirmText={setResolveConfirm}
        onStructureChecked={setResolveStructure}
        onClose={closeResolve}
        onSubmit={submitResolve}
      />
    </div>
  )
}

function FailureCallout({ view, fallbackTitle }: { view: FailureView; fallbackTitle: string }) {
  return (
    <Callout tone={view.tone} title={view.title || fallbackTitle}>
      <p>{view.detail}</p>
      <p className="mt-1 font-medium text-foreground">下一步：{view.nextStep}</p>
      {view.provenNoWrite && <p className="mt-1 text-muted-foreground">本次没有写入任何云端内容。</p>}
      {view.blockingRetained && <p className="mt-1 text-muted-foreground">阻断保持：修复/核对完成前不要重新上传。</p>}
    </Callout>
  )
}

/**
 * W3 管理员恢复/退场弹窗。
 *
 * 只提交契约白名单里的 4 个决策；没有「删除账本」「直接重传」「强制清除」入口。
 * 打开弹窗/刷新状态都不会发请求，只有点「提交处置」才调用 `wps_recovery_resolve`。
 */
function RecoveryResolveDialog({
  target, decision, note, confirm, structureChecked, error, busy,
  onDecision, onNote, onConfirmText, onStructureChecked, onClose, onSubmit,
}: {
  target: RecoveryOperationView | null
  decision: WpsRecoveryDecision | ''
  note: string
  confirm: string
  structureChecked: boolean
  error: string
  busy: boolean
  onDecision: (decision: WpsRecoveryDecision) => void
  onNote: (value: string) => void
  onConfirmText: (value: string) => void
  onStructureChecked: (value: boolean) => void
  onClose: () => void
  onSubmit: () => void
}) {
  const dialogRef = useRef<HTMLDivElement | null>(null)
  const headingRef = useRef<HTMLHeadingElement | null>(null)

  useEffect(() => {
    if (target) headingRef.current?.focus()
  }, [target])

  useEffect(() => {
    if (!target) return
    function onKeyDown(event: KeyboardEvent) {
      if (event.key === 'Escape') {
        event.preventDefault()
        onClose()
      }
    }
    document.addEventListener('keydown', onKeyDown)
    return () => document.removeEventListener('keydown', onKeyDown)
  }, [target, onClose])

  if (!target) return null
  const spec = decision ? DECISION_SPECS[decision] : null
  const decisions = target.decisions

  return (
    <div className="fixed inset-0 z-50 flex items-end justify-center bg-black/50 p-0 sm:items-center sm:p-4">
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby="wps-recovery-resolve-title"
        className="scroll-contain max-h-[92vh] w-full max-w-lg overflow-y-auto rounded-t-xl border bg-card p-3 shadow-xl sm:rounded-xl sm:p-4"
      >
        <h2
          id="wps-recovery-resolve-title"
          ref={headingRef}
          tabIndex={-1}
          className="text-sm font-semibold outline-none"
        >
          管理员恢复/退场处置
        </h2>
        <p className="mt-1 text-[11px] text-muted-foreground">
          本入口永不写云端（只改本地 journal 审计）；退场不会解除同一目标日期 + 云表的防重复闸门。
          失败时阻断保持，且没有「删除账本」或「直接重传」的捷径。
        </p>

        <dl className="mt-2 space-y-1 rounded-md border bg-secondary/30 px-2.5 py-2 text-[11px]">
          <div className="flex flex-wrap gap-x-2">
            <dt className="text-muted-foreground">目标日期：</dt>
            <dd className="tabular font-medium">{target.targetDate || UNKNOWN_TEXT}</dd>
          </div>
          <div className="flex flex-wrap gap-x-2">
            <dt className="text-muted-foreground">操作标识：</dt>
            <dd className="break-all font-mono font-medium">{target.operationId || UNKNOWN_TEXT}</dd>
          </div>
          <div className="flex flex-wrap gap-x-2">
            <dt className="text-muted-foreground">操作引用：</dt>
            <dd className="break-all font-mono">{target.operationRef || UNKNOWN_TEXT}</dd>
          </div>
          <div className="flex flex-wrap gap-x-2">
            <dt className="text-muted-foreground">当前状态：</dt>
            <dd className="font-medium">{target.statusLabel}{target.errorCode ? `（${target.errorCode}）` : ''}</dd>
          </div>
          <div className="flex flex-wrap gap-x-2">
            <dt className="text-muted-foreground">表数：</dt>
            <dd>{target.sheetCount || UNKNOWN_TEXT}</dd>
          </div>
          <div className="flex flex-wrap gap-x-2">
            <dt className="text-muted-foreground">允许动作：</dt>
            <dd>
              {target.allowedActions.length
                ? `${target.allowedActions.join('、')}（${target.allowedActionLabels.join('；')}）`
                : UNKNOWN_TEXT}
            </dd>
          </div>
          <div className="flex flex-wrap gap-x-2">
            <dt className="text-muted-foreground">风险：</dt>
            <dd className="text-warning">{target.risk}</dd>
          </div>
        </dl>

        <fieldset className="mt-2.5">
          <legend className="text-[12px] font-medium">处置动作（只有服务端白名单里的动作可选）</legend>
          {decisions.length === 0 ? (
            <p className="mt-1 rounded-md border bg-secondary/30 px-2 py-1.5 text-[11px] text-muted-foreground">
              该批次没有可用的恢复处置（例如已确认完成或只需重新预览）；请不要强行解阻断。
            </p>
          ) : (
            <div className="mt-1 space-y-1.5">
              {decisions.map((item) => {
                const itemSpec = DECISION_SPECS[item]
                return (
                  <label key={item} className="flex cursor-pointer gap-2 rounded-md border px-2.5 py-2 text-[11px]">
                    <input
                      type="radio"
                      name="wps-recovery-decision"
                      className="mt-0.5"
                      checked={decision === item}
                      disabled={busy}
                      onChange={() => onDecision(item)}
                    />
                    <span className="min-w-0">
                      <span className="font-medium">{itemSpec.label}</span>
                      <span className="ml-1 font-mono text-muted-foreground">{item}</span>
                      <span className="mt-0.5 block text-muted-foreground">影响：{itemSpec.effect}</span>
                      <span className="mt-0.5 block text-warning">风险：{itemSpec.risk}</span>
                    </span>
                  </label>
                )
              })}
            </div>
          )}
        </fieldset>

        <div className="mt-2.5 space-y-2">
          <label className="block text-[12px]" htmlFor="wps-resolve-note">
            <span className="font-medium">人工核对说明（≥4 字，写入 journal 审计）</span>
            <textarea
              id="wps-resolve-note"
              rows={2}
              className={addressTextareaClass}
              value={note}
              disabled={busy}
              onChange={(event) => onNote(event.target.value)}
              placeholder="例如：已只读核对 2026-09-20 云端排单表，仍无法判定是否写入"
            />
          </label>
          {spec?.requiresStructureCheck && (
            <label className="flex items-center gap-2 text-[11px]">
              <input
                type="checkbox"
                checked={structureChecked}
                disabled={busy}
                onChange={(event) => onStructureChecked(event.target.checked)}
              />
              <span>我确认已人工核对云端表结构（retire_guarded 必填）</span>
            </label>
          )}
          <label className="block text-[12px]" htmlFor="wps-resolve-confirm">
            <span className="font-medium">逐字确认：请输入当前的决策名 <code className="font-mono">{decision || '（先选择动作）'}</code></span>
            <TextInput
              id="wps-resolve-confirm"
              value={confirm}
              disabled={busy}
              onChange={(event) => onConfirmText(event.target.value)}
              placeholder={decision || '例如 retire_guarded'}
              autoComplete="off"
            />
          </label>
        </div>

        {error && (
          <p className="mt-2 rounded-md border border-destructive/40 bg-destructive/10 px-2 py-1.5 text-[11px] text-destructive" role="alert">
            {error}
          </p>
        )}

        <p className="mt-2 text-[11px] text-muted-foreground">
          提交只会修改本地审计；不会自动重传、不会删除账本、不会强制清除不确定结果。网络失败时结果未知，请只读核对后再决定。
        </p>

        <div className="mt-3 flex flex-wrap gap-2">
          <Button
            className="h-9 flex-1 rounded-[8px] text-sm"
            disabled={busy || decisions.length === 0}
            onClick={onSubmit}
          >
            {busy ? '提交中…' : '提交处置'}
          </Button>
          <Button variant="outline" className="h-9 rounded-[8px] text-sm" disabled={busy} onClick={onClose}>
            取消
          </Button>
        </div>
      </div>
    </div>
  )
}

function recoveryNextActionText(nextAction: string): string {
  const map: Record<string, string> = {
    manual_reconcile: '先人工核对云端与日志；核对完成前不要重新上传或补发',
    recover_journal: '需要先按日志恢复/修复，再重新预览；刷新状态本身不会解除阻断',
    repreview: '需要重新预览并生成新的上传令牌；不要直接重传旧令牌',
    fix_journal: '本地恢复日志不可用：请联系管理员修复，不要直接重试或重新上传',
    wait_for_recovery_lock: '另一个恢复/上传操作正在进行：请稍后再查询，不要重试',
    none: '没有待恢复操作',
  }
  return map[nextAction] || '恢复状态未知：请联系管理员只读核对，不要直接重试或重新上传'
}

function WpsRecoveryCard({ status, error, isAdmin, busy, onResolve }: {
  status: WpsRecoveryStatus | null
  error: { title: string; detail: string; nextStep: string } | null
  isAdmin: boolean
  busy: boolean
  onResolve: (operation: RecoveryOperationView) => void
}) {
  if (!status && !error) return null
  if (error) {
    return (
      <Callout tone="danger" title={error.title || '本地恢复记录读取失败'}>
        {error.detail} {error.nextStep} 只读刷新不会触发上传、解除阻断或自动重试。
      </Callout>
    )
  }
  if (!status) return null
  if (!status.ok) {
    const guidance = status.summary?.guidance || '恢复状态不可用：请联系管理员只读核对，不要直接重试或重新上传'
    const code = status.error_code ? `（${status.error_code}）` : ''
    // journal 损坏 / 版本不受支持 / 不可读 各自的下一步不同，不能合并成一句“重试”。
    const reasonText = `${status.error_code || ''} ${guidance}`
    const issue = journalIssueOf(status.error_code || '', reasonText)
    const journalHint = issue === 'version_unsupported'
      ? '本地日志版本不受支持：请升级到匹配版本或联系管理员迁移日志，不要删除日志，也不要直接重传。'
      : issue === 'write_failed'
        ? '本地日志持久化失败：先修复磁盘/权限，再重新只读核对；确认前不要重复提交。'
        : issue === 'corrupt'
          ? '本地账本/意图日志损坏：请联系管理员修复（可走恢复/退场入口），不要删除账本，也不要直接重传。'
          : issue === 'unreadable'
            ? '本地恢复日志不可读：请修复文件权限/磁盘后重试，不要删除账本，也不要直接重传。'
            : ''
    return (
      <Callout tone="danger" title="本地恢复记录不可读">
        {guidance}{code}。只读刷新不会伪造“没有待恢复”，不会上传，也不会自动解除阻断。
        下一步：{recoveryNextActionText(status.next_action)}
        {journalHint ? ` ${journalHint}` : ''}
      </Callout>
    )
  }

  const counts = status.counts || {}
  const summary = status.summary
  const pending = status.pending_operations ?? []
  const operations = status.operations ?? []
  const uncertainCount = Number(counts.uncertain ?? 0)
  const failedCount = Number(counts.failed ?? 0)
  const writingCount = Number(counts.writing ?? 0)
  const ledgerPendingCount = Number(counts.ledger_pending ?? 0)
  const retiredCount = retiredGuardedCount(status)
  const needsAttention = Boolean(summary?.needs_review)
    || pending.length > 0 || uncertainCount > 0 || failedCount > 0
    || writingCount > 0 || ledgerPendingCount > 0 || retiredCount > 0
    || status.next_action === 'manual_reconcile'
  const labelMap: Record<string, string> = {
    planned: '已计划', writing: '写入中', ledger_pending: '账本待落盘',
    verified: '已验证', uncertain: '待核对', failed: '失败', not_started: '未开始',
    retired_guarded: '已带审计退场（闸门保留）',
  }
  const isSummaryScope = status.scope === 'summary'
  const shown = isSummaryScope ? [] : (pending.length ? pending : operations).slice(0, 4)
  return (
    <Callout tone={needsAttention ? 'warning' : 'success'} title={needsAttention ? '待核对批次（只读可见）' : '本地恢复记录：无待核对批次'}>
      <p className="text-muted-foreground">
        本次刷新为只读查询：不会上传、不会解除阻断、不会自动重试，也不会自动恢复写入。
        {status.queried_cloud === false ? ' 当前展示的是本地 journal 记录，本查询未调用云端。' : ' 数据来自只读恢复查询。'}
      </p>
      {status.contains_cloud_checked_records && (
        <p className="mt-0.5 text-muted-foreground">
          包含此前已做云端只读核对（cloud_checked）的记录；与仅本地 journal 记录不同，逐表状态会以安全摘要字段标注。
        </p>
      )}
      {retiredCount > 0 && (
        <p className="mt-1 rounded-md border border-warning/40 bg-warning/10 px-2 py-1.5 text-warning">
          有 {retiredCount} 个批次已带审计退场，但同一「目标日期 + 云表」仍被防重复闸门阻断：证实之前不能重新上传。
        </p>
      )}
      {needsAttention && (
        <>
          <div className="mt-1.5 flex flex-wrap gap-1.5">
            {Object.entries(labelMap).map(([key, label]) => {
              const value = Number(summary?.[`${key}_count` as keyof typeof summary] ?? counts[key] ?? 0)
              const display = key === 'uncertain' ? (summary?.uncertain_count ?? value)
                : key === 'failed' ? (summary?.failed_count ?? value)
                  : key === 'not_started' ? (summary?.not_started_count ?? value)
                    : key === 'retired_guarded' ? retiredCount
                      : value
              return display > 0 ? (
                <span key={key} className="rounded-full border bg-card px-2 py-0.5 text-[10px]">
                  {label} {display}
                </span>
              ) : null
            })}
          </div>
          {isSummaryScope && (
            <p className="mt-1.5 rounded-md border border-border bg-secondary/30 px-2 py-1.5 text-muted-foreground">
              当前账号为安全摘要视图：不显示批次明细、操作引用或客户数据。摘要明确待核对时不会显示成功，也不会开放重传按钮。
            </p>
          )}
          {!isSummaryScope && shown.length > 0 && (
            <div className="mt-2 space-y-2">
              {shown.map((operation) => (
                <WpsRecoveryOperationCard
                  key={operation.operation_id || operation.operation_ref}
                  operation={operation}
                  isAdmin={isAdmin}
                  busy={busy}
                  onResolve={onResolve}
                />
              ))}
            </div>
          )}
          {!isSummaryScope && shown.length === 0 && (
            <p className="mt-1.5 text-muted-foreground">没有可展示的批次明细；请勿凭摘要猜测，先完成人工只读核对。</p>
          )}
        </>
      )}
      <p className="mt-1.5 font-medium text-foreground">
        下一步：{summary?.guidance || recoveryNextActionText(status.next_action)}
      </p>
    </Callout>
  )
}

function WpsRecoveryOperationCard({ operation, isAdmin, busy, onResolve }: {
  operation: WpsRecoveryOperation
  isAdmin: boolean
  busy: boolean
  onResolve: (view: RecoveryOperationView) => void
}) {
  const view = recoveryOperationView(operation)
  const actionCodes = view?.allowedActions.join('、') || ''
  const actionLabels = view?.allowedActionLabels.join('；') || ''
  const canResolve = isAdmin && (view?.decisions.length ?? 0) > 0
  return (
    <details className="rounded-md border bg-card px-2.5 py-2 text-[11px]" open={Boolean(view?.blockingRetained)}>
      <summary className="cursor-pointer">
        <span className="font-medium">{operation.operation_id || operation.operation_ref || '（无可展示引用）'}</span>
        <span className="ml-2 text-muted-foreground">
          {view?.statusLabel || operation.status || 'unknown'} · {operation.pending ? '待恢复' : '历史记录'}
          {operation.target_date ? ` · 目标日期 ${operation.target_date}` : ''}
          {operation.sheet_count ? ` · ${operation.sheet_count} 张表` : ''}
        </span>
      </summary>
      <p className="mt-1 text-muted-foreground">
        {operation.cloud_checked ? '该批次含云端只读核对记录' : '仅本地 journal 记录'}
        {operation.error_code ? ` · ${operation.error_code}` : ''}
        {actionCodes ? ` · 允许动作 ${actionCodes}${actionLabels ? `（${actionLabels}）` : ''}` : ''}
        {operation.manual_required ? ' · 需要人工处理' : ''}
      </p>
      {view && <p className="mt-1 text-warning">风险：{view.risk}</p>}
      {view?.blockingRetained && (
        <p className="mt-1 text-muted-foreground">当前仍然阻断：同目标日期的重新写入会被服务端以 uncertain + manual_reconcile 拒绝。</p>
      )}
      {operation.sheets?.length ? (
        <ul className="mt-1 space-y-0.5 text-muted-foreground">
          {operation.sheets.map((sheet, index) => (
            <li key={`${operation.operation_id}-${sheet.target_ref || index}`} className="break-words">
              目标 {sheet.target_ref || '（无引用）'}：{sheet.status || 'unknown'}
              {sheet.cloud_checked ? '（云端只读核对）' : '（本地记录）'}
              {sheet.error_code ? ` · ${sheet.error_code}` : ''}
              {sheet.allowed_next_actions?.length ? ` · 允许动作 ${sheet.allowed_next_actions.join('、')}` : ''}
              {sheet.manual_required ? ' · 需要人工处理' : ''}
            </li>
          ))}
        </ul>
      ) : null}
      {view && view.decisions.length > 0 && (
        <div className="mt-1.5">
          {canResolve ? (
            <Button variant="outline" size="sm" disabled={busy} onClick={() => onResolve(view)}>
              管理员恢复处置…
            </Button>
          ) : (
            <p className="text-muted-foreground">
              当前账号没有恢复处置权限（该入口仅管理员可用）；如需解除阻断请联系管理员。
            </p>
          )}
        </div>
      )}
    </details>
  )
}

function SaveIndicator({ state, onRetry, onSaveNow }: {
  state: ReturnType<typeof useConfigSave>['state']
  onRetry: () => void
  onSaveNow: () => void
}) {
  const view = saveStateView(state)
  return (
    <span className="inline-flex min-w-0 items-center gap-1">
      <span className={cn(
        'truncate',
        view.tone === 'warn' && 'text-warning',
        view.tone === 'success' && 'text-success',
        view.tone === 'progress' && 'text-primary',
        view.tone === 'muted' && 'text-muted-foreground',
      )}>{view.label}</span>
      {view.canRetry && <button type="button" className="font-medium text-primary underline" onClick={onRetry}>重试保存</button>}
      {!view.canRetry && state.phase === 'dirty' && <button type="button" className="font-medium text-primary underline" onClick={onSaveNow}>立即保存</button>}
    </span>
  )
}

function PreviewHeader({ preview, effectiveTestTarget }: { preview: WpsPreviewResult; effectiveTestTarget: boolean }) {
  return (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-1 rounded-md border bg-secondary/40 px-2.5 py-1.5 text-[11px]">
      <span>目标日期：<b className="tabular text-foreground">{preview.target_date || '—'}</b></span>
      <span>写入目标：<b className={effectiveTestTarget ? 'text-success' : 'text-warning'}>{effectiveTestTarget ? '测试副本' : '正式目标'}</b></span>
      <span>创建于：<b className="tabular">{preview.created_at || '—'}</b></span>
      <span>过期于：<b className="tabular">{preview.expires_at || '—'}</b></span>
    </div>
  )
}

function PreviewCounts({ counts, blocked, compact = false }: {
  counts: ReturnType<typeof previewCounts>
  blocked: ReturnType<typeof blockedItems>
  compact?: boolean
}) {
  if (!counts) return null
  // W6：预览阶段只有**计划**口径，标签必须写“计划”，不能写成“已更新/已完成”。
  const cells = [
    { label: '计划更新', value: counts.update, tone: 'progress' as const },
    { label: '计划新增', value: counts.append, tone: 'success' as const },
    { label: '计划跳过', value: counts.skipped, tone: 'warning' as const },
    { label: '计划不变', value: counts.unchanged, tone: 'neutral' as const },
    { label: '计划阻断', value: counts.blocked, tone: 'danger' as const },
    { label: '计划警告', value: counts.warned, tone: 'warning' as const },
  ]
  return (
    <div>
      <p className="mb-1 text-[11px] text-muted-foreground">
        以下全部是「计划」口径（还没执行）：实际写入行数只在执行后由服务端逐格回读证明，无法证明时显示“{UNKNOWN_TEXT}”。
      </p>
      <div className={cn('grid gap-1.5', compact ? 'grid-cols-3' : 'grid-cols-3 sm:grid-cols-6')}>
        {cells.map((cell) => <SummaryTile key={cell.label} label={cell.label} value={cell.value} tone={cell.tone} />)}
      </div>
      {blocked.length > 0 && (
        <p className="mt-1.5 text-[11px] text-destructive">
          阻断：{blocked.map((item) => `${item.sheet}${item.reason ? `（${item.reason}）` : ''}`).join('；')}
        </p>
      )}
    </div>
  )
}

function StructuredPreview({
  preview, freshness, counts, blocked, warnings, detailOpen, onToggleDetail,
}: {
  preview: WpsPreviewResult
  freshness: ReturnType<typeof previewFreshness>
  counts: ReturnType<typeof previewCounts>
  blocked: ReturnType<typeof blockedItems>
  warnings: string[]
  detailOpen: boolean
  onToggleDetail: () => void
}) {
  return (
    <section className="mb-3.5 rounded-lg border bg-card p-3" aria-label="结构化预览">
      <div className="flex flex-wrap items-center gap-2">
        <h3 className="text-[13px] font-semibold">结构化预览</h3>
        <StatusPill label={freshness.fresh ? '有效' : '已失效'} tone={freshness.fresh ? 'success' : 'warning'} />
      </div>
      <p className="mt-1 text-[11px] text-muted-foreground">{freshness.reason || '预览有效，可确认上传'}</p>
      <div className="mt-2">
        <PreviewHeader preview={preview} effectiveTestTarget={preview.test_mode} />
      </div>
      <div className="mt-2"><PreviewCounts counts={counts} blocked={blocked} /></div>

      {warnings.length > 0 && (
        <details className="mt-2 rounded-md border border-warning/40 bg-warning/10 px-2.5 py-2 text-[11px]" open={false}>
          <summary className="cursor-pointer font-medium text-warning">风险警告 {warnings.length} 条（点击展开，全部保留）</summary>
          <ul className="mt-1.5 list-disc space-y-0.5 pl-4 text-foreground">
            {warnings.map((warning, index) => <li key={`${warning}-${index}`}>{warning}</li>)}
          </ul>
        </details>
      )}

      <div className="mt-2 grid gap-1.5 sm:grid-cols-2">
        {preview.tables.map((table) => (
          <details key={table.sheet} className="rounded-md border bg-secondary/20 px-2.5 py-2 text-[11px]">
            <summary className="cursor-pointer font-medium text-foreground">
              {table.sheet}
              <span className="ml-2 font-normal text-muted-foreground">
                计划更新 {table.counts?.to_update ?? UNKNOWN_TEXT} · 计划新增 {table.counts?.to_append ?? UNKNOWN_TEXT} · 计划跳过 {table.counts?.skipped ?? UNKNOWN_TEXT} · 计划不变 {table.counts?.unchanged ?? UNKNOWN_TEXT}
              </span>
            </summary>
            {table.blocked_reason && <p className="mt-1.5 text-destructive">阻断：{table.blocked_reason}</p>}
            {table.target_header && (
              <p className="mt-1.5 text-muted-foreground">目标列：{table.target_header}（第 {table.target_col} 列）</p>
            )}
            {table.sort?.enabled && <p className="mt-1 text-muted-foreground">排序：{table.sort.sort_range || '按配置'}，键列 {table.sort.sort_key_col}</p>}
            {table.warnings?.length > 0 && (
              <ul className="mt-1 list-disc space-y-0.5 pl-4 text-warning">
                {table.warnings.map((warning, index) => <li key={`${warning}-${index}`}>{warning}</li>)}
              </ul>
            )}
            {table.changes?.length > 0 && (
              <ul className="mt-1 max-h-44 space-y-0.5 overflow-auto text-muted-foreground">
                {table.changes.map((change, index) => (
                  <li key={`${change.name}-${change.row}-${index}`} className="break-words">
                    {change.kind === 'new' ? '新增' : change.needs_write ? '更新' : '不变'} {change.name || '—'}
                    {change.phone ? `（${change.phone}）` : ''}
                    {change.detail ? `：${change.detail}` : ''}
                  </li>
                ))}
              </ul>
            )}
            {!table.changes?.length && <p className="mt-1 text-muted-foreground">无结构化变更行</p>}
          </details>
        ))}
      </div>

      <button type="button" className="mt-2 text-[11px] font-medium text-primary underline" onClick={onToggleDetail}>
        {detailOpen ? '收起后端全文' : '查看后端全文（默认折叠）'}
      </button>
      {detailOpen && preview.text && (
        <pre className="mt-1.5 max-h-60 overflow-auto whitespace-pre-wrap break-all rounded-md border bg-muted/40 p-2 text-[11px]">{preview.text}</pre>
      )}
    </section>
  )
}

function ResultCard({
  result, action, onAction,
}: {
  result: WpsUploadResult
  action: ReturnType<typeof uploadNextAction> | null
  onAction: () => void
}) {
  // W6：标题/语气只由 uploadHeadline 决定；只有 ok && success 才可能出现“已完成”。
  const headline = uploadHeadline(result)
  const failure = uploadFailureView(result)
  const lines = uploadCountLines(result)
  const counts = uploadCountView(result)
  const tone: Tone = headline.tone === 'success' ? 'success' : headline.tone === 'danger' ? 'danger' : 'neutral'
  // 机器可读的结果语义：浏览器检查直接断言这些属性，不依赖中文措辞是否被别的卡片遮住。
  return (
    <div
      data-wps-result="upload"
      data-result-status={String(result.status || '')}
      data-result-complete={headline.impliesComplete ? 'true' : 'false'}
      data-rows-unknown={counts.rowsUnknown ? 'true' : 'false'}
      data-verified-rows={counts.verifiedRows === null ? 'unknown' : String(counts.verifiedRows)}
      data-proven-no-write={counts.provenNoWrite ? 'true' : 'false'}
    >
    <Callout tone={tone} title={failure?.title || headline.title}>
      <p>{failure?.detail || result.reason || (headline.impliesComplete ? '云端写入完成' : headline.nextStep)}</p>
      {counts.provenNoWrite && (
        <p className="mt-1 text-muted-foreground">本次未写入任何内容（服务端已证明零云端写入）。</p>
      )}
      {!headline.impliesComplete && !counts.provenNoWrite && (
        <p className="mt-1 rounded-md border border-warning/40 bg-warning/10 px-2 py-1.5 text-warning">
          这不是“已完成”：结果没有拿到可核实的成功证据，请不要当作成功展示或据此重传。
        </p>
      )}
      <ul className="mt-1 space-y-0.5 text-muted-foreground">
        {lines.map((line) => (
          <li key={line.label} className={cn('break-words', line.tone === 'warning' && 'text-warning', line.tone === 'success' && 'text-foreground')}>
            <span className="font-medium text-foreground">{line.label}：</span>{line.value}
          </li>
        ))}
      </ul>
      {failure && <p className="mt-1 font-medium text-foreground">下一步：{failure.nextStep}</p>}
      {failure?.blockingRetained && (
        <p className="mt-1 text-muted-foreground">阻断保持：修复/核对完成前不要重新上传，也不要删除账本或直接重传。</p>
      )}
      {result.failed_sheets?.length ? <p className="mt-1 text-destructive">失败表：{result.failed_sheets.join('、')}</p> : null}
      {action && (
        <button type="button" className="mt-1.5 font-medium text-primary underline" onClick={onAction}>
          {action.label}
        </button>
      )}
    </Callout>
    </div>
  )
}

function CopyCheckCard({ check }: { check: WpsCopyCheck }) {
  const tone: Tone = !check.ok ? 'danger' : check.all_aligned ? 'success' : 'warning'
  return (
    <Callout tone={tone} title="副本一致性核对">
      {!check.ok ? (
        <p>{check.reason || '核对失败'}</p>
      ) : check.all_aligned ? (
        <p>6 张副本与正式表行数/姓名序列一致，测试结果可代表线上情况。</p>
      ) : (
        <>
          <p>有副本已过时：{(check.drifted ?? []).join('、') || '详见下方'}。建议先在云端同步副本或改用正式目标重新预览。</p>
          <div className="mt-1 space-y-0.5 text-muted-foreground">
            {(check.tables ?? []).map((table) => (
              <p key={table.sheet}>
                {table.sheet}：正式 {table.production_rows ?? '—'} / 副本 {table.rows ?? '—'}
                {table.missing?.length ? `，副本缺 ${table.missing.join('、')}` : ''}
                {table.extra?.length ? `，副本多 ${table.extra.join('、')}` : ''}
              </p>
            ))}
          </div>
        </>
      )}
    </Callout>
  )
}
