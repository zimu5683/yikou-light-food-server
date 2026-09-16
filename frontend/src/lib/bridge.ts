/**
 * 桥接客户端：与 app/bridge.py 的 js_api / 事件协议一一对应。
 *
 * Python→JS：window.__bridge.dispatch({event, payload})，由本模块分发。
 * JS→Python：window.pywebview.api.<method>()，返回 Promise。
 *
 * 网页版（手机/服务器模式，见 app/web_server.py）：没有 pywebview，改用
 * ``POST /api/<method>``（JSON 数组按位置传参）调用同一批 Python 方法；访问令牌
 * 从网址的 ``?token=`` 读取并持久化，之后随 ``X-Yikou-Token`` 头发送。
 */

// ---------- 协议类型 ----------

export type LogLevel = 'INFO' | 'OK' | 'WARN' | 'ERROR'

export interface LogEntry {
  ts: string
  level: LogLevel
  msg: string
}

export type StatusState =
  | 'ready'
  | 'running'
  | 'stopping'
  | 'success'
  | 'partial'
  | 'stopped'
  | 'error'
  | 'updating'

export interface AppConfigState {
  target_url: string
  phone_number: string
  excel_path: string
  order_date: string
  order_count: number | null
  split_ratio: number
  sss_url: string
  sss_account: string
  sss_excel_path: string
  /** 名单来源：wps = 下单前从 WPS 云端读当天标 1 的人；excel = 读《闪时送.xlsx》。 */
  sss_order_source: 'wps' | 'excel'
  sss_product_name: string
  sss_common_address: string
  sss_use_fixed_address: boolean
  sss_fixed_lnt: number
  sss_fixed_lat: number
  sss_fixed_area_code: string
  sss_fixed_address_detail: string
  sss_dry_run: boolean
  sss_preflight: boolean
  /** 平台支持客户端幂等字段时由配置指定，默认空 = 至少一次提交+对账确认。 */
  sss_idempotency_field?: string
  api_mode: boolean
  /** WPS 云文档同步：把本地排单表增量写入云端排单表。 */
  wps_enabled: boolean
  wps_test_mode: boolean
  wps_test_file_id: string
  wps_test_drive_id: string
  wps_drive_id: string
  wps_cli_path: string
  wps_tables: Record<string, { file_id: string; drive_id?: string }>
  wps_test_tables: Record<string, string>
  wps_target_hour_start: number
  wps_target_hour_end: number
  wps_marker_enabled: boolean
}

/** 云文档同步状态（bridge.wps_status 返回）。 */
export interface WpsTableState {
  sheet: string
  file_id: string
  effective_file_id: string
  last_sync: string
  last_people: number
}

export interface WpsStatus {
  ok: boolean
  reason?: string
  enabled: boolean
  test_mode: boolean
  cli_path: string
  cli_found: boolean
  authenticated: boolean
  target_date: string
  weekday_number: number
  excel_path: string
  marker_enabled: boolean
  /** 云表按地址顺序重排：总开关。 */
  sort_enabled: boolean
  /** 每张子表的地址顺序清单；空数组 = 该表按地址升序。 */
  address_order: Record<string, string[]>
  /** 出厂默认顺序（界面「恢复默认」用，避免前后端各写一份）。 */
  address_order_defaults: Record<string, string[]>
  test_file_id?: string
  /** 测试模式下每张正式表对应的测试副本。 */
  test_tables?: Record<string, string>
  /** 当前写入目标是否全部是测试副本（不与正式表重合）。 */
  writing_test_copies?: boolean
  /** 正式排单表 ID 备份（暂停使用，可用于切回）。 */
  production_tables?: Record<string, string>
  state_path?: string
  tables: WpsTableState[]
}

export interface WpsPlanSummary {
  to_update: number
  to_append: number
  unchanged: number
  warned: number
}

