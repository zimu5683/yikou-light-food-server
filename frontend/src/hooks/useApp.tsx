/**
 * 全局应用状态：桥接事件 + operation_status 权威状态 → React store 的唯一入口。
 *
 * 职责（对应 app/api/bridge.py 事件协议与 operation_status 协议）：
 * - 握手并装载初始状态（version / config / passwords / operation / operations）
 * - 日志、status、task:*、update:*、decision 事件分发
 * - 断线/超时只改变“连接状态”，不把网络失败等同于任务失败
 * - 断线恢复后查询 operation_status() + bridge_ready() 恢复权威任务状态
 * - 表单动作、任务动作、文件对话框、交互确认、错误条
 */
import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from 'react'
import { toast } from 'sonner'
import { shouldAutoCheckUpdates } from '@/lib/updateCheck'
import { classifyRequestError } from '@/lib/requestError'
import {
  addressFromPendingItem,
  beginInteractionSubmit,
  captchaFromPendingItem,
  decisionFromPendingItem,
  dedupePendingInteractions,
  endInteractionSubmit,
  interactionMetaFromRequest,
  isAddressKind,
  isCaptchaKind,
  isDecisionKind,
  readPendingInteractionMeta,
  submissionOutcome,
  writePendingInteractionMeta,
  type PendingInteractionMeta,
} from '@/lib/interactionRecovery'
import { taskOutcomeView } from '@/lib/taskOutcome'
import { evaluatePasswordClearResult } from '@/lib/credentialClear'
import {
  api,
  connectBridge,
  isApiReady,
  isWebTransport,
  onBridgeEvent,
  onRequestIssue,
  pullBridgeEvents,
  type AddressInputRequest,
  type AppState,
  type CaptchaRequest,
  type DecisionRequest,
  type GlobalRequestIssue,
  type OperationInfo,
  type OperationStatusResult,
  type OrderFormPayload,
  type SssFormPayload,
  type StatusState,
  type BridgeEvent,
  type Transport,
  type UpdateAvailable,
  type UpdateProgress,
} from '@/lib/bridge'
import {
  legacyStatusFromOperation,
  operationViewFromAuthority,
  operationViewFromStatus,
  type ConnectionState,
  type RecoveryState,
} from '@/lib/operationStatus'

import {
  AppContext,
  UpdateAvailableContext,
  type AppStateBundle,
  type ClearPasswordResult,
  type InteractionResolveResult,
  type LogRow,
  type TaskMode,
} from './appContext'

const timeText = () => new Date().toLocaleTimeString('zh-CN', { hour12: false })

function readSessionStorage(): Storage | null {
  try {
    return window.sessionStorage
  } catch {
    return null
  }
}

