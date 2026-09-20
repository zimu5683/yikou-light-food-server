/**
 * AppProvider 与消费者共享的 context/类型/selector。
 *
 * 单独成文件，让 useApp.tsx 只导出 React 组件，避免 Fast Refresh 因
 * “同一文件同时导出组件和 hook/常量”而失效。
 */
import { createContext, useContext } from 'react'
import {
  type AddressInputRequest,
  type AppState,
  type CaptchaRequest,
  type ClearPasswordResult,
  type DecisionRequest,
  type GlobalRequestIssue,
  type LogEntry,
  type OperationInfo,
  type OperationStatusResult,
  type OrderFormPayload,
  type SssFormPayload,
  type StatusState,
  type Transport,
  type UpdateAvailable,
  type UpdateProgress,
} from '@/lib/bridge'
import type { ConnectionState, OperationView, RecoveryState } from '@/lib/operationStatus'
import type { PendingInteractionMeta } from '@/lib/interactionRecovery'

export type { ClearPasswordResult }

export type TaskMode = 'order' | 'cloud' | 'sss'

export interface LogRow extends LogEntry {
  id: number
}

export type FieldErrors = Record<string, { message: string } | undefined>

export interface InteractionResolveResult {
  ok: boolean
  /** true = 前端本地/网络失败，输入保留可重试；false = 服务端权威已不再等待。 */
  retryable: boolean
  message?: string
}


export interface PasswordResetState {
  mode: 'order' | 'sss' | null
  nonce: number
}

export interface AppStateBundle {
  ready: boolean
  mocked: boolean
  /** 实际传输方式：网页版 HTTP / 无后端的 mock。 */
  transport: Transport
  /** 网页版令牌无效时的提示；非空时界面提示改用带令牌的完整网址。 */
  authError: string
  /** 用户是否拥有有效登录令牌；无 token 时上传/任务接口一律禁用。 */
  hasValidToken: boolean
  version: string
  status: StatusState
  /** 当前账号是否管理员；非管理员只看到基础功能（后端另有强制拦截）。 */
  isAdmin: boolean
  /** 运行平台：android = APK 自带 WebView；web = 纯浏览器访问。 */
  platform: 'android' | 'web'
  /** 是否支持应用内下载安装更新。 */
  canSelfUpdate: boolean
  config: AppState['config'] | null
  passwords: { order: string; sss: string }
  /** 密码清除成功后递增；表单按 mode 清空草稿，普通配置刷新不会覆盖正在输入的密码。 */
  passwordReset: PasswordResetState
  logs: LogRow[]
  decision: DecisionRequest | null
  captcha: CaptchaRequest | null
  addressInput: AddressInputRequest | null
  /** 本地与服务端的连接状态，不改变权威任务状态。 */
  connection: ConnectionState
  recovery: RecoveryState
  /** operation_status 的完整权威快照。 */
  operation: OperationStatusResult | null
  /** 最近操作列表（新→旧），用于断线恢复后的结果展示。 */
  operations: OperationInfo[]
  /** 权威 operation 的展示视图。 */
  operationView: OperationView
  /** 是否有任何服务端互斥操作在跑（任务/云上传/授权/更新）。 */
  operationActive: boolean
  /** 是否存在任务线程（用于日志自动展开等）。 */
  workerAlive: boolean
  mode: TaskMode
  setMode: (mode: TaskMode) => void
  startOrder: (payload: OrderFormPayload) => Promise<FieldErrors | null>
  startSss: (payload: SssFormPayload) => Promise<FieldErrors | null>
  stopTask: () => Promise<void>
  chooseExcel: (mode: 'order' | 'sss') => Promise<{ path: string; error: string }>
  newTemplate: (mode: 'order' | 'sss') => Promise<{ path: string; error: string }>
  clearPassword: (mode: 'order' | 'sss') => Promise<ClearPasswordResult>
  checkUpdates: (manual: boolean) => void
  /** APK 更新：下载/校验/安装进程中的状态。 */
  updateProgress: UpdateProgress | null
  /** 缺少「安装未知应用」权限时由弹窗引导到系统设置。 */
  updatePermissionRequired: boolean
  /** 最近一次更新相关错误的可展示文本。 */
  updateError: string
  installUpdate: () => void
  cancelUpdate: () => void
  openInstallSettings: () => void
  openExternal: (url: string) => void
  requestClose: () => void
  setSplitRatio: (ratio: number) => void
  clearLogs: () => void
  resolveDecision: (id: string, choice: string) => Promise<InteractionResolveResult>
  resolveCaptcha: (id: string, code: string) => Promise<InteractionResolveResult>
  resolveAddressInput: (id: string, entries: Record<string, string>) => Promise<InteractionResolveResult>
  /** 请求失败统一错误条。 */
  requestIssues: GlobalRequestIssue[]
  dismissRequestIssue: (id: number) => void
  /**
   * 页面刷新后仅保留的安全交互元数据（kind/id/operation_id）。
   * 后端当前没有 pending interaction 只读接口，因此显示“需要重新核对”而不是伪造恢复。
   */
  pendingRecoveryMeta: PendingInteractionMeta | null
  dismissPendingRecovery: () => void
  /** 管理员只读查询到「属于其他账号」的 pending 交互数量；只能看元数据，不能打开输入。 */
  redactedPendingCount: number
  /** 主动重新查询 bridge_ready/operation_status；失败时保持可核对状态。 */
  reconnect: () => Promise<boolean>
}

export const AppContext = createContext<AppStateBundle | null>(null)
export const UpdateAvailableContext = createContext<{
  available: UpdateAvailable | null
  setAvailable: (v: UpdateAvailable | null) => void
} | null>(null)

const STATUS_LABELS: Record<StatusState, string> = {
  ready: '就绪',
  running: '处理中',
  stopping: '正在停止',
  success: '处理完成',
  noop: '处理完成（无变化）',
  partial: '部分完成 · 待核对',
  stopped: '已停止',
  dry_run: '模拟完成（未下单）',
  preflight_ok: '预检完成（未下单）',
  no_orders: '无单可处理',
  insufficient_balance: '余额不足，本批未提交',
  balance_unknown: '余额未知，本批未提交',
  uncertain: '结果不确定 · 待核对',
  blocked_uncertain: '被阻断 · 待核对',
  blocked_concurrent: '另一个任务正在运行',
  recovered: '已恢复 · 待核对',
  not_started: '未开始',
  rejected: '已拒绝',
  error: '处理失败',
  updating: '处理中',
}

export function statusLabel(state: StatusState): string {
  return STATUS_LABELS[state] ?? state
}

export function useUpdateAvailable() {
  const ctx = useContext(UpdateAvailableContext)
  if (!ctx) throw new Error('useUpdateAvailable must be used within AppProvider')
  return ctx
}

export function useApp(): AppStateBundle {
  const ctx = useContext(AppContext)
  if (!ctx) throw new Error('useApp must be used within AppProvider')
  return ctx
}