export interface WpsCopyCheckItem {
  sheet: string
  file_id: string
  production_id: string
  status: 'aligned' | 'drifted' | 'same_as_production' | 'unreadable' | 'production_unreadable'
  rows?: number
  production_rows?: number
  missing?: string[]
  extra?: string[]
  reason?: string
}

export interface WpsCopyCheck {
  ok: boolean
  reason?: string
  drifted?: string[]
  all_aligned?: boolean
  tables?: WpsCopyCheckItem[]
}

export interface WpsResult {
  ok: boolean
  reason?: string
  target_date?: string
  text?: string
  summary?: WpsPlanSummary
  test_mode?: boolean
  result?: { written: number; failed: number; sheets: Array<{ sheet: string; status: string; reason?: string }> }
}

/** 云端当天名单（bridge.sss_day_orders 返回）。 */
export interface SssDayOrders {
  ok: boolean
  reason?: string
  target_date?: string
  date_text?: string
  total?: number
  archive_error?: string
  meals?: Record<
    string,
    {
      table: string
      marked: number
      skipped_address: number
      orders: number
      date_text: string
      skipped: boolean
      reason: string
      warnings: string[]
    }
  >
}

export interface AppState {
  version: string
  status: StatusState
  frozen: boolean
  /** Python 进程标识：用于识别重启后 sequence 归零，避免复用旧 cursor。 */
  event_producer_id?: string
  config: AppConfigState
  passwords: { order: string; sss: string }
}

export type DecisionKind = 'order_retry' | 'sss_retry' | 'save_retry' | 'close_confirm'

export interface DecisionChoice {
  value: string
  label: string
  style: 'primary' | 'neutral' | 'danger'
}

export interface DecisionRequest {
  id: string
  kind: DecisionKind
  title: string
  message: string
  choices: DecisionChoice[]
}

export interface CaptchaRequest {
  id: string
  image: string
}

export interface PendingAddressItem {
  raw_address: string
  order_numbers: string[]
  campus: string
  confidence: string
  reason: string
  suggested_point: string
  candidates?: Record<string, number>
}

export interface AddressInputRequest {
  id: string
  title: string
  message: string
  items: PendingAddressItem[]
}

export interface UpdateAvailable {
  tag: string
  current: string
  body: string
  can_auto_install: boolean
}

type BridgeEventBase =
  | { event: 'log'; payload: LogEntry }
  | { event: 'status'; payload: { state: StatusState } }
  | {
      event: 'task:done'
      payload: {
        message: string
        stopped: boolean
        partial: boolean
        result: Record<string, number | boolean | null>
      }
    }
  | { event: 'task:error'; payload: { message: string } }
  | { event: 'update:available'; payload: UpdateAvailable }
  | { event: 'update:latest'; payload: { manual: boolean; current: string } }
  | { event: 'update:error'; payload: { message: string } }
  | { event: 'update:progress'; payload: { downloaded: number; total: number | null } }
  | { event: 'update:stage'; payload: { stage: string } }
  | { event: 'update:install_error'; payload: { message: string } }
  | { event: 'update:installed'; payload: { message: string } }
  | { event: 'decision'; payload: DecisionRequest }
  | { event: 'captcha'; payload: CaptchaRequest }
  | { event: 'address_input'; payload: AddressInputRequest }
  | {
      event: 'events:dropped'
      payload: {
        dropped_count: number
        critical_dropped_count?: number
        first_sequence?: number
        last_sequence?: number
        total_dropped?: number
        total_critical_dropped?: number
        message: string
      }
    }

export interface BridgeEventMeta {
  event_id: string
  sequence: number
  created_at: number
  droppable: boolean
  /** Python 端合成的“事件被丢弃”告警，不对应真实 sequence。 */
  synthetic?: boolean
}

export type BridgeEvent = BridgeEventBase & BridgeEventMeta

export interface DrainEventsResult {
  events: BridgeEvent[]
  producer_id: string
  latest_sequence: number
  acked_sequence: number
  dropped_count: number
  critical_dropped_count?: number
  first_available_sequence: number
}