export function AppProvider({ children }: { children: ReactNode }) {
  const [ready, setReady] = useState(false)
  const [mocked, setMocked] = useState(false)
  const [transport, setTransport] = useState<Transport>('mock')
  const [authError, setAuthError] = useState('')
  const [hasValidToken, setHasValidToken] = useState(false)
  const [version, setVersion] = useState('')
  const [status, setStatus] = useState<StatusState>('ready')
  // 默认 false（受限）：握手失败时退化为「看不到敏感项」，而不是「全都能看」
  const [isAdmin, setIsAdmin] = useState(false)
  const [config, setConfig] = useState<AppState['config'] | null>(null)
  const [passwords, setPasswords] = useState({ order: '', sss: '' })
  const [passwordReset, setPasswordReset] = useState<{ mode: 'order' | 'sss' | null; nonce: number }>({
    mode: null,
    nonce: 0,
  })
  const [logs, setLogs] = useState<LogRow[]>([])
  const [decision, setDecision] = useState<DecisionRequest | null>(null)
  const [captcha, setCaptcha] = useState<CaptchaRequest | null>(null)
  const [addressInput, setAddressInput] = useState<AddressInputRequest | null>(null)
  const [updateAvailable, setAvailableState] = useState<UpdateAvailable | null>(null)
  const [updateProgress, setUpdateProgress] = useState<UpdateProgress | null>(null)
  const [updatePermissionRequired, setUpdatePermissionRequired] = useState(false)
  const [updateError, setUpdateError] = useState('')
  const [platform, setPlatform] = useState<'android' | 'web'>('web')
  const [canSelfUpdate, setCanSelfUpdate] = useState(false)
  const [mode, setMode] = useState<TaskMode>('order')
  const [connection, setConnection] = useState<ConnectionState>('connecting')
  const [recovery, setRecovery] = useState<RecoveryState>('idle')
  const [operation, setOperation] = useState<OperationStatusResult | null>(null)
  const [operations, setOperations] = useState<OperationInfo[]>([])
  const [lastTaskMessage, setLastTaskMessage] = useState('')
  const [requestIssues, setRequestIssues] = useState<GlobalRequestIssue[]>([])
  const [pendingRecoveryMeta, setPendingRecoveryMeta] = useState<PendingInteractionMeta | null>(
    () => readPendingInteractionMeta(readSessionStorage()),
  )
  const [redactedPendingCount, setRedactedPendingCount] = useState(0)
  const logId = useRef(0)
  const operationRef = useRef('')
  const localInteractionIdsRef = useRef({ decision: '', captcha: '', address: '' })
  const restoringPendingInteractions = useRef(false)
  const connectionRef = useRef<ConnectionState>('connecting')
  const recoveryTimer = useRef<number | undefined>(undefined)
  const interactionPending = useRef(new Set<string>())
  const passwordClearPending = useRef(new Set<string>())

  useEffect(() => {
    connectionRef.current = connection
  }, [connection])

  useEffect(() => () => {
    if (recoveryTimer.current !== undefined) window.clearTimeout(recoveryTimer.current)
  }, [])

  const appendLog = useCallback((entry: Omit<LogRow, 'id'>) => {
    setLogs((prev) => [...prev.slice(-1999), { ...entry, id: (logId.current += 1) }])
  }, [])

  // 请求错误统一广播 → 全局错误条；错误分类在 lib/requestError.ts。
  useEffect(() => onRequestIssue((issue) => {
    setRequestIssues((prev) => [...prev.slice(-2), issue])
  }), [])

  useEffect(() => {
    operationRef.current = operation?.operation_id ?? ''
  }, [operation?.operation_id])

  useEffect(() => {
    localInteractionIdsRef.current = {
      decision: decision?.id ?? '',
      captcha: captcha?.id ?? '',
      address: addressInput?.id ?? '',
    }
  }, [addressInput?.id, captcha?.id, decision?.id])

  // 待处理交互：只写入 kind/id/operation_id 安全元数据；清理由 resolve 成功/任务结束驱动。
  // 不在 effect 里 setState，避免把刷新恢复提示做成级联渲染。
  const recordPendingInteraction = useCallback((kind: PendingInteractionMeta['kind'], id: string) => {
    const meta = interactionMetaFromRequest(kind, id, operationRef.current)
    setPendingRecoveryMeta(meta)
    writePendingInteractionMeta(readSessionStorage(), meta)
  }, [])

  const clearPendingRecovery = useCallback(() => {
    setPendingRecoveryMeta(null)
    writePendingInteractionMeta(readSessionStorage(), null)
  }, [])

  const dismissPendingRecovery = clearPendingRecovery

  /**
   * 只读恢复 pending interaction：调用 A 的 pending_interactions()，
   * 按 interaction_id 去重；已有本地输入时不覆盖；只恢复弹窗，不自动提交。
   * 返回 false 表示后端无法恢复，由安全降级 banner 兜底。
   */
  const restorePendingInteractions = useCallback(async (operationId = ''): Promise<boolean> => {
    if (!isWebTransport() || restoringPendingInteractions.current) return false
    restoringPendingInteractions.current = true
    try {
      const result = await api().pending_interactions(operationId)
      if (!result?.ok) return false
      const items = dedupePendingInteractions(
        Array.isArray(result.interactions) ? result.interactions : [],
      )
      setRedactedPendingCount(items.filter((item) => item.request_redacted).length)
      if (items.length === 0) return false

      const currentIds = localInteractionIdsRef.current
      let restored = false

      const decisionItem = items.filter((item) => isDecisionKind(String(item.kind))).at(-1)
      if (decisionItem && decisionItem.interaction_id !== currentIds.decision) {
        const next = decisionFromPendingItem(decisionItem)
        if (next) {
          setDecision(next)
          restored = true
        }
      }

      const captchaItem = items.filter((item) => isCaptchaKind(String(item.kind))).at(-1)
      if (captchaItem && captchaItem.interaction_id !== currentIds.captcha) {
        const next = captchaFromPendingItem(captchaItem)
        if (next) {
          setCaptcha(next)
          restored = true
        }
      }

      const addressItem = items.filter((item) => isAddressKind(String(item.kind))).at(-1)
      if (addressItem && addressItem.interaction_id !== currentIds.address) {
        const next = addressFromPendingItem(addressItem)
        if (next) {
          setAddressInput(next)
          restored = true
        }
      }

      // 服务端已给出可恢复项；本地安全降级 banner 不再需要。
      if (restored) clearPendingRecovery()
      return restored
    } catch {
      return false
    } finally {
      restoringPendingInteractions.current = false
    }
  }, [clearPendingRecovery])

  const applyAuthority = useCallback((
    next: OperationStatusResult | OperationInfo | null | undefined,
    fallbackStatus?: StatusState,
    fallbackOperations?: OperationInfo[],
  ) => {
    if (next && 'operations' in next) {
      const authority = next as OperationStatusResult
      setOperation(authority)
      setOperations(authority.operations?.length ? authority.operations : (fallbackOperations ?? []))
      setStatus(legacyStatusFromOperation(authority))
      const message = (authority.next_action || authority.reason || '').trim()
      if (message) setLastTaskMessage(message)
      return
    }
    if (fallbackStatus) setStatus(fallbackStatus)
    if (fallbackOperations) setOperations(fallbackOperations)
  }, [])

  const applyState = useCallback((state: AppState, withMock = false) => {
    setVersion(state.version)
    setStatus(state.status)
    setIsAdmin(state.is_admin === true)
    setPlatform(state.platform === 'android' ? 'android' : 'web')
    setCanSelfUpdate(state.can_self_update === true)
    setConfig(state.config)
    setPasswords(state.passwords)
    setHasValidToken(!withMock && !authError)
    applyAuthority(state.operation, state.status, state.operations)
  }, [applyAuthority, authError])

  // ---- 桥接握手 ----
  useEffect(() => {
    let active = true
    connectBridge().then(({ state, mocked: nextMocked, transport: nextTransport, authError: nextAuthError }) => {
      if (!active) return
      setReady(true)
      setMocked(nextMocked)
      setTransport(nextTransport)
      setAuthError(nextAuthError)
      setHasValidToken(!nextMocked && !nextAuthError)
      applyState(state, nextMocked)
      // 连接成功后才算 connected；握手失败无 token/无服务端时保持断线可核对态。
      setConnection(nextMocked ? 'disconnected' : 'connected')
      if (!nextMocked) {
        void restorePendingInteractions(state.operation?.operation_id || '')
        api()
          .frontend_report({ kind: 'ready', version: state.version, status: state.status })
          .catch((error) => appendLog({
            ts: timeText(), level: 'WARN',
            msg: `前端就绪上报失败：${classifyRequestError(error).detail}`,
          }))
      }
      if (!nextMocked) {
        let shouldCheck = true
        try {
          shouldCheck = shouldAutoCheckUpdates(window.localStorage)
        } catch {
          shouldCheck = true
        }
        if (shouldCheck) {
          window.setTimeout(() => {
            api().check_updates(false).catch((error) => {
              console.warn('自动检查更新失败', error)
            })
          }, 700)
        }
      }
    })
    return () => {
      active = false
    }
  }, [appendLog, applyState, restorePendingInteractions])

  const refreshOperation = useCallback(async (): Promise<OperationStatusResult | null> => {
    if (!isWebTransport()) return null
    try {
      const next = await api().operation_status()
      applyAuthority(next)
      setConnection('connected')
      return next
    } catch {
      // 网络不确定：保留已有权威状态，只把连接标为断开；绝不把本次请求失败写成任务失败。
      setConnection('disconnected')
      return null
    }
  }, [applyAuthority])

  const reconnect = useCallback(async (): Promise<boolean> => {
    if (!isWebTransport()) return false
    setRecovery('checking')
    if (recoveryTimer.current !== undefined) window.clearTimeout(recoveryTimer.current)
    try {
      const [state, latest] = await Promise.all([
        api().bridge_ready(),
        api().operation_status(),
      ])
      applyState(state)
      applyAuthority(latest ?? state.operation, state.status, state.operations)
      setConnection('connected')
      setRecovery('recovered')
      void restorePendingInteractions(state.operation?.operation_id || '')
      recoveryTimer.current = window.setTimeout(() => setRecovery('idle'), 1400)
      return true
    } catch {
      setConnection('disconnected')
      setRecovery('unavailable')
      return false
    }
  }, [applyAuthority, applyState, restorePendingInteractions])

  // ---- 事件应用 ----
  const applyEvent = useCallback(
    (event: BridgeEvent) => {
      switch (event.event) {
        case 'log': {
          if (/^\s*-{3,}\s*$/.test(event.payload.msg)) break
          appendLog(event.payload)
          break
        }
        case 'events:dropped': {
          const critical = (event.payload.critical_dropped_count ?? 0) > 0
          appendLog({
            ts: timeText(),
            level: critical ? 'ERROR' : 'WARN',
            msg: event.payload.message,
          })
          if (critical) toast.error(event.payload.message, { duration: 10000 })
          break
        }
        case 'status':
          setStatus(event.payload.state)
          // 事件只是提示；权威 active/结果由 operation_status 决定。
          void refreshOperation()
          break
        case 'task:done': {
          const payload = event.payload
          const outcome = taskOutcomeView(payload)
          // 服务端结束任务时会取消所有待处理交互；同时清掉本地弹窗，避免陈旧提交。
          setDecision(null)
          setCaptcha(null)
          setAddressInput(null)
          clearPendingRecovery()
          setRedactedPendingCount(0)
          setLastTaskMessage(payload.message)
          void refreshOperation()
          if (outcome.toast === 'success') toast.success(outcome.message)
          else if (outcome.toast === 'error') toast.error(outcome.message, { duration: 10000 })
          else if (outcome.toast === 'warning') toast.warning(outcome.message, { duration: 10000 })
          else toast.info(outcome.message)
          break
        }
        case 'task:error': {
          const payload = event.payload
          // 后端对 task:error 也带 result_status；老事件可能只有 status。
          // 只有两者都缺失时才回退 'error'，避免把 blocked_concurrent 之类
          // 非失败状态硬编码成失败（R6 §11 第 4 条）。
          const rawStatus = String(payload.result_status || payload.status || '')
          const outcome = taskOutcomeView({
            ...payload, status: rawStatus || 'error', ok: false, success: false,
          })
          setDecision(null)
          setCaptcha(null)
          setAddressInput(null)
          clearPendingRecovery()
          setRedactedPendingCount(0)
          setLastTaskMessage(payload.message)
          // R6-9：blocked_concurrent 是「另一个任务正在运行」，本次没发任何请求；
          // 必须按 WARN 展示，不能写成任务失败，也不需要站内对账。
          const concurrent = outcome.statusKey === 'blocked_concurrent'
          setStatus(outcome.statusKey)
          void refreshOperation()
          if (concurrent) toast.warning(outcome.message, { duration: 10000 })
          else toast.error(outcome.message, { duration: 10000 })
          break
        }
        case 'update:available':
          setAvailableState(event.payload)
          setUpdateProgress(null)
          setUpdatePermissionRequired(false)
          setUpdateError('')
          break
        case 'update:latest':
          setUpdateProgress(null)
          setUpdatePermissionRequired(false)
          if (event.payload.manual) {
            toast.info(`当前已是最新版本（${event.payload.current}）。`)
          }
          break
        case 'update:progress':
          setUpdateProgress(event.payload)
          setUpdateError('')
          setUpdatePermissionRequired(false)
          break
        case 'update:permission_required':
          setUpdatePermissionRequired(true)
          setUpdateProgress(null)
          break
        case 'update:cancelled':
          setUpdateProgress(null)
          setUpdatePermissionRequired(false)
          toast.info(event.payload.message || '已取消更新下载')
          break
        case 'update:error':
          setUpdateProgress(null)
          setUpdateError(event.payload.message)
          toast.error(`更新失败：${event.payload.message}`, { duration: 8000 })
          break
        case 'desktop_update:available': {
          const payload = event.payload
          const url = payload.html_url
            || `https://github.com/zimu5683/yikou-light-food-desktop/releases/tag/${payload.tag}`
          toast.info(`桌面版发布新版本 ${payload.tag}`, {
            description: '网页版不会自动更新；如需同步功能，请手动调整代码。',
            duration: 12000,
            action: {
              label: '查看桌面版发布页',
              onClick: () => { api().open_external(url).catch((error) => {
                toast.error(`打开发布页失败：${classifyRequestError(error).detail}`)
              }) },
            },
          })
          break
        }
        case 'decision':
          setDecision(event.payload)
          recordPendingInteraction('decision', event.payload.id)
          break
        case 'captcha':
          setCaptcha(event.payload)
          recordPendingInteraction('captcha', event.payload.id)
          break
        case 'address_input':
          setAddressInput(event.payload)
          recordPendingInteraction('address_input', event.payload.id)
          break
      }
    },
    [appendLog, clearPendingRecovery, recordPendingInteraction, refreshOperation],
  )

  useEffect(() => {
    const off = onBridgeEvent(applyEvent)
    return off
  }, [applyEvent])

  // 事件轮询：断线期间保留 cursor，恢复后重放缺失的 task:done / task:error。
  useEffect(() => {
    if (!ready || mocked) return
    let stopped = false
    let timer: number | undefined
    const poll = async () => {
      try {
        await pullBridgeEvents()
      } catch {
        // 超时或临时断线：下一轮继续，不改变任务状态。
      } finally {
        if (!stopped) timer = window.setTimeout(poll, 800)
      }
    }
    timer = window.setTimeout(poll, 120)
    return () => {
      stopped = true
      if (timer !== undefined) window.clearTimeout(timer)
    }
  }, [ready, mocked, applyEvent])

  // operation_status 心跳：成功即权威；失败只标记连接断开。
  useEffect(() => {
    if (!ready || mocked || !isWebTransport()) return
    let stopped = false
    let timer: number | undefined
    const tick = async () => {
      if (stopped) return
      const wasDown = connectionRef.current !== 'connected'
      const latest = await refreshOperation()
      if (stopped) return
      if (latest && wasDown) {
        // 恢复时同时查 bridge_ready，以服务器权威覆盖断线期间可能错过的状态。
        setRecovery('checking')
        try {
          const state = await api().bridge_ready()
          if (stopped) return
          applyState(state)
          applyAuthority(state.operation ?? latest, state.status, state.operations)
          setRecovery('recovered')
          void restorePendingInteractions(state.operation?.operation_id || '')
          if (recoveryTimer.current !== undefined) window.clearTimeout(recoveryTimer.current)
          recoveryTimer.current = window.setTimeout(() => setRecovery('idle'), 1400)
        } catch {
          if (!stopped) setRecovery('unavailable')
        }
      }
      if (!stopped) timer = window.setTimeout(tick, 15_000)
    }
    timer = window.setTimeout(tick, 12_000)
    return () => {
      stopped = true
      if (timer !== undefined) window.clearTimeout(timer)
    }
  }, [ready, mocked, refreshOperation, applyState, applyAuthority, restorePendingInteractions])

  // ---- 动作 ----
  const startOrder = useCallback(async (payload: OrderFormPayload) => {
    try {
      const result = await api().start_order(payload)
      if (result.ok) {
        void refreshOperation()
        return null
      }
      if (result.reason === 'busy' || result.code === 'operation_conflict') {
        toast.error(result.message ?? '已有操作正在进行，请先等待或查询操作状态')
        void refreshOperation()
        return {}
      }
      if (!result.fields || Object.keys(result.fields).length === 0) {
        toast.error(result.message ?? '校验未通过，请检查表单')
      }
      return result.fields ?? {}
    } catch {
      // bridge 已统一广播错误条；返回空对象避免表单卡在校验中。
      return {}
    }
  }, [refreshOperation])

  const startSss = useCallback(async (payload: SssFormPayload) => {
    try {
      const result = await api().start_sss(payload)
      if (result.ok) {
        void refreshOperation()
        return null
      }
      if (result.reason === 'busy' || result.code === 'operation_conflict') {
        toast.error(result.message ?? '已有操作正在进行，请先等待或查询操作状态')
        void refreshOperation()
        return {}
      }
      if (!result.fields || Object.keys(result.fields).length === 0) {
        toast.error(result.message ?? '校验未通过，请检查表单')
      }
      return result.fields ?? {}
    } catch {
      return {}
    }
  }, [refreshOperation])

  const stopTask = useCallback(async () => {
    if (!isWebTransport()) {
      toast.error('尚未连接服务端，无法停止任务')
      return
    }
    try {
      const result = await api().stop_task()
      if (!result.ok) {
        toast.warning('服务端当前没有可停止的任务，已重新查询运行状态')
      }
      await refreshOperation()
    } catch {
      // 停止请求失败不代表任务失败；保留状态等待权威恢复。
      setConnection('disconnected')
      toast.warning('停止请求未确认，页面会继续查询权威任务状态')
    }
  }, [refreshOperation])

  const chooseExcel = useCallback(async (mode: 'order' | 'sss') => {
    try {
      return await api().choose_excel(mode)
    } catch (error) {
      return { path: '', error: classifyRequestError(error).detail }
    }
  }, [])

  const newTemplate = useCallback(async (mode: 'order' | 'sss') => {
    try {
      return await api().new_template(mode)
    } catch (error) {
      return { path: '', error: classifyRequestError(error).detail }
    }
  }, [])

  const clearPassword = useCallback(async (mode: 'order' | 'sss'): Promise<ClearPasswordResult> => {
    const failure = (state: string, reason: string, nextAction: string): ClearPasswordResult => ({
      ok: false, status: 'error', state, mode, deleted: false,
      reason, next_action: nextAction, summary: {},
    })
    if (!isWebTransport()) {
      return failure('offline', '尚未连接服务端，无法清除密码', '连接恢复后重试；当前草稿保持不变')
    }
    if (passwordClearPending.current.has(mode)) {
      return failure('in_progress', '正在清除密码，请勿重复点击', '等待当前清除结果')
    }
    passwordClearPending.current.add(mode)
    try {
      const result = await api().clear_password(mode)
      const decision = evaluatePasswordClearResult(result)
      if (!decision.clearDraft) {
        return {
          ok: false,
          status: decision.status,
          state: decision.state,
          mode,
          deleted: false,
          reason: decision.reason,
          next_action: decision.nextAction,
          summary: result?.summary || {},
        }
      }
      // 只有确认已删除/原本不存在才同步清空表单草稿和内存密码。
      setPasswords((prev) => ({ ...prev, [mode]: '' }))
      setPasswordReset((prev) => ({ mode, nonce: prev.nonce + 1 }))
      toast.success(mode === 'sss' ? '已确认清除本机保存的闪时送密码' : '已确认清除本机保存的密码')
      return result
    } catch (error) {
      const decision = evaluatePasswordClearResult(null, classifyRequestError(error).detail)
      return {
        ok: false, status: decision.status, state: decision.state, mode,
        deleted: false, reason: decision.reason, next_action: decision.nextAction, summary: {},
      }
    } finally {
      passwordClearPending.current.delete(mode)
    }
  }, [])

  const checkUpdates = useCallback((manual: boolean) => {
    api().check_updates(manual).catch((error) => {
      toast.error(`检查更新失败：${classifyRequestError(error).detail}`)
    })
  }, [])

  const installUpdate = useCallback(() => {
    setUpdateError('')
    setUpdatePermissionRequired(false)
    setUpdateProgress({ phase: 'downloading', percent: 0, message: '正在准备下载…' })
    api().install_update()
      .then((result) => {
        if (result.ok) return
        if (result.reason === 'permission_required') {
          setUpdateProgress(null)
          setUpdatePermissionRequired(true)
          return
        }
        setUpdateProgress(null)
        setUpdateError(result.message || '无法开始更新')
      })
      .catch((error: unknown) => {
        setUpdateProgress(null)
        setUpdateError(classifyRequestError(error).detail)
      })
  }, [])

  const cancelUpdate = useCallback(() => {
    setUpdateProgress(null)
    api().cancel_update().catch((error) => {
      toast.warning(`取消更新未确认：${classifyRequestError(error).detail}`)
    })
  }, [])

  const openInstallSettings = useCallback(() => {
    setUpdatePermissionRequired(false)
    setUpdateError('')
    api().open_install_settings()
      .then((result) => {
        if (!result.ok) setUpdateError(result.message || '无法打开安装权限设置')
      })
      .catch((error: unknown) => {
        setUpdateError(classifyRequestError(error).detail)
      })
  }, [])

  const openExternal = useCallback((url: string) => {
    api().open_external(url).catch((error) => {
      toast.error(`无法打开链接：${classifyRequestError(error).detail}`)
    })
  }, [])

  const requestClose = useCallback(() => {
    api().request_close().catch((error) => {
      toast.warning(`关闭请求未确认：${classifyRequestError(error).detail}`)
    })
  }, [])

  const setSplitRatio = useCallback((ratio: number) => {
    if (isApiReady()) api().set_split_ratio(ratio).catch((error) => {
      console.warn('保存分栏比例失败', error)
    })
  }, [])

  const clearLogs = useCallback(() => {
    const keep = logs.filter((row) => row.level === 'WARN' || row.level === 'ERROR'
      || /审计|audit/i.test(row.msg))
    const removed = logs.length - keep.length
    setLogs(keep)
    if (removed > 0) toast.info(`已清理 ${removed} 条常规日志；警告与审计日志已保留`)
  }, [logs])

  const resolveDecision = useCallback(async (id: string, choice: string): Promise<InteractionResolveResult> => {
    const key = `decision:${id}`
    if (!isWebTransport()) return { ok: false, retryable: true, message: '尚未连接服务端，输入已保留，可恢复后重试' }
    if (!beginInteractionSubmit(interactionPending.current, key)) {
      return { ok: false, retryable: true, message: '正在提交，请勿重复点击' }
    }
    try {
      const result = await api().resolve_decision(id, choice)
      const outcome = submissionOutcome({ ok: Boolean(result?.ok) })
      if (outcome.clearRequest) {
        setDecision((prev) => (prev?.id === id ? null : prev))
        clearPendingRecovery()
      }
      if (outcome.tone === 'ended') toast.warning(outcome.message)
      return { ok: outcome.tone === 'resolved', retryable: outcome.keepInput, message: outcome.message }
    } catch (error) {
      const outcome = submissionOutcome({ ok: false, thrown: true, message: classifyRequestError(error).detail })
      return { ok: false, retryable: outcome.keepInput, message: outcome.message }
    } finally {
      endInteractionSubmit(interactionPending.current, key)
    }
  }, [clearPendingRecovery])

  const resolveCaptcha = useCallback(async (id: string, code: string): Promise<InteractionResolveResult> => {
    const key = `captcha:${id}`
    if (!isWebTransport()) return { ok: false, retryable: true, message: '尚未连接服务端，验证码已保留，可恢复后重试' }
    if (!beginInteractionSubmit(interactionPending.current, key)) {
      return { ok: false, retryable: true, message: '正在提交，请勿重复点击' }
    }
    try {
      const result = await api().resolve_captcha(id, code)
      const outcome = submissionOutcome({ ok: Boolean(result?.ok) })
      if (outcome.clearRequest) {
        setCaptcha((prev) => (prev?.id === id ? null : prev))
        clearPendingRecovery()
      }
      if (outcome.tone === 'ended') toast.warning(outcome.message)
      return { ok: outcome.tone === 'resolved', retryable: outcome.keepInput, message: outcome.message }
    } catch (error) {
      const outcome = submissionOutcome({ ok: false, thrown: true, message: classifyRequestError(error).detail })
      return { ok: false, retryable: outcome.keepInput, message: outcome.message }
    } finally {
      endInteractionSubmit(interactionPending.current, key)
    }
  }, [clearPendingRecovery])

  const resolveAddressInput = useCallback(async (
    id: string,
    entries: Record<string, string>,
  ): Promise<InteractionResolveResult> => {
    const key = `address:${id}`
    if (!isWebTransport()) return { ok: false, retryable: true, message: '尚未连接服务端，地址输入已保留，可恢复后重试' }
    if (!beginInteractionSubmit(interactionPending.current, key)) {
      return { ok: false, retryable: true, message: '正在提交，请勿重复点击' }
    }
    try {
      const result = await api().resolve_address_input(id, entries)
      const outcome = submissionOutcome({ ok: Boolean(result?.ok) })
      if (outcome.clearRequest) {
        setAddressInput((prev) => (prev?.id === id ? null : prev))
        clearPendingRecovery()
      }
      if (outcome.tone === 'ended') toast.warning(outcome.message)
      return { ok: outcome.tone === 'resolved', retryable: outcome.keepInput, message: outcome.message }
    } catch (error) {
      const outcome = submissionOutcome({ ok: false, thrown: true, message: classifyRequestError(error).detail })
      return { ok: false, retryable: outcome.keepInput, message: outcome.message }
    } finally {
      endInteractionSubmit(interactionPending.current, key)
    }
  }, [clearPendingRecovery])

  const dismissRequestIssue = useCallback((id: number) => {
    setRequestIssues((prev) => prev.filter((item) => item.id !== id))
  }, [])

  const operationView = useMemo(() => {
    const base = operation
      ? operationViewFromAuthority(operation, connection, recovery)
      : operationViewFromStatus(status, false, connection)
    if (!operation && lastTaskMessage && (
      status === 'success' || status === 'noop' || status === 'error'
      || status === 'stopped' || status === 'partial' || status === 'dry_run'
      || status === 'preflight_ok' || status === 'no_orders'
      || status === 'insufficient_balance' || status === 'balance_unknown'
      || status === 'uncertain' || status === 'blocked_uncertain'
      || status === 'recovered' || status === 'not_started' || status === 'rejected'
    )) {
      return { ...base, detail: lastTaskMessage }
    }
    return base
  }, [connection, lastTaskMessage, operation, recovery, status])

  const operationActive = useMemo(() => {
    if (operation) return Boolean(operation.active) || status === 'updating'
    return status === 'running' || status === 'stopping' || status === 'updating'
  }, [operation, status])

  const workerAlive = useMemo(() => {
    if (operation?.active) return operation.mode === 'order' || operation.mode === 'sss'
    return status === 'running' || status === 'stopping'
  }, [operation, status])

  const value = useMemo<AppStateBundle>(
    () => ({
      ready,
      mocked,
      transport,
      authError,
      hasValidToken,
      version,
      status,
      isAdmin,
      platform,
      canSelfUpdate,
      config,
      passwords,
      passwordReset,
      logs,
      decision,
      captcha,
      addressInput,
      connection,
      recovery,
      operation,
      operations,
      operationView,
      operationActive,
      workerAlive,
      mode,
      setMode,
      startOrder,
      startSss,
      stopTask,
      chooseExcel,
      newTemplate,
      clearPassword,
      checkUpdates,
      updateProgress,
      updatePermissionRequired,
      updateError,
      installUpdate,
      cancelUpdate,
      openInstallSettings,
      openExternal,
      requestClose,
      setSplitRatio,
      clearLogs,
      resolveDecision,
      resolveCaptcha,
      resolveAddressInput,
      requestIssues,
      dismissRequestIssue,
      pendingRecoveryMeta,
      dismissPendingRecovery,
      redactedPendingCount,
      reconnect,
    }),
    [ready, mocked, transport, authError, hasValidToken, version, status, isAdmin, platform,
      canSelfUpdate, config, passwords, passwordReset, logs, decision, captcha, addressInput,
      connection, recovery, operation, operations, operationView, operationActive, workerAlive,
      mode, startOrder, startSss, stopTask, chooseExcel, newTemplate, clearPassword, checkUpdates,
      updateProgress, updatePermissionRequired, updateError, installUpdate, cancelUpdate,
      openInstallSettings, openExternal, requestClose, setSplitRatio, clearLogs,
      resolveDecision, resolveCaptcha, resolveAddressInput, requestIssues, dismissRequestIssue,
      pendingRecoveryMeta, dismissPendingRecovery, redactedPendingCount, reconnect],
  )

  const updateAvailableValue = useMemo(
    () => ({
      available: updateAvailable,
      setAvailable: setAvailableState,
    }),
    [updateAvailable],
  )

  return (
    <AppContext.Provider value={value}>
      <UpdateAvailableContext.Provider value={updateAvailableValue}>
        {children}
      </UpdateAvailableContext.Provider>
    </AppContext.Provider>
  )
}
