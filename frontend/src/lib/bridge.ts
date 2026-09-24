/**
 * 后端 API 客户端：与 app/api/bridge.py 的方法 / 事件协议一一对应。
 *
 * JS→Python：``POST /api/<method>``，JSON 数组按位置传参；
 * Python→JS：前端定时 ``drain_events`` 拉取事件，按 ``event_id`` 去重后分发。
 *
 * 访问令牌从网址的 ``?token=`` 读取并持久化，之后随 ``X-Yikou-Token`` 头发送；
 * 未配置后端的纯静态预览会退化为 mock 数据（仅用于样式开发）。
 */

import { RequestError, classifyRequestError, toRequestError, type RequestErrorView } from './requestError.ts'

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
  | 'noop'
  | 'partial'
  | 'stopped'
  | 'dry_run'
  | 'preflight_ok'
  | 'no_orders'
  | 'insufficient_balance'
  | 'balance_unknown'
  | 'uncertain'
  | 'blocked_uncertain'
  | 'blocked_concurrent'
  | 'recovered'
  | 'not_started'
  | 'rejected'
  | 'error'
  | 'updating'

export type OperationMode =
  | ''
  | 'order'
  | 'sss'
  | 'wps_upload'
  | 'wps_authorize'
  | 'wps_logout'
  | 'check_update'
  | 'install_update'
  | 'wps_preview'
  | 'wps_check_copies'
  | 'sss_day_orders'
  | 'wps_recovery_resolve'

export type OperationLifecycle =
  | 'idle'
  | 'running'
  | 'success'
  | 'noop'
  | 'partial'
  | 'stopped'
  | 'dry_run'
  | 'preflight_ok'
  | 'no_orders'
  | 'insufficient_balance'
  | 'balance_unknown'
  | 'uncertain'
  | 'blocked'
  | 'blocked_uncertain'
  | 'error'
  | 'failed'
  | 'rejected'
  | 'recovered'
  | 'not_started'
  | 'not_found'

/** operation_status() 返回的单条操作（活动或最近历史）。 */
export interface OperationInfo {
  ok: boolean
  active: boolean
  operation_id: string
  mode: OperationMode | string
  status: OperationLifecycle | string
  phase: string
  summary: Record<string, unknown>
  next_action: string
  reason: string
  started_at: number | null
  finished_at: number | null
}

/** operation_status() 完整返回；未指定 ID 时选择活动 > 最新完成 > 空闲。 */
export interface OperationStatusResult extends OperationInfo {
  operations: OperationInfo[]
}

export interface ClearPasswordResult {
  ok: boolean
  status: string
  state: string
  mode: 'order' | 'sss' | string
  deleted: boolean
  reason: string
  next_action: string
  summary: Record<string, unknown>
}

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
  /** 日期格已被协作者写了别的值（例如 0 = 当天不送），本次没动这一格的人数。 */
  skipped?: number
  /** 结构化表统计中的阻断表数；summary 兼容字段可能没有，界面优先用 tables 求和。 */
  blocked?: number | null
}

/**
 * W6：行数口径（**计划**）。
 *
 * `planned_summary.rows.to_update/to_append` 是「计划要改几行」，永远不能当作
 * 成功行数展示。后端 `Bridge._wps_execution_summary` 是唯一实现。
 */
export interface WpsPlanRows {
  to_update: number
  to_append: number
  unchanged: number
  skipped: number
  warned: number
  blocked?: number | null
}

export interface WpsPlannedSummary {
  kind: 'plan'
  contract_version?: number
  rows: WpsPlanRows
  [key: string]: unknown
}

/**
 * W6：行数口径（**执行**）。
 *
 * - `sheets.*` 是**表数**，`rows.*` 才是行数，两者绝不能混用；
 * - `rows.verified === null` 或 `rows_unknown === true` 表示「无法证明实际写入行数」，
 *   界面必须显示“未知/待核对”，不得显示 0 或计划数；
 * - `proven_no_write === true` 才允许说“本次未写入任何内容”。
 */