// ---------- js_api 载荷 ----------

export interface OrderFormPayload {
  url: string
  phone: string
  password: string
  excel: string
  date: string
  count: string
  remember: boolean
  api_mode: boolean
}

/** 订单表单防抖即时保存的载荷（不触发任务、不带密码）。 */
export interface OrderConfigPayload {
  url?: string
  phone?: string
  excel?: string
  date?: string
  count?: number | null
  api_mode?: boolean
}

/** 闪时送表单防抖即时保存的载荷（不触发任务、不带密码）。 */
export interface SssConfigPayload {
  url?: string
  account?: string
  excel?: string
  order_source?: 'wps' | 'excel'
  product_name?: string
  common_address?: string
  use_fixed_address?: boolean
  fixed_lnt?: string | number
  fixed_lat?: string | number
  fixed_area_code?: string
  fixed_address_detail?: string
  dry_run?: boolean
  preflight?: boolean
  api_mode?: boolean
}

export interface SssFormPayload {
  url: string
  account: string
  password: string
  excel: string
  order_source: 'wps' | 'excel'
  product_name: string
  common_address: string
  use_fixed_address: boolean
  fixed_lnt: string
  fixed_lat: string
  fixed_area_code: string
  fixed_address_detail: string
  remember: boolean
  dry_run: boolean
  preflight: boolean
  api_mode: boolean
}

export interface FieldErrors {
  ok: boolean
  reason?: string
  message?: string
  fields?: Record<string, { message: string }>
}

/** 云文档同步配置（只包含这个页签会改的字段）。 */
export interface WpsConfigPayload {
  enabled: boolean
  test_mode: boolean
  cli_path: string
  drive_id: string
  test_file_id: string
  test_drive_id: string
  marker_enabled: boolean
  /** 排序总开关；不带该字段时后端保持原值。 */
  sort_enabled?: boolean
  /** 每张子表的地址顺序（传数组）；空数组 = 该表按地址升序。 */
  address_order?: Record<string, string[]>
  tables: Record<string, { file_id: string; drive_id?: string }>
  test_tables: Record<string, string>
}

// ---------- window 声明 ----------

interface PywebviewApi {
  bridge_ready(): Promise<AppState>
  start_order(payload: OrderFormPayload): Promise<FieldErrors>
  start_sss(payload: SssFormPayload): Promise<FieldErrors>
  sss_day_orders(): Promise<SssDayOrders>
  stop_task(): Promise<{ ok: boolean }>
  worker_alive(): Promise<boolean>
  resolve_decision(id: string, choice: string): Promise<{ ok: boolean }>
  resolve_captcha(id: string, code: string): Promise<{ ok: boolean }>
  resolve_address_input(id: string, entries: Record<string, string>): Promise<{ ok: boolean }>
  choose_excel(mode: 'order' | 'sss', path?: string): Promise<{ path: string; error: string }>
  new_template(mode: 'order' | 'sss', path?: string): Promise<{ path: string; error: string }>
  wps_status(): Promise<WpsStatus>
  wps_preview(): Promise<WpsResult>
  wps_upload(): Promise<WpsResult>
  wps_authorize(): Promise<{ ok: boolean; reason?: string; hint?: string }>
  wps_check_copies(): Promise<WpsCopyCheck>
  save_wps_config(payload: WpsConfigPayload): Promise<{ ok: boolean; reason?: string }>
  clear_password(mode: 'order' | 'sss'): Promise<{ ok: boolean }>
  check_updates(manual: boolean): Promise<{ ok: boolean; reason?: string }>
  install_update(): Promise<{ ok: boolean; reason?: string }>
  open_external(url: string): Promise<{ ok: boolean }>
  frontend_report(payload: Record<string, unknown> | string): Promise<{ ok: boolean }>
  drain_events(lastSequence?: number, ackSequence?: number, producerId?: string): Promise<DrainEventsResult>
  echo_test(message: string, payload?: Record<string, unknown>): Promise<{ echo: string; payload_keys: string[] | null }>
  window_action(action: 'minimize' | 'toggle_maximize' | 'close'): Promise<{ action?: string }>
  request_close(): Promise<{ action: string }>
  set_split_ratio(ratio: number): Promise<{ ok: boolean; ratio: number }>
  save_order_config(payload: OrderConfigPayload): Promise<{ ok: boolean; reason?: string; saved?: { order_date: string; order_count: number | null } }>
  save_sss_config(payload: SssConfigPayload): Promise<{ ok: boolean; reason?: string }>
}

declare global {
  interface Window {
    pywebview?: { api: PywebviewApi }
    __bridge: { dispatch(message: BridgeEvent): void }
  }
}

// ---------- 传输方式 ----------

/**
 * `pywebview`：桌面端原生窗口（原有行为）；
 * `http`：网页版，浏览器 ↔ app/web_server.py；
 * `mock`：纯浏览器直开（无 Python），只给样式开发用的静态假数据。
 */
export type Transport = 'pywebview' | 'http' | 'mock'

let transport: Transport = 'mock'

export function currentTransport(): Transport {
  return transport
}

/** 网页版（走 HTTP 服务端）时为 true。 */
export function isWebTransport(): boolean {
  return transport === 'http'
}

// ---------- 网页版：访问令牌 ----------

const TOKEN_STORAGE_KEY = 'yikou.web.token.v1'

function readToken(): string {
  // 优先用网址里带的令牌（首次点开链接），否则用之前存下的，保证刷新后仍可用。
  try {
    const fromUrl = new URLSearchParams(window.location.search).get('token')
    if (fromUrl) {
      window.localStorage.setItem(TOKEN_STORAGE_KEY, fromUrl)
      return fromUrl
    }
    return window.localStorage.getItem(TOKEN_STORAGE_KEY) ?? ''
  } catch {
    // 无 location / localStorage（Node 测试、隐私模式）时退化为无令牌。
    return ''
  }
}

let authToken = readToken()

export function hasAuthToken(): boolean {
  return Boolean(authToken)
}

/** 允许调用方在运行时补一个令牌（例如从设置里粘贴）。 */
export function setAuthToken(token: string): void {
  authToken = token
  try {
    window.localStorage.setItem(TOKEN_STORAGE_KEY, token)
  } catch {
    // 忽略存储失败；本次会话内仍然生效。
  }
}

function authHeaders(): Record<string, string> {
  const headers: Record<string, string> = { 'Content-Type': 'application/json' }
  if (authToken) headers['X-Yikou-Token'] = authToken
  return headers
}

async function postJson(url: string, body: unknown): Promise<{ ok: boolean; status: number; data: unknown }> {
  const response = await fetch(url, {
    method: 'POST',
    headers: authHeaders(),
    body: JSON.stringify(body),
  })
  const text = await response.text()
  let data: unknown = null
  if (text) {
    try {
      data = JSON.parse(text)
    } catch {
      data = { error: text }
    }
  }
  return { ok: response.ok, status: response.status, data }
}

function errorMessage(data: unknown, status: number): string {
  if (data && typeof data === 'object' && 'error' in data) {
    const message = (data as { error?: unknown }).error
    if (typeof message === 'string' && message) return message
  }
  return `请求失败（HTTP ${status}）`
}

/** 用 HTTP 实现整个 PywebviewApi：方法名即路径，参数按位置传。 */
function createHttpApi(): PywebviewApi {
  const call = async (method: string, args: unknown[]): Promise<unknown> => {
    const { ok, status, data } = await postJson(`/api/${method}`, args)
    if (!ok) {
      if (status === 401) throw new Error('访问令牌无效或已失效，请用带 ?token= 的完整网址重新打开')
      throw new Error(errorMessage(data, status))
    }
    return data
  }
  return new Proxy({} as PywebviewApi, {
    get(_target, prop) {
      if (typeof prop !== 'string') return undefined
      return (...args: unknown[]) => call(prop, args)
    },
  })
}