export interface WpsExecutionSheets {
  total: number
  verified: number
  noop: number
  failed: number
  uncertain: number
  skipped: number
  blocked: number
  other: number
}

export interface WpsExecutionRows {
  verified: number | null
  failed: number | null
  uncertain: number | null
  skipped: number | null
  planned: number | null
}

export interface WpsExecutionSummary {
  kind: 'execution'
  contract_version?: number
  status: string
  executed: boolean
  /** `apply_plan` | `rejected_before_write` | `unknown_after_exception`；只是计数来源，不是状态。 */
  counts_source: string
  sheets: WpsExecutionSheets
  rows: WpsExecutionRows
  rows_unknown: boolean
  proven_no_write: boolean
  written_sheets: number | null
  failed_sheets: number | null
  note?: string
  next_action?: string
  [key: string]: unknown
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

export interface WpsTableCounts {
  to_update: number
  to_append: number
  unchanged: number
  skipped: number
  warned: number
  blocked?: number | null
}

export interface WpsStructuredChange {
  kind: 'new' | 'existing' | string
  name: string
  phone: string
  row: number
  slot: number
  delta: number
  total_before: number
  total_after: number
  target_col: number
  target_ok: boolean
  target_blocked: boolean
  target_occupied: string
  needs_write: boolean
  local_rows: Array<number | string>
  address: string
  meal_type: string
  meal_kind: string
  detail: string
  [key: string]: unknown
}

export interface WpsSortInfo {
  enabled: boolean
  sort_range: string
  sort_key_col: number
  row_keys_count: number
  unknown_addresses: string[]
}

export interface WpsStructuredTable {
  sheet: string
  file_id: string
  drive_id?: string
  target_date: string
  target_col: number
  target_header: string
  weekday_number: number
  blocked_reason: string
  counts: WpsTableCounts
  changes: WpsStructuredChange[]
  insert_blocks: Array<Record<string, unknown>>
  warnings: string[]
  unknown_addresses: string[]
  sort?: WpsSortInfo
  [key: string]: unknown
}

export interface WpsBlockedItem {
  sheet: string
  reason: string
}

export interface WpsFingerprint {
  local_sha256: string
  context: string
  plan: string
}

/** wps_preview() 成功返回的结构化预览 + 一次性 preview_id。 */
export interface WpsPreviewResult {
  ok: boolean
  status: string
  reason: string
  code?: string
  next_action: string
  summary: WpsPlanSummary
  stats?: WpsPlanSummary
  operation_id: string
  preview_id: string
  created_at: string
  expires_at: string
  expires_in: number
  ttl_seconds: number
  local_sha256: string
  context_fingerprint: string
  plan_fingerprint: string
  fingerprint: WpsFingerprint
  target_tables: Record<string, { file_id: string; drive_id?: string }>
  target_date: string
  test_mode: boolean
  text?: string
  tables: WpsStructuredTable[]
  blocked: WpsBlockedItem[]
  warnings: string[]
  state?: string
  /** W6：预览阶段只有计划；`summary/stats` 在预览里也是计划口径。 */
  planned_summary?: WpsPlannedSummary
  /** W6：预览阶段执行口径必然 `proven_no_write=true`（还没写任何东西）。 */
  execution_summary?: WpsExecutionSummary
}

/** wps_upload(preview_id) 成功/部分/不确定/拒绝的统一返回。 */
export interface WpsUploadResult {
  ok: boolean
  status: string
  code?: string
  reason: string
  next_action: string
  /**
   * 旧字段名保留，但 R6 起含义已改为**执行口径**（= `execution_summary`）。
   * 不要再按 `summary.to_update` 展示“更新 N 行”。
   */
  summary?: WpsPlanSummary & Record<string, unknown>
  stats?: Record<string, unknown>
  /** W6：计划口径（要改几行）。 */
  planned_summary?: WpsPlannedSummary | null
  /** W6：执行口径（实际写入几行）。未知时为 `null` 字段 / `rows_unknown=true`。 */
  execution_summary?: WpsExecutionSummary | null
  /** 旧兼容字段：`planned_summary` 的镜像。 */
  summary_stats?: WpsPlannedSummary | null
  /** `execution_summary.rows_unknown` 的镜像。 */
  rows_unknown?: boolean
  operation_id: string
  preview_id?: string
  target_date?: string
  test_mode?: boolean
  failed?: number
  written?: number
  failed_sheets?: string[]
  text?: string
  message?: string
  uncertain?: boolean
  executor_status?: string
  executor_next_action?: string
  executor_operation_id?: string
  verification_missing?: boolean
  contradictory?: boolean
  possible_write?: boolean
  written_verified?: number
  journal_path?: string
  recovery?: Record<string, unknown> | null
  result?: {
    written: number
    failed: number
    sheets: Array<{
      sheet: string
      status: string
      reason?: string
      next_action?: string
      uncertain?: boolean
    }>
  }
}

export type WpsResult = WpsPreviewResult | WpsUploadResult

export interface PendingInteractionItem {
  interaction_id: string
  operation_id: string
  kind: string
  created_at: number
  expires_at: number
  status: 'pending'
  request: Record<string, unknown>
  /** 管理员非 owner 时 request 被脱敏为空；不要尝试用空 request 恢复客户输入。 */
  request_redacted?: boolean
}

export interface PendingInteractionsResult {
  ok: boolean
  interactions: PendingInteractionItem[]
  count: number
  next_action: string
}

export interface WpsRecoveryCounts {
  planned: number
  writing: number
  ledger_pending: number
  uncertain: number
  verified: number
  failed: number
  not_started: number
  [key: string]: number
}

export interface WpsRecoverySummary {
  operation_count: number
  pending_count: number
  uncertain_count: number
  failed_count: number
  not_started_count: number
  /** R6 新增：已带审计退场、但同目标防重复闸门仍保留的批次数。 */
  retired_guarded_count?: number
  has_pending: boolean
  needs_review: boolean
  guidance: string
}

export interface WpsRecoverySheet {
  target_date: string
  target_ref: string
  status: string
  raw_status: string
  error_code: string
  allowed_next_actions: string[]
  manual_required: boolean
  cloud_checked: boolean
  evidence: string
}

export interface WpsRecoveryOperation {
  operation_id: string
  operation_ref: string
  status: string
  pending: boolean
  cloud_checked: boolean
  created_at: string
  updated_at: string
  target_date: string
  target_refs: string[]
  sheet_count: number
  error_code: string
  allowed_next_actions: string[]
  manual_required: boolean
  sheets: WpsRecoverySheet[]
}

/** 只读恢复状态：普通用户 scope=summary 不含 operations/journal_path；管理员 scope=admin 也不含客户明细。 */
export interface WpsRecoveryStatus {
  ok: boolean
  contract_version: number
  source: string
  read_only: boolean
  queried_cloud: boolean
  contains_cloud_checked_records: boolean
  scope: 'summary' | 'admin'
  counts: WpsRecoveryCounts
  next_action: string
  error_code: string
  summary: WpsRecoverySummary
  operations?: WpsRecoveryOperation[]
  pending_operations?: WpsRecoveryOperation[]
  /** R6 新增：管理员带审计退场、但同目标防重复闸门仍保留的批次数。 */
  retired_guarded_count?: number
}

export type WpsRecoveryDecision =
  | 'retire_guarded'
  | 'cloud_verified'
  | 'cloud_untouched'
  | 'keep'

/** `wps_recovery_resolve` 请求体（数组里放**对象**，不要放 JSON 字符串）。 */
export interface WpsRecoveryResolvePayload {
  operation_id: string
  decision: WpsRecoveryDecision
  /** 逐字确认：必须与 `decision` 完全相同。 */
  confirm: string
  /** 人工核对说明，≥4 字符，写入 journal 审计。 */
  note: string
  /** 仅 `retire_guarded`：确认已人工核对云端表结构。 */
  confirm_structure_checked?: boolean
}

export interface WpsRecoveryResolveScope {
  operation_ref: string
  target_dates: string[]
  target_refs: string[]
  sheet_count: number
  guard_retained: boolean
  blocking: string
}

export interface WpsRecoveryResolveAudit {
  actor: string
  at: string
  decision: string
  note_recorded: boolean
  duplicate: boolean
  effects: {
    cloud_written: boolean
    guard_retained: boolean
    blocking: string
    auto_retry_allowed: boolean
  }
}

/**
 * `wps_recovery_resolve` 返回。**永不写云端**（`cloud_write=false`）。
 *
 * `ok=false` 时 `changed=false`，一个文件都不改：界面必须保持阻断，不能显示成功、
 * 不能提供“删除账本/直接重传”捷径。
 */
export interface WpsRecoveryResolveResult {
  ok: boolean
  status: string
  code?: string
  reason: string
  next_action: string
  contract_version?: number
  read_only?: boolean
  cloud_write?: boolean
  operation_id?: string
  operation_ref?: string
  changed?: boolean
  verified_on_disk?: boolean
  allowed_decisions?: string[]
  scope?: WpsRecoveryResolveScope
  audit?: WpsRecoveryResolveAudit
  recovery?: WpsRecoveryStatus | null
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


/** 未决记录的活跃状态：inflight = POST 前置记录已写、等待响应/对账；unresolved = 响应不确定。 */
export type SssUncertainRecordStatus = 'inflight' | 'unresolved'

/** 只读核对对单条记录的分类。 */
export type SssUncertainClassification =
  | 'station_missing'
  | 'station_found_other_day'
  | 'station_confirmed'
  | 'scan_failed'

/**
 * `sss_uncertain_records` 返回的一条未决记录（脱敏投影）。
 *
 * 电话/账号**已由后端掩码**（如 138****0001），前端只做兜底，绝不还原完整号码；
 * `journal_id` 是解除操作 record_ids 要提交的取值。
 */
export interface SssUncertainRecord {
  journal_id: string
  identifier: string
  sheet: string
  batch_id: string
  delivery_date: string
  status: SssUncertainRecordStatus | string
  error: string
  created_at: string
  batch_started_at: number | null
  name: string
  phone: string
  delivery_time: string
  door_num: string
  account: string
  reason: string
}

export interface SssUncertainCounts {
  active: number
  inflight: number
  unresolved: number
  resolved: number
  discarded: number
}

export interface SssUncertainReviewCounts {
  station_missing: number
  station_found_other_day: number
  station_confirmed: number
  scan_failed: number
}

/**
 * 上一次只读核对快照。没有快照时后端返回
 * `{available:false, stale:false, journal_matches:false, counts:{}, classifications:{}, ...}`。
 *
 * `journal_matches=false` 表示核对之后本地记录变了（或还没核对过）；
 * `station_absent` 解除必须先重新核对，否则服务端会以 review_required /
 * review_stale / journal_changed 拒绝。
 */
export interface SssUncertainReview {
  available: boolean
  checked_at: string
  age_s: number | null
  stale: boolean
  journal_fingerprint: string
  journal_matches: boolean
  wide_window_days: number
  counts: Partial<SssUncertainReviewCounts> & Record<string, number>
  classifications: Record<string, SssUncertainClassification | string>
}

/**
 * `sss_uncertain_records()` 返回（**只读，仅管理员，无网络**）。
 *
 * `ok=false`（journal 损坏/不可读等）时只有 status/code/reason/next_action，
 * 没有 counts/records/review；界面必须显示失败，绝不能当成“没有未决记录”。
 */
export interface SssUncertainState {
  ok: boolean
  status?: string
  code?: string
  reason?: string
  next_action: string
  read_only: boolean
  contract_version: number
  origin?: string
  account?: string
  journal?: string
  batch_key?: string
  delivery_date?: string
  counts?: SssUncertainCounts
  records?: SssUncertainRecord[]
  review?: SssUncertainReview
}

export type SssUncertainDecision = 'station_absent' | 'station_present' | 'keep'

/** `sss_uncertain_resolve` 请求体（数组里放**对象**，不要放 JSON 字符串）。 */
export interface SssUncertainResolvePayload {
  decision: SssUncertainDecision
  /** 逐字确认：必须与 `decision` 完全相同。 */
  confirm: string
  /** 人工核对说明，≥4 字符，写入本地 journal 审计。 */
  note: string
  /** 要处置的未决记录 journal_id；至少一条。 */
  record_ids: string[]
}

export interface SssUncertainResolveAudit {
  actor: string
  note: string
  at: string
  journal: string
  journal_fingerprint: string
  reviewed_at: string
}

/**
 * `sss_uncertain_resolve` 返回。**唯一写入口，但永不发 POST**：
 * `cloud_write=false`、`post_sent=false`；`changed=false` 时一个文件都没改。
 */
export interface SssUncertainResolveResult {
  ok: boolean
  status: string
  code: string
  reason: string
  next_action: string
  contract_version?: number
  read_only?: boolean
  cloud_write?: boolean
  post_sent?: boolean
  changed?: boolean
  decision?: string
  record_ids?: string[]
  affected?: number
  remaining?: number
  audit?: SssUncertainResolveAudit
  allowed_decisions?: string[]
}

/** `start_sss_review({password, remember})` 返回：启动只读核对 worker（零 POST）。 */
export interface SssReviewStartResult {
  ok: boolean
  status: string
  reason: string
  next_action: string
  summary?: { message?: string }
  operation_id?: string
  /** validation_failed 时的逐字段错误。 */
  fields?: Record<string, { message?: string }>
}

export interface AppState {
  version: string
  status: StatusState
  /** 当前登录账号是否管理员。后端按会话判定，前端据此只做显示裁剪。 */
  is_admin?: boolean
  /** Python 进程标识：用于识别重启后 sequence 归零，避免复用旧 cursor。 */
  event_producer_id?: string
  /** 运行平台：android = APK 自带 WebView；web = 纯浏览器访问。 */
  platform?: 'android' | 'web'
  /** 当前环境是否支持应用内下载安装（仅 APK 模式为 true）。 */
  can_self_update?: boolean
  /** 当前权威操作状态：断线重连后不依赖事件也能恢复 active/最近结果。 */
  operation: OperationStatusResult
  /** operation_status().operations 的镜像，按新→旧排序。 */
  operations: OperationInfo[]
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
  /** Release 页面地址；仅作为信息保留，更新按钮不再打开它。 */
  html_url?: string
  /** Android 模式：Release 里选中的 APK 资产名与大小。 */
  asset_name?: string
  size?: number
  /** 当前环境是否支持应用内下载安装。 */
  can_install?: boolean
}

export type UpdatePhase = 'downloading' | 'verifying' | 'installing'

export interface UpdateProgress {
  phase: UpdatePhase
  percent: number
  downloaded?: number
  total?: number
  message?: string
}

export interface TaskEventPayload {
  message: string
  status: StatusState | string
  result_status: StatusState | string
  ok: boolean
  success: boolean
  real_order: boolean
  stopped: boolean
  partial: boolean
  uncertain: boolean
  blocked: boolean
  needs_review: boolean
  next_action: string
  reason: string
  summary: Record<string, unknown>
  operation_id: string
  result: Record<string, unknown>
}

type BridgeEventBase =
  | { event: 'log'; payload: LogEntry }
  | { event: 'status'; payload: { state: StatusState } }
  | { event: 'task:done'; payload: TaskEventPayload }
  | { event: 'task:error'; payload: TaskEventPayload }
  | { event: 'update:available'; payload: UpdateAvailable }
  | { event: 'update:latest'; payload: { manual: boolean; current: string } }
  | { event: 'update:progress'; payload: UpdateProgress }
  | { event: 'update:permission_required'; payload: { message: string } }
  | { event: 'update:cancelled'; payload: { message?: string } }
  | { event: 'update:error'; payload: { code?: string; message: string } }
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
}

/** 订单表单防抖即时保存的载荷（不触发任务、不带密码）。 */
export interface OrderConfigPayload {
  url?: string
  phone?: string
  excel?: string
  date?: string
  count?: number | null
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
}

export interface FieldErrors {
  ok: boolean
  reason?: string
  message?: string
  fields?: Record<string, { message: string }>
  /** operation_status 统一冲突协议字段（rejected/busy 等）。 */
  code?: string
  status?: string
  next_action?: string
  operation_id?: string
  summary?: Record<string, unknown>
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

interface BackendApi {
  bridge_ready(): Promise<AppState>
  operation_status(operationId?: string): Promise<OperationStatusResult>
  start_order(payload: OrderFormPayload): Promise<FieldErrors>
  start_sss(payload: SssFormPayload): Promise<FieldErrors>
  sss_day_orders(): Promise<SssDayOrders>
  /** 管理员专用只读：列出未决记录与上次核对快照；无网络、不改文件。 */
  sss_uncertain_records(): Promise<SssUncertainState>
  /**
   * 管理员专用只读核对（零 POST）：启动 worker 扫描站内订单。
   * 结果通过 task:done/task:error 事件返回，前端不轮询。
   */
  start_sss_review(payload: { password: string; remember: boolean }): Promise<SssReviewStartResult>
  /**
   * 管理员专用唯一写入口：按 decision 解除/确认/保留阻断。
   * **永不发 POST**（post_sent=false），只写本地 journal 审计。
   */
  sss_uncertain_resolve(payload: SssUncertainResolvePayload): Promise<SssUncertainResolveResult>
  stop_task(): Promise<{ ok: boolean }>
  worker_alive(): Promise<boolean>
  resolve_decision(id: string, choice: string): Promise<{ ok: boolean }>
  resolve_captcha(id: string, code: string): Promise<{ ok: boolean }>
  resolve_address_input(id: string, entries: Record<string, string>): Promise<{ ok: boolean }>
  choose_excel(mode: 'order' | 'sss', path?: string): Promise<{ path: string; error: string }>
  new_template(mode: 'order' | 'sss', path?: string): Promise<{ path: string; error: string }>
  wps_status(): Promise<WpsStatus>
  wps_recovery_status(): Promise<WpsRecoveryStatus>
  /**
   * 管理员专用（普通用户 HTTP 403 `admin_only`）。
   * 只改本地 journal 审计，**永不写云端**；不提供“删除账本/直接重传”。
   */
  wps_recovery_resolve(payload: WpsRecoveryResolvePayload): Promise<WpsRecoveryResolveResult>
  wps_preview(): Promise<WpsPreviewResult>
  wps_upload(previewId: string): Promise<WpsUploadResult>
  pending_interactions(operationId?: string): Promise<PendingInteractionsResult>
  wps_authorize(): Promise<{ ok: boolean; reason?: string; hint?: string }>
  wps_logout(): Promise<{ ok: boolean; reason?: string }>
  wps_check_copies(): Promise<WpsCopyCheck>
  save_wps_config(payload: WpsConfigPayload): Promise<{ ok: boolean; reason?: string }>
  clear_password(mode: 'order' | 'sss'): Promise<ClearPasswordResult>
  check_updates(manual: boolean): Promise<{ ok: boolean; reason?: string; message?: string }>
  install_update(): Promise<{ ok: boolean; reason?: string; message?: string }>
  cancel_update(): Promise<{ ok: boolean }>
  open_install_settings(): Promise<{ ok: boolean; message?: string }>
  open_external(url: string): Promise<{ ok: boolean }>
  drain_events(lastSequence?: number, ackSequence?: number, producerId?: string): Promise<DrainEventsResult>
  request_close(): Promise<{ action: string }>
  set_split_ratio(ratio: number): Promise<{ ok: boolean; ratio: number }>
  save_order_config(payload: OrderConfigPayload): Promise<{ ok: boolean; reason?: string; saved?: { order_date: string; order_count: number | null } }>
  save_sss_config(payload: SssConfigPayload): Promise<{ ok: boolean; reason?: string }>
}

declare global {
  interface Window {
    // 仅依赖 localStorage / location 等标准字段，不再需要原生窗口壳注入 API。
  }
}

// ---------- 传输方式 ----------

/**
 * `http`：浏览器 ↔ app/web/server.py；
 * `mock`：无后端的纯静态预览，只给样式开发用的假数据。
 */
export type Transport = 'http' | 'mock'

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

const REQUEST_TIMEOUT_MS: Record<string, number> = {
  // 长操作：后端同步执行，超时只代表前端等待窗口结束，不代表服务端未执行。
  wps_upload: 240_000,
  wps_preview: 120_000,
  sss_day_orders: 120_000,
  wps_check_copies: 120_000,
  // 未决记录列表只是本地读盘；启动核对 worker 要等登录 + 扫站内订单，给足窗口；
  // 解除只写本地审计，但仍可能重新读盘核对指纹，给 60s。
  sss_uncertain_records: 30_000,
  start_sss_review: 120_000,
  sss_uncertain_resolve: 60_000,
  // `cloud_verified/cloud_untouched` 需要重新只读云端，给足窗口。
  wps_recovery_resolve: 120_000,
  install_update: 60_000,
  check_updates: 60_000,
  // 配置/启动/状态是短请求。
  save_order_config: 15_000,
  save_sss_config: 15_000,
  save_wps_config: 15_000,
  operation_status: 15_000,
  worker_alive: 15_000,
  bridge_ready: 15_000,
}

function timeoutFor(method: string): number {
  return REQUEST_TIMEOUT_MS[method] ?? 30_000
}

async function postJson(
  url: string,
  body: unknown,
  timeoutMs = 30_000,
): Promise<{ ok: boolean; status: number; data: unknown }> {
  const controller = typeof AbortController === 'undefined' ? null : new AbortController()
  const timer = controller ? setTimeout(() => controller.abort(), timeoutMs) : undefined
  try {
    const response = await fetch(url, {
      method: 'POST',
      headers: authHeaders(),
      body: JSON.stringify(body),
      signal: controller?.signal,
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
  } catch (error) {
    if (controller?.signal.aborted) {
      throw new RequestError(
        'timeout',
        `请求超时（${Math.round(timeoutMs / 1000)} 秒内未返回）`,
        { cause: error },
      )
    }
    throw toRequestError(error)
  } finally {
    if (timer !== undefined) clearTimeout(timer)
  }
}

function errorMessage(data: unknown, status: number): string {
  if (data && typeof data === 'object' && 'error' in data) {
    const message = (data as { error?: unknown }).error
    if (typeof message === 'string' && message) return message
  }
  return `请求失败（HTTP ${status}）`
}

function requestErrorForStatus(status: number, data: unknown): RequestError {
  const message = status === 401
    ? '访问令牌无效或已失效，请用带有效 ?token= 的完整网址重新打开'
    : errorMessage(data, status)
  let kind: ConstructorParameters<typeof RequestError>[0] = 'client'
  if (status === 401) kind = 'auth'
  else if (status === 403) kind = 'permission'
  else if (status === 408 || status === 504 || status === 524) kind = 'timeout'
  else if (status >= 500) kind = 'server'
  return new RequestError(kind, message, { status })
}

export interface GlobalRequestIssue extends RequestErrorView {
  id: number
  scope: string
  at: number
}

const issueListeners = new Set<(issue: GlobalRequestIssue) => void>()
let issueSeq = 0

export function onRequestIssue(listener: (issue: GlobalRequestIssue) => void): () => void {
  issueListeners.add(listener)
  return () => issueListeners.delete(listener)
}

/** 把错误广播给全局错误条；纯逻辑错误分类在 requestError.ts。 */
export function reportRequestError(error: unknown, scope = '请求'): RequestError {
  const normalized = toRequestError(error)
  const view = classifyRequestError(normalized)
  const issue: GlobalRequestIssue = {
    id: (issueSeq += 1),
    scope,
    at: Date.now(),
    ...view,
  }
  for (const listener of issueListeners) listener(issue)
  return normalized
}

/** 用 HTTP 实现整个 BackendApi：方法名即路径，参数按位置传。 */
function createHttpApi(): BackendApi {
  const call = async (method: string, args: unknown[]): Promise<unknown> => {
    let response: { ok: boolean; status: number; data: unknown }
    try {
      response = await postJson(`/api/${method}`, args, timeoutFor(method))
    } catch (error) {
      throw reportRequestError(error, method)
    }
    const { ok, status, data } = response
    if (!ok) {
      throw reportRequestError(requestErrorForStatus(status, data), method)
    }
    return data
  }
  return new Proxy({} as BackendApi, {
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

export function onBridgeEvent(listener: Listener): () => void {
  listeners.add(listener)
  return () => listeners.delete(listener)
}

let httpApi: BackendApi | null = null

export function api(): BackendApi {
  if (transport === 'http') {
    httpApi ??= createHttpApi()
    return httpApi
  }
  throw new Error('后端 API 尚未就绪')
}

export function isApiReady(): boolean {
  return transport === 'http'
}

export interface ReadyResult {
  state: AppState
  /** 模拟浏览器开发环境（无 Python 壳）时为 true。 */
  mocked: boolean
  /** 实际使用的传输方式，便于界面区分「HTTP 网页版」与「mock 预览」。 */
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
 * 建立连接：优先探测 HTTP 后端；探测不到才进入 mock 预览（仅样式开发）。
 */
export async function connectBridge(): Promise<ReadyResult> {
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
    transport = 'mock'
    apiReady = true
    return { state: mockState(), mocked: true, transport, authError }
  }
  // 浏览器直开（无后端）：提供 mock 状态方便样式开发。
  transport = 'mock'
  apiReady = true
  return { state: mockState(), mocked: true, transport, authError: '' }
}

/**
 * 浏览器直开（无后端）时的**纯展示**初始状态，只用于样式开发/无后端演示。
 *
 * 这里不得出现真实手机号/账号或开发机绝对路径：`dist/index.html` 会被内联进 APK
 * 随测试版分发。所有"未配置"字段一律留空字符串——这与全新安装的真实状态一致，
 * 且非管理员不校验这些隐藏字段（见 `lib/formValidation.ts`），演示流程不受影响。
 * 若将来某个演示场景确实需要占位值，只能使用一眼可辨的合成值（例如
 * `13800000000`、`/tmp/synthetic-*.xlsx`），并在此处说明来源。
 */
function mockState(): AppState {
  const idleOperation: OperationStatusResult = {
    ok: true,
    active: false,
    operation_id: '',
    mode: '',
    status: 'idle',
    phase: '',
    summary: {},
    next_action: '',
    reason: '',
    started_at: null,
    finished_at: null,
    operations: [],
  }
  return {
    version: '3.0.0-dev',
    status: 'ready',
    operation: idleOperation,
    operations: [],
    config: {
      target_url: 'https://m.icall.me/admin/#/login',
      // 凭据类字段留空：不携带任何来源未确认的真实手机号/账号。
      phone_number: '',
      excel_path: '',
      order_date: '2026-09-05',
      order_count: null,
      split_ratio: 0.38,
      sss_url: 'https://sssplusnew.zhuopaikeji.com/takeout',
      sss_account: '',
      sss_excel_path: '',
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