// ---------- 网页版：服务器端文件浏览器 ----------
//
// 客户端选中的必须是**运行任务那台机器**上的路径（Excel 在手机上，不在打开
// 网页的设备上），所以文件列表由服务端提供，而不是用 <input type="file">。

export interface FsEntry {
  name: string
  path: string
  is_dir: boolean
  size: number
}

export interface FsShortcut {
  name: string
  path: string
}

export interface FsListResult {
  path: string
  parent?: string
  entries: FsEntry[]
  shortcuts?: FsShortcut[]
  error: string
}

/** 列出服务端目录（仅返回目录与 Excel 文件）。 */
export async function listServerDir(path = ''): Promise<FsListResult> {
  const query = path ? `?path=${encodeURIComponent(path)}` : ''
  const response = await fetch(`/api/fs/list${query}`, { headers: authHeaders() })
  const text = await response.text()
  let data: FsListResult | null = null
  try {
    data = text ? (JSON.parse(text) as FsListResult) : null
  } catch {
    data = null
  }
  if (!response.ok || !data) {
    if (response.status === 401) {
      throw new Error('访问令牌无效或已失效，请用带 ?token= 的完整网址重新打开')
    }
    throw new Error(data ? data.error : `读取目录失败（HTTP ${response.status}）`)
  }
  return data
}

// ---------- 客户端实现 ----------

type Listener = (event: BridgeEvent) => void

const listeners = new Set<Listener>()
const queued: BridgeEvent[] = []

/** Python 端就绪前的事件先入队，握手后统一回放。 */
let apiReady = false

// ---------- cursor / 重放 / 去重 ----------
//
// Python 保留最近事件并按 sequence 返回；前端只在事件成功 dispatch 后推进
// cursor，并持久化到 localStorage。页面刷新/短暂断开后可从断点重放；同一
// event_id 不会重复应用。Python 进程重启会换 producer_id，此时 cursor 归零。
const CURSOR_STORAGE_KEY = 'yikou.bridge.cursor.v1'
const DEDUPE_LIMIT = 5000

interface StoredCursor {
  producerId: string
  sequence: number
  ackSequence: number
}

let eventProducerId = ''
let eventCursor = 0
let eventAckCursor = 0
let droppedCountNotified = 0
const seenEventIds = new Set<string>()
const seenEventOrder: string[] = []

function readStoredCursor(): void {
  try {
    const raw = window.localStorage.getItem(CURSOR_STORAGE_KEY)
    if (!raw) return
    const parsed = JSON.parse(raw) as Partial<StoredCursor>
    if (typeof parsed.producerId === 'string') eventProducerId = parsed.producerId
    if (typeof parsed.sequence === 'number') eventCursor = Math.max(0, parsed.sequence)
    if (typeof parsed.ackSequence === 'number') eventAckCursor = Math.max(0, parsed.ackSequence)
  } catch {
    // localStorage 不可用（隐私模式/文件协议）时退化为本次页面内 cursor。
  }
}

function persistCursor(): void {
  try {
    const value: StoredCursor = {
      producerId: eventProducerId,
      sequence: eventCursor,
      ackSequence: eventAckCursor,
    }
    window.localStorage.setItem(CURSOR_STORAGE_KEY, JSON.stringify(value))
  } catch {
    // 忽略存储失败；下一次轮询仍按内存 cursor 继续。
  }
}

function adoptProducer(producerId?: string): void {
  if (!producerId || producerId === eventProducerId) return
  // 新的 Python 进程：旧 sequence 无意义，必须从头消费保留窗口。
  eventProducerId = producerId
  eventCursor = 0
  eventAckCursor = 0
  droppedCountNotified = 0
  seenEventIds.clear()
  seenEventOrder.length = 0
  persistCursor()
}

function rememberEventId(eventId: string): boolean {
  if (seenEventIds.has(eventId)) return false
  seenEventIds.add(eventId)
  seenEventOrder.push(eventId)
  if (seenEventOrder.length > DEDUPE_LIMIT) {
    const oldest = seenEventOrder.shift()
    if (oldest) seenEventIds.delete(oldest)
  }
  return true
}

readStoredCursor()

/** 拉取并应用一批桥接事件；由 useApp 的定时轮询调用。 */
export async function pullBridgeEvents(): Promise<void> {
  if (!isApiReady()) return
  const result = await api().drain_events(eventCursor, eventAckCursor, eventProducerId)
  if (!result) return
  adoptProducer(result.producer_id)

  for (const event of result.events) {
    if (event.sequence <= eventCursor && !event.synthetic) continue
    if (seenEventIds.has(event.event_id)) {
      // 之前已成功应用但 cursor 尚未推进（例如持久化前页面抖动）：补推进即可。
      if (event.sequence > eventCursor) eventCursor = event.sequence
      continue
    }
    try {
      dispatch(event)
    } catch (error) {
      // 监听器异常时绝不能推进 cursor：保留该事件，下一次轮询重放。
      console.error('bridge event listener failed; event will be replayed', error)
      break
    }
    rememberEventId(event.event_id)
    if (event.sequence > eventCursor) eventCursor = event.sequence
  }

  if (result.acked_sequence > eventAckCursor) eventAckCursor = result.acked_sequence
  eventAckCursor = Math.max(eventAckCursor, eventCursor)
  if (result.dropped_count > droppedCountNotified) {
    droppedCountNotified = result.dropped_count
  }
  persistCursor()
}

export function bridgeCursor(): StoredCursor {
  return { producerId: eventProducerId, sequence: eventCursor, ackSequence: eventAckCursor }
}

function dispatch(message: BridgeEvent): void {
  if (!apiReady) {
    queued.push(message)
    return
  }
  for (const listener of listeners) listener(message)
}

window.__bridge = { dispatch }

export function onBridgeEvent(listener: Listener): () => void {
  listeners.add(listener)
  return () => listeners.delete(listener)
}

let httpApi: PywebviewApi | null = null

export function api(): PywebviewApi {
  const raw = window.pywebview?.api
  if (raw) {
    return new Proxy(raw, {
      get(target, prop) {
        const value = Reflect.get(target, prop)
        if (typeof value !== 'function') return value
        return (...args: unknown[]) => {
          const promise = (value as (...a: unknown[]) => Promise<unknown>).apply(target, args)
          // evaluate_js 结果投递在 WebKitGTK 上可能被吞，超时兜底避免 UI 永久悬挂。
          // 文件对话框等会合法长阻塞的调用不设超时。
          const timeoutMs = prop === 'drain_events' ? 4000 : 0
          if (!timeoutMs) return promise
          return new Promise((resolve, reject) => {
            const timer = setTimeout(() => reject(new Error('bridge timeout')), timeoutMs)
            promise.then(
              (value) => {
                clearTimeout(timer)
                resolve(value)
              },
              (error) => {
                clearTimeout(timer)
                reject(error)
              },
            )
          })
        }
      },
    }) as PywebviewApi
  }
  if (transport === 'http') {
    httpApi ??= createHttpApi()
    return httpApi
  }
  throw new Error('桥接 API 尚未就绪')
}

export function isApiReady(): boolean {
  return Boolean(window.pywebview?.api) || transport === 'http'
}

export interface ReadyResult {
  state: AppState
  /** 模拟浏览器开发环境（无 Python 壳）时为 true。 */
  mocked: boolean
  /** 实际使用的传输方式，便于界面区分「网页版」与「mock 预览」。 */
  transport: Transport
  /** 网页版令牌无效时的提示（非空表示需要用户换用带令牌的网址）。 */
  authError: string
}

/** 网页版握手：成功返回初始状态，令牌不对返回提示，没有服务端返回 null。 */
async function tryHttpHandshake(): Promise<{ state: AppState | null; authError: string }> {
  try {
    const { ok, status, data } = await postJson('/api/bridge_ready', [])
    if (status === 401) {
      return {
        state: null,
        authError: errorMessage(data, status),
      }
    }
    if (ok && data && typeof data === 'object' && 'event_producer_id' in data) {
      return { state: data as AppState, authError: '' }
    }
    return { state: null, authError: '' }
  } catch {
    // 网络错误 / 不是本服务（例如 Vite 开发服务器返回 HTML）：当作没有服务端。
    return { state: null, authError: '' }
  }
}

/**
 * 建立桥接：优先 pywebview，其次网页版 HTTP，最后降级 mock。
 *
 * 网页版探测放在前面且很快返回，避免浏览器直开时白等 pywebview 的 10 秒注入窗口；
 * 桌面端（file:// 或 Vite 开发服务器）探测一定失败，行为与以前一致。
 */
export async function connectBridge(): Promise<ReadyResult> {
  if (!window.pywebview?.api) {
    const { state, authError } = await tryHttpHandshake()
    if (state) {
      transport = 'http'
      adoptProducer(state.event_producer_id)
      apiReady = true
      for (const message of queued.splice(0)) dispatch(message)
      return { state, mocked: false, transport, authError: '' }
    }
    if (authError) {
      // 服务端在，但令牌不对：不能悄悄退化成 mock，否则用户会以为连上了。
      return { state: mockState(), mocked: true, transport: 'mock', authError }
    }
  }
  if (!window.pywebview) {
    await new Promise<void>((resolve) => {
      // pywebview 6 GTK 的注入可能晚于首帧；10s 内未注入才降级 mock。
      const timer = setTimeout(() => resolve(), 10000)
      window.addEventListener('pywebviewready', () => {
        clearTimeout(timer)
        resolve()
      })
    })
  }
  if (!window.pywebview?.api) {
    // 浏览器直开（无 Python 壳）：提供 mock 状态方便样式开发。
    transport = 'mock'
    apiReady = true
    return { state: mockState(), mocked: true, transport, authError: '' }
  }
  transport = 'pywebview'
  const state = await api().bridge_ready()
  adoptProducer(state.event_producer_id)
  apiReady = true
  for (const message of queued.splice(0)) dispatch(message)
  return { state, mocked: false, transport, authError: '' }
}

function mockState(): AppState {
  return {
    version: '3.0.0-dev',
    status: 'ready',
    frozen: false,
    config: {
      target_url: 'https://m.icall.me/admin/#/login',
      phone_number: '13968033834',
      excel_path: '/home/zimu/文档/排单.xlsx',
      order_date: '2026-09-05',
      order_count: null,
      split_ratio: 0.38,
      sss_url: 'https://sssplusnew.zhuopaikeji.com/takeout',
      sss_account: '18758187837',
      sss_excel_path: '/home/zimu/文档/闪时送.xlsx',
      sss_order_source: 'wps',
      sss_product_name: '轻食',
      sss_common_address: '嗯哼',
      sss_use_fixed_address: true,
      sss_fixed_lnt: 119.728224,
      sss_fixed_lat: 30.256632,
      sss_fixed_area_code: '330110',
      sss_fixed_address_detail: '浙江农林大学东湖校区',
      sss_dry_run: true,
      sss_preflight: false,
      sss_idempotency_field: '',
      api_mode: true,
      wps_enabled: false,
      wps_test_mode: true,
      wps_test_file_id: '',
      wps_test_drive_id: '',
      wps_drive_id: '',
      wps_cli_path: '',
      wps_tables: {},
      wps_test_tables: {},
      wps_target_hour_start: 20,
      wps_target_hour_end: 10,
      wps_marker_enabled: true,
    },
    passwords: { order: '', sss: '' },
  }
}
