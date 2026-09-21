/**
 * 真实渲染组件 + 合成/mock HTTP 后端的浏览器交互检查。
 *
 * 边界：
 * - 只服务 frontend/dist/index.html；所有 /api/* 都是脚本内合成 JSON + 计数；
 * - 不调用真实 WPS/闪时送/订单，不读系统凭据，不写正式文件；
 * - 本机 Chrome only（Headless）；不安装任何依赖。
 *
 * 运行：node frontend/scripts/browser-interaction-check.mjs
 */
import http from 'node:http'
import net from 'node:net'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { spawn, spawnSync } from 'node:child_process'
import { fileURLToPath } from 'node:url'
import assert from 'node:assert/strict'

const HERE = path.dirname(fileURLToPath(import.meta.url))
const ROOT = path.resolve(HERE, '..')
const DIST = path.join(ROOT, 'dist')
const INDEX_HTML = fs.readFileSync(path.join(DIST, 'index.html'), 'utf8')

const results = []
/** R8：当前场景/断言名。页面异常按它归属，用于把异常精确定位到具体场景。 */
let currentScenario = '启动/握手'
function enterScenario(name) { currentScenario = name }

function record(name, ok, detail = '') {
  results.push({ name, ok, detail })
  currentScenario = name
  console.log(`${ok ? 'PASS' : 'FAIL'} ${name}${detail ? ` :: ${detail}` : ''}`)
}

/* ------------------------------------------------------------------ */
/* R8：浏览器页面异常采集                                              */
/*                                                                     */
/* 采集三类事件（CDP），在任何导航与业务脚本执行之前订阅：              */
/* - `Runtime.exceptionThrown` → 未捕获异常 / `Uncaught (in promise)` 未处理拒绝； */
/* - `Runtime.consoleAPICalled`（type=error）→ console.error；          */
/* - `Log.entryAdded`（level=error）→ 浏览器级错误（含资源加载失败）。 */
/*                                                                     */
/* 每条记录都带「场景 + 脱敏后的文本 + 位置/栈」；未预期的异常会让     */
/* 收尾的 R8 断言失败 → 进程退出码非 0。                                */
/* ------------------------------------------------------------------ */
const pageErrors = []
function resetPageErrors() { pageErrors.length = 0 }

/** 脱敏：去掉 token/密码/凭据与长十六进制、长 base64 串，并截断长度。 */
function redactPageError(value) {
  let text = String(value ?? '').replace(/\s+/g, ' ').trim()
  text = text.replace(/([?&](?:token|password|pwd|secret|api_key|key)=)[^&\s]*/gi, '$1<redacted>')
  text = text.replace(/\b(Bearer\s+)[A-Za-z0-9._~+/-]+=*/gi, '$1<redacted>')
  text = text.replace(/\b(password|passwd|pwd|secret|credential|token)\b\s*[:=]\s*["']?[^\s"',;]+/gi, '$1=<redacted>')
  text = text.replace(/\b[A-Fa-f0-9]{32,}\b/g, (match) => `${match.slice(0, 8)}…<redacted>`)
  text = text.replace(/\b[A-Za-z0-9+/]{40,}={0,2}\b/g, (match) => `${match.slice(0, 8)}…<redacted>`)
  return text.length > 300 ? `${text.slice(0, 300)}…` : text
}

function recordPageError(kind, text, extra = {}) {
  pageErrors.push({
    kind,
    scenario: currentScenario,
    text: redactPageError(text),
    at: new Date().toISOString(),
    ...extra,
  })
}

/** CDP `Runtime.exceptionThrown`：未捕获异常与未处理 Promise 拒绝。 */
function collectPageException(params) {
  const details = params?.exceptionDetails || {}
  const text = String(details.text || '')
  const description = String(details.exception?.description ?? details.exception?.value ?? '')
  const rejection = /in promise|unhandledrejection/i.test(`${text} ${description}`)
  const frames = details.stackTrace?.callFrames || []
  recordPageError(rejection ? 'rejection' : 'exception', `${text} ${description}`.trim() || '(无描述)', {
    url: details.url || frames[0]?.url || '',
    line: Number.isFinite(details.lineNumber) ? details.lineNumber + 1 : null,
    column: Number.isFinite(details.columnNumber) ? details.columnNumber + 1 : null,
    stack: frames.slice(0, 3)
      .map((frame) => `${frame.functionName || '(anonymous)'}@${frame.url || '?'}:${(frame.lineNumber ?? 0) + 1}`)
      .join(' <- '),
  })
}

/** CDP `Runtime.consoleAPICalled`：只收 type=error（console.error）与失败的 console.assert。 */
function collectConsoleCall(params) {
  if (params?.type !== 'error' && params?.type !== 'assert') return
  const parts = (params.args || []).map((arg) => {
    if (arg?.value !== undefined) return typeof arg.value === 'string' ? arg.value : JSON.stringify(arg.value)
    return String(arg?.description ?? arg?.unserializableValue ?? '')
  })
  recordPageError('consoleError', parts.join(' ').trim() || '(空console.error)', {
    url: params.stackTrace?.callFrames?.[0]?.url || '',
    line: Number.isFinite(params.stackTrace?.callFrames?.[0]?.lineNumber)
      ? params.stackTrace.callFrames[0].lineNumber + 1
      : null,
  })
}

/** CDP `Log.entryAdded`：浏览器级 error 条目（资源加载失败、未捕获异常也会进这里）。 */
function collectLogEntry(params) {
  const entry = params?.entry || {}
  if (entry.level !== 'error') return
  const source = String(entry.source || '')
  const text = `${source}: ${entry.text || ''}`.trim()
  // 与 Runtime 通道去重：脚本类错误已由 exceptionThrown 记录，console 调用已由 consoleAPICalled 记录。
  if (source === 'javascript' || source === 'console-api') return
  recordPageError(source === 'network' ? 'resourceError' : 'logError', text, { url: entry.url || '' })
}

/**
 * R8：预期内异常白名单。
 *
 * **只允许**「场景 + 资源 + 内容 + 次数」都精确命中的条目，多出一次即算未预期异常。
 *
 * 设计口径：
 * - 只有 `resourceError`（`Log.entryAdded` 的 network 条目）可以被白名单；未捕获异常、未处理拒绝、
 *   `console.error`、浏览器其它 log 错误**一律不允许**出现，出现即门禁失败。
 * - 定位以 `端点 + 错误码 + max=1` 为准：浏览器日志事件可能比同一场景的 DOM 断言晚几毫秒到达，
 *   所以场景名用「场景族」正则（marker 名或紧随其后的断言名都能命中），端点与错误码则是精确的。
 * - 每条都必须说明「为什么这个场景必然产生这条浏览器错误」。
 */
const EXPECTED_PAGE_ERRORS = [
  {
    scenario: /密码清除/,
    kind: 'resourceError',
    url: /\/api\/clear_password$/,
    content: /ERR_EMPTY_RESPONSE/,
    max: 1,
    reason: '「order 密码清除网络中断」用 req.socket.destroy() 在响应前断开 socket（验证保留草稿、不显示已清除），浏览器必然记录 1 条 /api/clear_password 加载失败；该场景的断言本身就要求这次请求失败。',
  },
  {
    scenario: /验证码/,
    kind: 'resourceError',
    url: /\/api\/resolve_captcha$/,
    content: /ERR_EMPTY_RESPONSE/,
    max: 1,
    reason: '「验证码提交网络失败」同样故意销毁 socket（验证保留输入、不自动重提），断言要求这次请求失败。',
  },
  {
    scenario: /恢复查询/,
    kind: 'resourceError',
    url: /\/api\/wps_recovery_status$/,
    content: /status of 403/,
    max: 1,
    reason: '「恢复查询无权限」场景要求服务端返回 403（验证准确的权限提示、不误报网络故障、不反复请求）；这条 403 是该场景的预期结果。',
  },
  {
    scenario: /断线恢复|FE-1/,
    kind: 'resourceError',
    url: /\/api\/operation_status_unreachable$/,
    content: /ERR_CONNECTION_REFUSED/,
    max: 1,
    reason: '「FE-1 断线错误恢复」故意把 operation_status 重定向到无人监听的端口（避免 Chrome 传输层自动重发污染请求计数），必然产生 1 条连接被拒。',
  },
  {
    scenario: /恢复处置/,
    kind: 'resourceError',
    url: /\/api\/wps_recovery_resolve$/,
    content: /status of 403/,
    max: 1,
    reason: '「管理员恢复处置权限不足」场景要求 403（验证阻断保持、不显示成功、不自动重试）。',
  },
  {
    scenario: /恢复处置/,
    kind: 'resourceError',
    url: /\/api\/wps_recovery_resolve_unreachable$/,
    content: /ERR_CONNECTION_REFUSED/,
    max: 1,
    reason: '「恢复处置网络失败」用死端口重定向模拟结果未知（验证阻断保持且不自动重试）。',
  },
]

/** 按白名单切分：命中的算预期，其余一律未预期（并会打印脱敏后的原文）。 */
function classifyPageErrors() {
  const remaining = pageErrors.slice()
  const expected = []
  for (const rule of EXPECTED_PAGE_ERRORS) {
    let used = 0
    for (let index = 0; index < remaining.length && used < rule.max;) {
      const entry = remaining[index]
      const hit = entry.kind === rule.kind
        && rule.scenario.test(entry.scenario)
        && rule.content.test(entry.text)
        && (!rule.url || rule.url.test(String(entry.url || '')))
      if (!hit) { index += 1; continue }
      expected.push({ reason: rule.reason, entry })
      remaining.splice(index, 1)
      used += 1
    }
  }
  return { expected, unexpected: remaining }
}

/**
 * 截图 + 脱敏页面异常日志的输出目录。
 *
 * 默认每个进程一个独立临时目录：并行任务/并行运行不会互相覆盖；
 * 需要固定目录时显式传 `UI_SCREEN_DIR`（mutation-check 就是这么做的）。
 */
const SCREEN_DIR = process.env.UI_SCREEN_DIR
  || fs.mkdtempSync(path.join(os.tmpdir(), 'yikou-fe-shots-'))
async function captureScreenshot(cdp, name) {
  try {
    fs.mkdirSync(SCREEN_DIR, { recursive: true })
    const shot = await cdp.send('Page.captureScreenshot', { format: 'png' })
    fs.writeFileSync(path.join(SCREEN_DIR, name), Buffer.from(shot.data, 'base64'))
  } catch (error) {
    console.warn('screenshot failed', name, error?.message || error)
  }
}

function syntheticConfig() {
  return {
    target_url: 'https://mock.example/admin',
    phone_number: '10000000000',
    excel_path: '/tmp/synthetic-order.xlsx',
    order_date: '2026-09-19',
    order_count: null,
    split_ratio: 0.38,
    sss_url: 'https://mock.example/sss',
    sss_account: '10000000001',
    sss_excel_path: '/tmp/synthetic-sss.xlsx',
    sss_order_source: 'wps',
    sss_product_name: '轻食',
    sss_common_address: '合成地址',
    sss_use_fixed_address: true,
    sss_fixed_lnt: 119.7,
    sss_fixed_lat: 30.2,
    sss_fixed_area_code: '330110',
    sss_fixed_address_detail: '合园区',
    sss_dry_run: true,
    sss_preflight: false,
    sss_idempotency_field: '',
    wps_enabled: true,
    wps_test_mode: true,
    wps_test_file_id: 'FTEST',
    wps_test_drive_id: '',
    wps_drive_id: '',
    wps_cli_path: '/tmp/synthetic-kdocs-cli',
    wps_tables: { 东湖中餐: { file_id: 'F1' } },
    wps_test_tables: {},
    wps_target_hour_start: 20,
    wps_target_hour_end: 10,
    wps_marker_enabled: true,
  }
}

function idleOperation() {
  return {
    ok: true, active: false, operation_id: '', mode: '', status: 'idle', phase: '',
    reason: '', next_action: '', summary: {}, started_at: null, finished_at: null, operations: [],
  }
}

const mock = {
  pendingItems: [],
  /**
   * 合成日志事件队列：`drain_events` 取走后清空（模拟真实投递，不重复投递）。
   * `logSequence` 全程只增不减 —— 页面 reload 会从 localStorage 恢复 eventCursor，
   * 复位序号会让新日志被当成「旧事件」跳过。
   */
  logEvents: [],
  logSequence: 0,
  logDelivered: 0,
  clearResult: 'success',
  clearCalls: 0,
  clearModes: [],
  pendingCalls: 0,
  resolveDecisionCalls: 0,
  resolveCaptchaCalls: 0,
  resolveCaptchaNetwork: false,
  resolveAddressCalls: 0,
  isAdmin: true,
  operationStatus: null,
  /** FE-1：让 operation_status 网络失败，用于复核「断线 → 重新连接 → 恢复」流程。 */
  operationStatusNetwork: false,
  /** 危险执行入口计数（FE-1：uncertain 下必须为 0，且刷新不得自动重传）。 */
  startOrderCalls: 0,
  startSssCalls: 0,
  stopTaskCalls: 0,
  wpsUploadCalls: 0,
  wpsUploadActive: 0,
  wpsUploadMaxActive: 0,
  wpsUploadDelay: 0,
  wpsUpload: null,
  wpsPreviewCalls: 0,
  wpsPreview: null,
  wpsRecoveryCalls: 0,
  wpsRecoveryForbidden: false,
  wpsResolveCalls: 0,
  wpsResolveActive: 0,
  wpsResolveMaxActive: 0,
  wpsResolveDelay: 0,
  wpsResolveNetwork: false,
  wpsResolveForbidden: false,
  wpsResolveResult: null,
  wpsResolvePayloads: [],
  wpsResolveTimes: [],
  /** 无人监听的本地端口：用于模拟网络失败而又不让请求被传输层重发。 */
  deadPort: 0,
  wpsStatusDelay: 0,
  wpsStatusCliPath: '/tmp/synthetic-kdocs-cli',
  wpsStatusAddressOrder: {},
  saveOrderDelay: 0,
  saveOrderFail: false,
  saveOrderPayloads: [],
  saveOrderActive: 0,
  saveOrderMaxActive: 0,
  saveSssDelay: 0,
  saveSssFail: false,
  saveSssPayloads: [],
  saveSssActive: 0,
  saveSssMaxActive: 0,
  saveWpsDelay: 0,
  saveWpsFail: false,
  saveWpsPayloads: [],
  saveWpsActive: 0,
  saveWpsMaxActive: 0,
  wpsRecovery: {
    ok: true, journal_path: '/tmp/synthetic-journal.json', operations: [], pending_operations: [],
    counts: {}, next_action: 'none', contract_version: 1, source: 'local_journal',
    read_only: true, queried_cloud: false, contains_cloud_checked_records: false,
  },
}

/** W6：合成预览（计划口径 + 执行口径 proven_no_write）。 */
function syntheticPreview(overrides = {}) {
  return {
    ok: true, status: 'preview_ready', reason: '', code: '', next_action: 'wps_upload(preview_id)',
    summary: { to_update: 1, to_append: 0, unchanged: 0, warned: 0, skipped: 0, blocked: 0 },
    stats: { to_update: 1, to_append: 0, unchanged: 0, warned: 0, skipped: 0, blocked: 0 },
    planned_summary: {
      kind: 'plan', contract_version: 1,
      rows: { to_update: 1, to_append: 0, unchanged: 0, skipped: 0, warned: 0, blocked: 0 },
    },
    execution_summary: {
      kind: 'execution', contract_version: 1, status: 'preview_ready', executed: false,
      counts_source: 'rejected_before_write',
      sheets: { total: 1, verified: 0, noop: 0, failed: 0, uncertain: 0, skipped: 0, blocked: 0, other: 0 },
      rows: { verified: 0, failed: 0, uncertain: 0, skipped: 0, planned: 1 },
      rows_unknown: false, proven_no_write: true, written_sheets: 0, failed_sheets: 0,
    },
    operation_id: '', preview_id: 'pv-mock-w6-1',
    created_at: new Date(Date.now() - 60_000).toISOString(),
    expires_at: new Date(Date.now() + 9 * 60_000).toISOString(),
    expires_in: 540, ttl_seconds: 600,
    local_sha256: 'aaaaaaaa', context_fingerprint: 'ctx-mock', plan_fingerprint: 'plan-mock',
    fingerprint: { local_sha256: 'aaaaaaaa', context: 'ctx-mock', plan: 'plan-mock' },
    target_tables: { 东湖中餐: { file_id: 'FTEST' } },
    target_date: '2026-09-20', test_mode: true,
    tables: [{
      sheet: '东湖中餐', file_id: 'F1', drive_id: '', target_date: '2026-09-20',
      target_col: 3, target_header: '2026-09-20', weekday_number: 6,
      counts: { to_update: 1, to_append: 0, unchanged: 0, skipped: 0, warned: 0, blocked: 0 },
      changes: [], warnings: [],
    }],
    blocked: [], warnings: [], text: '合成预览全文（mock，仅用于浏览器断言）',
    ...overrides,
  }
}

function json(res, data, status = 200) {
  const body = JSON.stringify(data)
  res.writeHead(status, { 'Content-Type': 'application/json; charset=utf-8' })
  res.end(body)
}

function readBody(req) {
  return new Promise((resolve) => {
    let data = ''
    req.on('data', (chunk) => { data += chunk })
    req.on('end', () => {
      if (!data) return resolve(null)
      try { resolve(JSON.parse(data)) } catch { resolve(null) }
    })
  })
}

/* ------------------------------------------------------------------ */
/* 合成日志数据（只走真实投递路径：drain_events → 前端 appendLog）      */
/*                                                                     */
/* 覆盖四类形态，正好对应「重复展示」缺陷的四个触发点：                 */
/* - 长单行：>120 字但只有一行（旧规则只看长度 → 误报可展开）；         */
/* - 多行：首行摘要 + 隐藏的后续行（展开必须是替换，不是追加）；        */
/* - 订单摘要：逐字段本来就显示完整（旧规则仍给「展开明细」→ 重复）；   */
/* - 错误行：默认级别「全部」时必须可见，不能被默认过滤掉。             */
/* ------------------------------------------------------------------ */
const SYNTHETIC_LONG_LINE =
  'WPS 只读核对完成：目标日期 2026-09-20，目标表「一口轻食排单表」，'
  + '读取 37 行，其中待核对 1 行、已确认 36 行，未发现重复写入痕迹；'
  + '本次为只读查询，不会上传、不会解除阻断、不会自动重试，也不会恢复写入；'
  + '如需继续请先人工只读核对云端与日志。'

const SYNTHETIC_MULTI_LINE = [
  '闪时送下单失败：地址无法自动识别',
  '原始地址：A座12层 靠窗那个工位',
  '规则建议：A12 / 学三 / 教5',
  '处理办法：在待确认地址里补全后重试，不要直接重跑本批',
].join('\n')

const SYNTHETIC_ORDER_SUMMARY = 'W123456｜张三｜小份｜已下单'
/** 只出现在多行日志**隐藏行**里的搜索词（用来验证「命中隐藏内容要可见」）。 */
const SYNTHETIC_HIDDEN_NEEDLE = '教5'
const SYNTHETIC_NO_MATCH = 'zzz-绝对匹配不到-zzz'

/** 合成一批日志事件；`sequence` 单调递增，reload 后也不会被 cursor 跳过。 */
function queueLogs(entries) {
  for (const entry of entries) {
    mock.logSequence += 1
    mock.logEvents.push({
      event: 'log',
      payload: { ts: entry.ts || '12:00:00', level: entry.level || 'INFO', msg: entry.msg },
      event_id: `mock-log-${mock.logSequence}`,
      sequence: mock.logSequence,
      created_at: Date.now(),
      droppable: true,
    })
  }
  return mock.logSequence
}

/** 本场景默认的合成日志集合（长单行 / 多行 / 订单 / 错误）。 */
function queueSyntheticLogSet() {
  return queueLogs([
    { ts: '12:00:01', level: 'INFO', msg: SYNTHETIC_LONG_LINE },
    { ts: '12:00:02', level: 'WARN', msg: SYNTHETIC_MULTI_LINE },
    { ts: '12:00:03', level: 'OK', msg: SYNTHETIC_ORDER_SUMMARY },
    { ts: '12:00:04', level: 'ERROR', msg: '下单失败：验证码错误，请重新获取后再试' },
  ])
}

function createMockServer() {
  return http.createServer(async (req, res) => {
    const url = new URL(req.url || '/', 'http://127.0.0.1')
    if (req.method !== 'POST' || !url.pathname.startsWith('/api/')) {
      res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' })
      res.end(INDEX_HTML)
      return
    }
    const name = url.pathname.slice('/api/'.length)
    const args = await readBody(req)
    const firstArg = Array.isArray(args) ? args[0] : undefined

    switch (name) {
      case 'bridge_ready':
        return json(res, {
          version: 'mock-1', status: 'ready', event_producer_id: 'mock-producer',
          is_admin: mock.isAdmin, platform: 'web', can_self_update: false,
          operation: mock.operationStatus || idleOperation(), operations: [],
          config: syntheticConfig(), passwords: { order: '', sss: '' },
        })
      case 'drain_events': {
        const events = mock.logEvents.splice(0, mock.logEvents.length)
        mock.logDelivered += events.length
        return json(res, {
          events, producer_id: 'mock-producer',
          latest_sequence: mock.logSequence, acked_sequence: mock.logSequence,
          dropped_count: 0, first_available_sequence: 0,
        })
      }
      case 'operation_status':
      case 'worker_alive':
        if (name === 'worker_alive') return json(res, false)
        if (mock.operationStatusNetwork) {
          // 同 wpsResolveNetwork：用「重定向到无人监听的端口」模拟网络失败，
          // 避免 socket 被销毁时 Chrome 在传输层自动重发而污染请求计数。
          res.writeHead(302, { Location: `http://127.0.0.1:${mock.deadPort || 1}/api/operation_status_unreachable` })
          res.end()
          return
        }
        return json(res, mock.operationStatus || idleOperation())
      case 'stop_task':
        mock.stopTaskCalls += 1
        return json(res, { ok: true })
      case 'pending_interactions': {
        mock.pendingCalls += 1
        const wanted = String(firstArg || '')
        const items = wanted
          ? mock.pendingItems.filter((item) => item.operation_id === wanted)
          : mock.pendingItems
        return json(res, { ok: true, interactions: items, count: items.length, next_action: items.length ? 'resolve_*' : '' })
      }
      case 'resolve_decision': {
        mock.resolveDecisionCalls += 1
        const id = String(firstArg || '')
        mock.pendingItems = mock.pendingItems.filter((item) => item.interaction_id !== id)
        return json(res, { ok: true, status: 'accepted', interaction_id: id, operation_id: 'op-test', next_action: '' })
      }
      case 'resolve_captcha': {
        mock.resolveCaptchaCalls += 1
        if (mock.resolveCaptchaNetwork) {
          req.socket.destroy()
          return
        }
        const id = String(firstArg || '')
        mock.pendingItems = mock.pendingItems.filter((item) => item.interaction_id !== id)
        return json(res, { ok: true, status: 'accepted', interaction_id: id, operation_id: 'op-test', next_action: '' })
      }
      case 'resolve_address_input': {
        mock.resolveAddressCalls += 1
        const id = String(firstArg || '')
        mock.pendingItems = mock.pendingItems.filter((item) => item.interaction_id !== id)
        return json(res, { ok: true, status: 'accepted', interaction_id: id, operation_id: 'op-test', next_action: '' })
      }
      case 'clear_password': {
        mock.clearCalls += 1
        mock.clearModes.push(firstArg || 'order')
        if (mock.clearResult === 'network') {
          req.socket.destroy()
          return
        }
        if (mock.clearResult === 'fail') {
          return json(res, {
            ok: false, status: 'error', state: 'delete_failed', mode: firstArg || 'order',
            deleted: false, reason: '模拟删除失败：凭据仍可读或后端不可用',
            next_action: '检查系统凭据管理器后重试；不要把失败当作已清除', summary: {},
          })
        }
        if (mock.clearResult === 'already') {
          return json(res, {
            ok: true, status: 'no_change', state: 'already_cleared', mode: firstArg || 'order',
            deleted: false, reason: '该密码此前已清除', next_action: '', summary: {},
          })
        }
        return json(res, {
          ok: true, status: 'success', state: 'deleted', mode: firstArg || 'order',
          deleted: true, reason: '', next_action: '', summary: {},
        })
      }
      case 'wps_status': {
        const respond = () => json(res, {
          ok: true, reason: '', enabled: true, test_mode: true,
          cli_path: mock.wpsStatusCliPath, cli_found: true, authenticated: true,
          target_date: '2026-09-20', weekday_number: 6,
          excel_path: '/tmp/synthetic-order.xlsx', marker_enabled: true,
          sort_enabled: true, address_order: mock.wpsStatusAddressOrder, address_order_defaults: {},
          writing_test_copies: true, production_tables: {}, state_path: '/tmp/synthetic-ledger.json',
          tables: [{ sheet: '东湖中餐', file_id: 'F1', effective_file_id: 'FTEST', last_sync: '', last_people: 0 }],
        })
        if (mock.wpsStatusDelay > 0) setTimeout(respond, mock.wpsStatusDelay)
        else respond()
        return
      }
      case 'wps_recovery_status':
        mock.wpsRecoveryCalls += 1
        if (mock.wpsRecoveryForbidden) {
          return json(res, { error: '该操作仅管理员可用', code: 'admin_only' }, 403)
        }
        return json(res, mock.wpsRecovery)
      case 'wps_recovery_resolve': {
        mock.wpsResolveCalls += 1
        mock.wpsResolvePayloads.push(firstArg || null)
        mock.wpsResolveTimes.push(Date.now())
        mock.wpsResolveActive += 1
        mock.wpsResolveMaxActive = Math.max(mock.wpsResolveMaxActive, mock.wpsResolveActive)
        if (mock.wpsResolveNetwork) {
          // 用「重定向到无人监听的端口」模拟网络失败，而不是 req.socket.destroy()：
          // 实测 Chrome 对 socket 被销毁的 POST 会在传输层自动重发（1 次 fetch → 3 次
          // 服务端请求），这会让「服务端请求数」无法代表「用户提交次数」。
          // 重定向目标连接被拒 → 前端同样拿到 Failed to fetch，而本服务端只收到 1 次。
          mock.wpsResolveActive = Math.max(0, mock.wpsResolveActive - 1)
          res.writeHead(302, { Location: `http://127.0.0.1:${mock.deadPort || 1}/api/wps_recovery_resolve_unreachable` })
          res.end()
          return
        }
        const respondResolve = () => {
          mock.wpsResolveActive = Math.max(0, mock.wpsResolveActive - 1)
          if (mock.wpsResolveForbidden) {
            return json(res, { ok: false, status: 'forbidden', code: 'forbidden', reason: '仅管理员可用', changed: false }, 403)
          }
          return json(res, mock.wpsResolveResult || {
            ok: true, status: 'retired_guarded', code: 'retired_guarded',
            reason: '已按管理员决策处理，本地 journal 已写入审计',
            next_action: 'manual_reconcile', contract_version: 1, read_only: false,
            cloud_write: false, operation_id: 'wps-1234567890abcdef',
            operation_ref: 'wps-op:abcdef123456', changed: true, verified_on_disk: true,
            scope: {
              operation_ref: 'wps-op:abcdef123456', target_dates: ['2026-09-20'],
              target_refs: ['wps-target:abc123def456'], sheet_count: 1,
              guard_retained: true, blocking: 'retired_guarded',
            },
            audit: {
              actor: 'admin@example.com', at: '2026-09-20T10:00:00', decision: 'retire_guarded',
              note_recorded: true, duplicate: false,
              effects: { cloud_written: false, guard_retained: true, blocking: 'retired_guarded', auto_retry_allowed: false },
            },
          })
        }
        if (mock.wpsResolveDelay > 0) setTimeout(respondResolve, mock.wpsResolveDelay)
        else respondResolve()
        return
      }
      case 'start_order':
        mock.startOrderCalls += 1
        return json(res, { ok: true })
      case 'start_sss':
        mock.startSssCalls += 1
        return json(res, { ok: true })
      case 'wps_upload': {
        mock.wpsUploadCalls += 1
        mock.wpsUploadActive += 1
        mock.wpsUploadMaxActive = Math.max(mock.wpsUploadMaxActive, mock.wpsUploadActive)
        const payload = mock.wpsUpload || { ok: false, status: 'rejected', code: 'preview_missing', reason: 'mock 拒绝', next_action: '重新预览', operation_id: '' }
        const respondUpload = () => {
          mock.wpsUploadActive = Math.max(0, mock.wpsUploadActive - 1)
          json(res, payload)
        }
        if (mock.wpsUploadDelay > 0) setTimeout(respondUpload, mock.wpsUploadDelay)
        else respondUpload()
        return
      }
      case 'wps_preview':
        mock.wpsPreviewCalls += 1
        if (mock.wpsPreview) return json(res, mock.wpsPreview)
        return json(res, {
          ok: false, status: 'rejected', reason: 'mock 预览未启用', code: 'mock', next_action: '重新预览',
          summary: {}, stats: {}, operation_id: '', preview_id: '', created_at: '', expires_at: '',
          expires_in: 0, ttl_seconds: 0, local_sha256: '', context_fingerprint: '', plan_fingerprint: '',
          fingerprint: { local_sha256: '', context: '', plan: '' }, target_tables: {},
          target_date: '', test_mode: true, tables: [], blocked: [], warnings: [],
        })
      case 'wps_check_copies':
        return json(res, { ok: true, drifted: [], all_aligned: true, tables: [] })
      case 'save_order_config':
      case 'save_sss_config':
      case 'save_wps_config': {
        const key = name === 'save_order_config' ? 'saveOrder'
          : name === 'save_sss_config' ? 'saveSss' : 'saveWps'
        const payloadKey = `${key}Payloads`
        const activeKey = `${key}Active`
        const maxKey = `${key}MaxActive`
        const delayKey = `${key}Delay`
        const failKey = `${key}Fail`
        mock[payloadKey].push(firstArg || {})
        mock[activeKey] += 1
        mock[maxKey] = Math.max(mock[maxKey], mock[activeKey])
        const respond = () => {
          mock[activeKey] = Math.max(0, mock[activeKey] - 1)
          json(res, mock[failKey] ? { ok: false, reason: 'mock 保存失败' } : { ok: true })
        }
        if (mock[delayKey] > 0) setTimeout(respond, mock[delayKey])
        else respond()
        return
      }
      case 'frontend_report':
      case 'check_updates':
      case 'set_split_ratio':
      case 'echo_test':
        return json(res, { ok: true })
      default:
        return json(res, { ok: true })
    }
  })
}

async function freePort() {
  return new Promise((resolve, reject) => {
    const server = net.createServer()
    server.listen(0, '127.0.0.1', () => {
      const address = server.address()
      const port = typeof address === 'object' && address ? address.port : 0
      server.close(() => resolve(port))
    })
    server.on('error', reject)
  })
}

function findChrome() {
  const candidates = [process.env.CHROME, 'google-chrome', 'chromium', 'chromium-browser'].filter(Boolean)
  for (const candidate of candidates) {
    const found = spawnSync('bash', ['-lc', `command -v ${candidate}`], { encoding: 'utf8' })
    const bin = found.stdout.trim()
    if (found.status === 0 && bin) return bin
  }
  return ''
}

class Cdp {
  constructor(ws) { this.ws = ws; this.seq = 0; this.pending = new Map() }
  dialogCount = 0
  /** R8：CDP 的 Runtime/Log 域已启用（异常采集生效）之前不允许开始导航。 */
  pageErrorsSubscribed = false

  static async connect(port, baseUrl) {
    for (let i = 0; i < 60; i += 1) {
      try {
        const targets = await fetch(`http://127.0.0.1:${port}/json/list`).then((r) => r.json())
        const page = targets.find((t) => t.type === 'page' && t.url.startsWith(baseUrl))
        if (page) {
          const ws = new WebSocket(page.webSocketDebuggerUrl)
          await new Promise((resolve, reject) => { ws.onopen = resolve; ws.onerror = reject })
          const cdp = new Cdp(ws)
          ws.onmessage = (event) => {
            const msg = JSON.parse(String(event.data))
            if (msg.method === 'Page.javascriptDialogOpening') {
              cdp.dialogCount += 1
              void cdp.send('Page.handleJavaScriptDialog', { accept: true }).catch(() => {})
              return
            }
            // R8：页面异常采集（订阅在任何导航与业务脚本执行之前完成）。
            if (msg.method === 'Runtime.exceptionThrown') { collectPageException(msg.params); return }
            if (msg.method === 'Runtime.consoleAPICalled') { collectConsoleCall(msg.params); return }
            if (msg.method === 'Log.entryAdded') { collectLogEntry(msg.params); return }
            if (!msg.id || !cdp.pending.has(msg.id)) return
            const { resolve, reject } = cdp.pending.get(msg.id)
            cdp.pending.delete(msg.id)
            if (msg.error) reject(new Error(msg.error.message))
            else resolve(msg.result)
          }
          await cdp.send('Runtime.enable')
          await cdp.send('Page.enable')
          // Log 域负责浏览器级错误（资源加载失败、未捕获异常的浏览器日志副本）。
          await cdp.send('Log.enable')
          cdp.pageErrorsSubscribed = true
          return cdp
        }
      } catch { /* Chrome not ready yet */ }
      await new Promise((r) => setTimeout(r, 250))
    }
    throw new Error('Chrome DevTools target not found')
  }
  send(method, params = {}) {
    return new Promise((resolve, reject) => {
      const id = ++this.seq
      this.pending.set(id, { resolve, reject })
      this.ws.send(JSON.stringify({ id, method, params }))
    })
  }
  async eval(expression) {
    const result = await this.send('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true })
    if (result.exceptionDetails) throw new Error(result.exceptionDetails.text || 'evaluate failed')
    return result.result?.value
  }
  async waitFor(expression, timeout = 4000) {
    const start = Date.now()
    while (Date.now() - start < timeout) {
      try {
        if (await this.eval(expression)) return true
      } catch {
        // 页面 reload/导航期间 document.body 可能暂时不可用，继续轮询。
      }
      await new Promise((r) => setTimeout(r, 120))
    }
    return false
  }
  async viewport(width, height) {
    await this.send('Emulation.setDeviceMetricsOverride', { width, height, deviceScaleFactor: 1, mobile: true })
    await new Promise((r) => setTimeout(r, 250))
  }
}

function documentTextIncludes(text, needles) { return needles.every((needle) => String(text).includes(needle)) }

/** 断言失败时给出目标词附近的上下文，避免只知道 false 不知道为什么。 */
/** 读取结果卡片上的机器可读语义（不依赖中文措辞）。 */
const resultAttrsJs = `(() => {
  const el = document.querySelector('[data-wps-result="upload"]')
  if (!el) return null
  return {
    status: el.dataset.resultStatus,
    complete: el.dataset.resultComplete,
    rowsUnknown: el.dataset.rowsUnknown,
    verifiedRows: el.dataset.verifiedRows,
    provenNoWrite: el.dataset.provenNoWrite,
  }
})()`

/**
 * FE-1（R6-8）：权威操作状态的真实渲染面。
 *
 * 来源是 `lib/operationStatus.ts` 的 operationView。**批 2 之后它只渲染一处**：
 *   `<div class="mb-2 flex"><div><h1>任务工作台</h1><p>{detail}</p></div><StatusPill label/></div>`
 * （页签内那份标题=状态胶囊、正文=detail 的重复 Callout 已按用户确认的方案删除）。
 * 因此这里除了 label/detail，还给出：
 *   - `callouts`：仍然存在的「标题+正文」重复块（与 label+detail 精确相等），**必须为空**；
 *   - `detailClipped`：说明行是否被截断（横向省略号或纵向裁切），**必须为 false**，
 *     否则删掉页签 Callout 就等于把长失败原因一起藏掉。
 * 取值全部基于**可见 DOM 结构**（不依赖 class 名、不读源码字符串），
 * 所以「uncertain 被渲染成已完成」这类回归只能靠真实渲染结果抓到。
 */
const operationSurfaceJs = `(() => {
  const visible = (el) => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0 }
  const norm = (value) => String(value || '').replace(/\\s+/g, ' ').trim()
  const h1 = [...document.querySelectorAll('h1')].find((el) => (el.textContent || '').includes('任务工作台'))
  if (!h1) return null
  const row = h1.parentElement?.parentElement
  const pill = row ? row.lastElementChild : null
  const label = pill && visible(pill) ? norm(pill.textContent) : ''
  const detailEl = h1.nextElementSibling
  const detail = norm(detailEl?.textContent)
  // 与「页签内被删除的那块」逐字相同（标题+正文）才算重复块：
  // 用精确匹配而不是前缀匹配，避免误伤以同一状态文案开头的 WPS 恢复卡片等合法模块。
  const duplicateText = label + detail
  const callouts = duplicateText
    ? [...new Set([...document.querySelectorAll('div')]
      .filter((el) => norm(el.textContent) === duplicateText && visible(el))
      .map((el) => norm(el.textContent)))]
    : []
  return {
    label,
    detail,
    callouts,
    detailClipped: detailEl
      ? detailEl.scrollWidth > detailEl.clientWidth + 1 || detailEl.scrollHeight > detailEl.clientHeight + 1
      : null,
    surfaceText: [label, detail, ...callouts].join(' | '),
  }
})()`

/**
 * 批 2：任务工作台头部（唯一权威状态区）的真实渲染面。
 *
 * - `headerLabel` / `headerDetail`：头部状态胶囊与说明行的可见文本；
 * - `duplicateBlocks`：**与页签内被删除的那块逐字相同**的可见块
 *   （`label + detail` 精确相等）。用精确匹配而不是 `startsWith(label)`：
 *   WPS 恢复卡片等合法模块也可能以同一状态文案开头，按前缀判重会误伤。
 *   删掉页签内重复块之后这里必须为空 —— 这是「状态不再同屏重复」的机器判据；
 * - `detailOccurrences`：说明文本在整页出现几次（就绪态应为 1）；
 * - `detailClipped`：说明行是否被截断。横向（`truncate` 的省略号）与纵向
 *   （`line-clamp` 裁切）都查，才不会漏掉「失败原因看不全」；
 * - `authoritativeText`：头部状态胶囊 + 说明，用于「不得出现完成文案」的判定。
 */
const taskStatusSurfaceJs = `(() => {
  const norm = (value) => String(value || '').replace(/\\s+/g, ' ').trim()
  const visible = (el) => {
    if (!el) return false
    const r = el.getBoundingClientRect()
    const s = getComputedStyle(el)
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'
  }
  const h1 = [...document.querySelectorAll('h1')].find((el) => (el.textContent || '').includes('任务工作台'))
  if (!h1) return null
  const row = h1.parentElement?.parentElement
  const pill = row ? row.lastElementChild : null
  const label = pill && visible(pill) ? norm(pill.textContent) : ''
  const detailEl = h1.nextElementSibling
  const detail = norm(detailEl?.textContent)
  const duplicateText = label + detail
  const duplicateBlocks = duplicateText
    ? [...document.querySelectorAll('div')]
      .filter((el) => norm(el.textContent) === duplicateText && visible(el))
      .map((el) => norm(el.textContent).slice(0, 120))
    : []
  const bodyText = norm(document.body.innerText)
  const count = (haystack, needle) => (needle ? haystack.split(needle).length - 1 : 0)
  const clipped = detailEl
    ? detailEl.scrollWidth > detailEl.clientWidth + 1 || detailEl.scrollHeight > detailEl.clientHeight + 1
    : null
  return {
    headerLabel: label,
    headerDetail: detail,
    duplicateBlocks: [...new Set(duplicateBlocks)],
    detailOccurrences: count(bodyText, detail),
    detailClipped: clipped,
    detailHeight: detailEl ? Math.round(detailEl.getBoundingClientRect().height) : null,
    authoritativeText: label + ' | ' + detail,
    overflow: document.documentElement.scrollWidth > window.innerWidth,
    innerWidth: window.innerWidth,
    innerHeight: window.innerHeight,
  }
})()`

/** 当前可见的全部按钮（隐藏页签/未打开的弹层不计入），带 disabled 与主按钮标记。 */
const visibleButtonsJs = `(() => {
  const visible = (el) => {
    const r = el.getBoundingClientRect(); const s = getComputedStyle(el)
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'
  }
  return [...document.querySelectorAll('button')].filter(visible).map((el) => ({
    label: (el.textContent || '').replace(/\\s+/g, ' ').trim(),
    disabled: el.disabled === true,
    primary: el.classList.contains('btn-serif-primary'),
  }))
})()`

/** 底部主动作按钮（BottomDock 的 btn-serif-primary；同一时刻只有当前页签那一个可见）。 */
const primaryDockButtonJs = `(() => {
  const visible = (el) => {
    const r = el.getBoundingClientRect(); const s = getComputedStyle(el)
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden'
  }
  const nodes = [...document.querySelectorAll('button.btn-serif-primary')].filter(visible)
  if (nodes.length !== 1) return { count: nodes.length, label: '', disabled: false }
  const el = nodes[0]
  return { count: 1, label: (el.textContent || '').replace(/\\s+/g, ' ').trim(), disabled: el.disabled === true }
})()`

/**
 * 打开中的确认弹窗（真实可见）内容与按钮状态。
 *
 * 「可见」必须同时满足（R6-8 收尾要求：不能只看 boundingClientRect）：
 * 1. 自身与**全部祖先**都 display≠none、visibility≠hidden、opacity≠0、无 inert / aria-hidden；
 * 2. 自身 pointer-events≠none；
 * 3. 可交互性：弹窗中心点命中测试必须落在自己或自己的后代上。
 *
 * 手机端日志面板是常驻 DOM 的 `role="dialog"`（收起的时 visibility:hidden + inert，
 * 矩形仍是 390x844），只查 rect 会把它误判成“打开的确认弹窗”——这正是本判据要挡住的。
 */
const openDialogProbeJs = `(() => {
  const isReallyVisible = (el) => {
    const rect = el.getBoundingClientRect()
    if (rect.width <= 0 || rect.height <= 0) return false
    for (let node = el; node && node.nodeType === 1; node = node.parentElement) {
      const style = getComputedStyle(node)
      if (style.display === 'none' || style.visibility === 'hidden') return false
      if (Number(style.opacity) === 0) return false
      if (node.hasAttribute('inert')) return false
      if (node.getAttribute('aria-hidden') === 'true') return false
    }
    if (getComputedStyle(el).pointerEvents === 'none') return false
    const cx = Math.min(Math.max(rect.left + rect.width / 2, 1), Math.max(window.innerWidth - 1, 1))
    const cy = Math.min(Math.max(rect.top + rect.height / 2, 1), Math.max(window.innerHeight - 1, 1))
    const hit = document.elementFromPoint(cx, cy)
    return Boolean(hit && (hit === el || el.contains(hit)))
  }
  const roots = [...document.querySelectorAll('[role="dialog"],[role="alertdialog"]')].filter(isReallyVisible)
  if (roots.length === 0) return null
  return {
    count: roots.length,
    text: (roots[0].innerText || '').replace(/\\s+/g, ' ').trim(),
    buttons: roots.flatMap((root) => [...root.querySelectorAll('button')].map((el) => ({
      label: (el.textContent || '').replace(/\\s+/g, ' ').trim(),
      disabled: el.disabled === true,
    }))),
  }
})()`

/** 流程条上「结果」步骤：uncertain 时必须是待核对步骤，而不是打勾的完成步骤。 */
const flowResultStepJs = `(() => {
  const strip = [...document.querySelectorAll('ol[aria-label]')]
    .find((el) => (el.textContent || '').includes('结果') && el.getBoundingClientRect().width > 0)
  if (!strip) return null
  const item = [...strip.querySelectorAll('li')].find((el) => (el.textContent || '').includes('结果'))
  const badge = item ? item.querySelector('span') : null
  return {
    label: strip.getAttribute('aria-label'),
    step: (item?.textContent || '').trim(),
    badgeText: (badge?.textContent || '').trim(),
    hasCheckIcon: Boolean(badge?.querySelector('svg')),
  }
})()`

/**
 * 手机端日志开合按钮 = 右上角悬浮按钮（`data-log-fab`，见 components/LogFab.tsx）。
 *
 * 用稳定钩子而不是 aria-label：按钮文案会随开合在「展开运行日志/收起运行日志」
 * 之间变化，当锚点会在收起后失效。点击走真实 `el.click()`（React onClick）。
 */
const logFabClickJs = `(() => {
  const el = document.querySelector('[data-log-fab]')
  if (!el) return false
  el.click()
  return true
})()`

/** 悬浮按钮的真实状态：几何、层级、可点性、是否在可视视口内。 */
const logFabProbeJs = `(() => {
  const el = document.querySelector('[data-log-fab]')
  if (!el) return null
  const r = el.getBoundingClientRect()
  const s = getComputedStyle(el)
  const cx = Math.min(Math.max(r.left + r.width / 2, 1), Math.max(window.innerWidth - 1, 1))
  const cy = Math.min(Math.max(r.top + r.height / 2, 1), Math.max(window.innerHeight - 1, 1))
  const hit = document.elementFromPoint(cx, cy)
  return {
    count: document.querySelectorAll('[data-log-fab]').length,
    label: el.getAttribute('aria-label'),
    expanded: el.getAttribute('aria-expanded'),
    controls: el.getAttribute('aria-controls'),
    rect: { left: r.left, top: r.top, right: r.right, bottom: r.bottom, width: r.width, height: r.height },
    center: { x: r.left + r.width / 2, y: r.top + r.height / 2 },
    visible: r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none' && Number(s.opacity) > 0,
    pointerEvents: s.pointerEvents,
    zIndex: Number(s.zIndex),
    hitSelf: Boolean(hit && (hit === el || el.contains(hit))),
    inViewport: r.left >= -1 && r.top >= -1 && r.right <= window.innerWidth + 1 && r.bottom <= window.innerHeight + 1,
    innerWidth: window.innerWidth,
    innerHeight: window.innerHeight,
  }
})()`

/**
 * 手机端日志面板（`#phone-log-sheet`）的真实状态。
 *
 * 面板常驻 DOM，只查 rect 会把收起的它当成「打开的弹窗」；这里同时给出
 * 裁剪值、可见性、inert/aria、中心命中测试与按钮层级，用于判定
 * 「展开/收起/动画中/快速开关后」到底停在哪一态。
 */
const logSheetProbeJs = `(() => {
  const el = document.getElementById('phone-log-sheet')
  if (!el) return null
  const s = getComputedStyle(el)
  const r = el.getBoundingClientRect()
  const cx = Math.min(Math.max(r.left + r.width / 2, 1), Math.max(window.innerWidth - 1, 1))
  const cy = Math.min(Math.max(r.top + r.height / 2, 1), Math.max(window.innerHeight - 1, 1))
  const hit = document.elementFromPoint(cx, cy)
  const fab = document.querySelector('[data-log-fab]')
  const clip = s.clipPath || ''
  const radius = clip.match(/^circle\\(([\\d.]+)px/)
  return {
    specifiedClip: el.style.clipPath || '',
    computedClip: clip,
    radiusPx: radius ? Number(radius[1]) : null,
    visibility: s.visibility,
    pointerEvents: s.pointerEvents,
    display: s.display,
    inert: el.hasAttribute('inert'),
    ariaHidden: el.getAttribute('aria-hidden'),
    ariaModal: el.getAttribute('aria-modal'),
    zIndex: Number(s.zIndex),
    fabZIndex: fab ? Number(getComputedStyle(fab).zIndex) : null,
    coversCenter: Boolean(hit && (hit === el || el.contains(hit))),
    centerHitTag: hit ? (hit.tagName || '') + (hit.id ? '#' + hit.id : '') : null,
    filterValue: el.querySelector('input[aria-label="搜索运行日志"]')?.value ?? null,
    searchOpen: Boolean(el.querySelector('[data-log-search="true"]')),
    toolsOpen: Boolean(document.querySelector('[data-log-tools="true"][data-state="open"]')),
    text: (el.innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 120),
  }
})()`

/**
 * 日志正文的真实渲染面：逐行的折叠/展开/命中状态。
 *
 * 「重复展示」必须看**真实渲染出来的行**，不能只看数据 ——
 * 旧实现的缺陷正是「摘要 + 追加全文」，数据没问题、界面出现两份。
 */
const logRowsProbeJs = `(() => {
  const paper = document.querySelector('#phone-log-sheet .receipt-paper')
    || document.querySelector('.receipt-paper')
  if (!paper) return null
  const rows = [...paper.querySelectorAll('[data-log-row="true"]')].map((row) => {
    const lineNodes = [...row.querySelectorAll('[data-log-line]')]
    return {
      level: row.querySelector('[data-log-level-badge]')?.dataset.logLevelBadge ?? '',
      hit: row.dataset.logHit === 'true',
      expandable: row.dataset.logExpandable === 'true',
      full: row.dataset.logFull === 'true',
      lines: lineNodes.map((line) => (line.textContent || '').replace(/\\u00a0/g, '').trim()),
      highlighted: lineNodes.filter((line) => String(line.className).includes('bg-hit')).length,
      toggle: row.querySelector('[data-log-toggle]')?.dataset.logToggle ?? null,
      forced: Boolean(row.querySelector('[data-log-forced]')),
      text: (row.innerText || '').replace(/\\s+/g, ' ').trim(),
    }
  })
  const text = (paper.innerText || '').replace(/\\s+/g, ' ').trim()
  return {
    count: rows.length,
    rows,
    slogan: text.includes('一单一味'),
    emptyHint: text.includes('当前筛选无匹配日志'),
    text: text.slice(0, 300),
  }
})()`

/** 按可见文本定位一行日志的下标（找不到返回 -1）。 */
const logRowIndexJs = (needle) => `(() => {
  const paper = document.querySelector('#phone-log-sheet .receipt-paper')
    || document.querySelector('.receipt-paper')
  if (!paper) return -1
  const rows = [...paper.querySelectorAll('[data-log-row="true"]')]
  return rows.findIndex((row) => (row.innerText || '').includes(${JSON.stringify(needle)}))
})()`

/** 点击第 index 行的「展开/收起明细」按钮。 */
const clickLogToggleJs = (index) => `(() => {
  const paper = document.querySelector('#phone-log-sheet .receipt-paper')
    || document.querySelector('.receipt-paper')
  const row = [...(paper?.querySelectorAll('[data-log-row="true"]') || [])][${index}]
  const button = row?.querySelector('[data-log-toggle]')
  if (!button) return false
  button.click()
  return true
})()`

/**
 * 真实点击一个选择器命中的元素。
 *
 * Radix 的下拉菜单靠 `pointerdown` 打开、`click` 选中 —— 只调 `el.click()`
 * 打不开菜单，必须补上完整的指针序列。
 */
const clickSelectorJs = (selector) => `(() => {
  const target = document.querySelector(${JSON.stringify(selector)})
  if (!target) return false
  const r = target.getBoundingClientRect()
  if (!(r.width > 0 && r.height > 0)) return false
  target.focus?.()
  try {
    target.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true, cancelable: true, button: 0, pointerId: 1, pointerType: 'mouse' }))
    target.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true, button: 0 }))
  } catch { /* older engines */ }
  target.click()
  try {
    target.dispatchEvent(new PointerEvent('pointerup', { bubbles: true, cancelable: true, button: 0, pointerId: 1, pointerType: 'mouse' }))
    target.dispatchEvent(new MouseEvent('mouseup', { bubbles: true, cancelable: true, button: 0 }))
  } catch { /* older engines */ }
  return true
})()`

/**
 * 水波圆心（真实布局解析）：从面板**当前的 clip-path 表达式**里取出圆心，
 * 交给浏览器自己的布局引擎解析成像素，再与悬浮按钮中心比较。
 *
 * 为什么不直接读 `getComputedStyle().clipPath`：Chrome 对 `at` 里的
 * `calc(100% - …)` 只解析一部分，x 会保留表达式原文；这里用两个探针元素
 * （width=x 表达式 / height=y 表达式）让布局引擎算出真实像素值。
 */
const logRevealOriginJs = `(() => {
  const splitTopLevel = (value) => {
    const out = []
    let depth = 0
    let current = ''
    for (const ch of value) {
      if (ch === '(') depth += 1
      if (ch === ')') depth -= 1
      if (ch === ' ' && depth === 0) {
        if (current) out.push(current)
        current = ''
        continue
      }
      current += ch
    }
    if (current) out.push(current)
    return out
  }
  const sheet = document.getElementById('phone-log-sheet')
  const fab = document.querySelector('[data-log-fab]')
  if (!sheet || !fab) return null
  const specified = sheet.style.clipPath || getComputedStyle(sheet).clipPath || ''
  const matched = specified.match(/^circle\\([^)]*? at (.+)\\)$/)
  if (!matched) return { ok: false, reason: 'no_circle_origin', specified }
  const parts = splitTopLevel(matched[1])
  if (parts.length !== 2) return { ok: false, reason: 'origin_parts', specified, parts }
  const probe = document.createElement('div')
  probe.setAttribute('data-reveal-probe', 'true')
  probe.style.cssText = 'position:fixed;left:0;top:0;pointer-events:none;visibility:hidden'
  document.body.appendChild(probe)
  probe.style.width = parts[0]
  probe.style.height = '0px'
  const x = probe.getBoundingClientRect().width
  probe.style.width = '0px'
  probe.style.height = parts[1]
  const y = probe.getBoundingClientRect().height
  probe.remove()
  const fr = fab.getBoundingClientRect()
  const fabCenter = { x: fr.left + fr.width / 2, y: fr.top + fr.height / 2 }
  return {
    ok: true,
    specified,
    origin: { x, y },
    fabCenter,
    dx: Math.abs(x - fabCenter.x),
    dy: Math.abs(y - fabCenter.y),
  }
})()`

/**
 * 悬浮按钮与「标题栏主题切换 / 日志头部过滤控件」是否重叠（真实矩形 + 命中测试）。
 * 按钮是 fixed、不占布局，只靠肉眼看截图容易漏掉「压住控件」这类问题。
 */
const logFabClearanceJs = `(() => {
  const fab = document.querySelector('[data-log-fab]')
  if (!fab) return null
  const fr = fab.getBoundingClientRect()
  const overlaps = (r) => !(fr.right <= r.left || fr.left >= r.right || fr.bottom <= r.top || fr.top >= r.bottom)
  const inspect = (el) => {
    if (!el) return { found: false }
    const r = el.getBoundingClientRect()
    const cx = Math.min(Math.max(r.left + r.width / 2, 1), Math.max(window.innerWidth - 1, 1))
    const cy = Math.min(Math.max(r.top + r.height / 2, 1), Math.max(window.innerHeight - 1, 1))
    const hit = document.elementFromPoint(cx, cy)
    return {
      found: true,
      rect: { left: r.left, top: r.top, right: r.right, bottom: r.bottom },
      overlap: overlaps(r),
      hitSelf: Boolean(hit && (hit === el || el.contains(hit))),
    }
  }
  const theme = [...document.querySelectorAll('header button')]
    .find((b) => /主题/.test(b.getAttribute('aria-label') || ''))
  const sheet = document.getElementById('phone-log-sheet')
  // 常驻头部控件：搜索图标入口（点开后换成输入框）、工具菜单入口、任务状态。
  // 级别筛选/自动滚动/复制/清理都在工具菜单里（portal 到 body，不占头部）。
  const searchOpen = sheet?.querySelector('[data-log-search-open]') || null
  const searchInput = sheet?.querySelector('[data-log-search]') || null
  const tools = sheet?.querySelector('[data-log-tools]') || null
  const status = sheet?.querySelector('[data-log-status]') || null
  return {
    fab: { left: fr.left, top: fr.top, right: fr.right, bottom: fr.bottom },
    theme: inspect(theme),
    search: inspect(searchOpen || searchInput),
    searchIsInput: Boolean(searchInput),
    tools: inspect(tools),
    status: inspect(status),
    safeRight: getComputedStyle(document.documentElement).getPropertyValue('--safe-right').trim(),
    innerWidth: window.innerWidth,
  }
})()`

/** 底部「停止」按钮的真实状态（可见 + disabled）。 */
const stopButtonJs = `(() => {
  const visible = (el) => {
    const r = el.getBoundingClientRect(); const s = getComputedStyle(el)
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'
  }
  const el = [...document.querySelectorAll('button')].filter(visible)
    .find((node) => (node.textContent || '').trim() === '停止')
  if (!el) return { found: false, disabled: false }
  return { found: true, disabled: el.disabled === true }
})()`

/**
 * 提示条（sonner toast）的真实矩形/文本 + 日志按钮中心的命中结果。
 *
 * 判「提示条有没有压住日志按钮」不能只看 z-index：必须同时给出**矩形是否相交**与
 * `elementFromPoint(按钮中心)` 命中谁；真实鼠标点击（`realClickAt`）再补一刀 ——
 * 被盖住时点不到，那才是用户真正的感受。
 */
const toastProbeJs = `(() => {
  const fab = document.querySelector('[data-log-fab]')
  const fr = fab ? fab.getBoundingClientRect() : null
  const fabRect = fr ? { left: fr.left, top: fr.top, right: fr.right, bottom: fr.bottom } : null
  const cx = fr ? Math.min(Math.max(fr.left + fr.width / 2, 1), Math.max(window.innerWidth - 1, 1)) : 0
  const cy = fr ? Math.min(Math.max(fr.top + fr.height / 2, 1), Math.max(window.innerHeight - 1, 1)) : 0
  const hit = fr ? document.elementFromPoint(cx, cy) : null
  const overlaps = (a, b) => Boolean(a && b) && !(a.right <= b.left || a.left >= b.right || a.bottom <= b.top || a.top >= b.bottom)
  const toasts = [...document.querySelectorAll('[data-sonner-toaster] [data-sonner-toast]')]
    .filter((el) => {
      const r = el.getBoundingClientRect()
      const s = getComputedStyle(el)
      return r.width > 0 && r.height > 0 && s.visibility !== 'hidden'
        && Number(s.opacity) > 0 && el.getAttribute('data-visible') !== 'false'
        && el.getAttribute('data-removed') !== 'true'
    })
    .map((el) => {
      const r = el.getBoundingClientRect()
      return {
        type: el.getAttribute('data-type') || '',
        text: (el.innerText || '').replace(/\\s+/g, ' ').trim(),
        rect: { left: r.left, top: r.top, right: r.right, bottom: r.bottom, width: r.width, height: r.height },
      }
    })
  return {
    count: toasts.length,
    toasts,
    fab: fabRect,
    fabCenter: fr ? { x: cx, y: cy } : null,
    fabHitIsFab: Boolean(hit && fab && (hit === fab || fab.contains(hit))),
    fabHitDesc: hit ? (hit.tagName || '') + (hit.getAttribute && hit.getAttribute('data-sonner-toast') !== null ? '[toast]' : '') : null,
    overlapsFab: toasts.some((item) => overlaps(item.rect, fabRect)),
    innerWidth: window.innerWidth,
    innerHeight: window.innerHeight,
  }
})()`

/** 日志头部（标题行/搜索/工具）与底栏主动作的真实矩形 + 中心命中结果。 */
const logChromeProbeJs = `(() => {
  const rectOf = (el) => {
    if (!el) return null
    const r = el.getBoundingClientRect()
    return { left: r.left, top: r.top, right: r.right, bottom: r.bottom, width: r.width, height: r.height }
  }
  const hitOf = (el) => {
    if (!el) return null
    const r = el.getBoundingClientRect()
    const x = Math.min(Math.max(r.left + r.width / 2, 1), Math.max(window.innerWidth - 1, 1))
    const y = Math.min(Math.max(r.top + r.height / 2, 1), Math.max(window.innerHeight - 1, 1))
    const hit = document.elementFromPoint(x, y)
    return { hitSelf: Boolean(hit && (hit === el || el.contains(hit))), tag: hit ? hit.tagName : null }
  }
  const sheet = document.getElementById('phone-log-sheet')
  const search = sheet?.querySelector('[data-log-search]') || sheet?.querySelector('[data-log-search-open]') || null
  const tools = sheet?.querySelector('[data-log-tools]') || null
  const headerRow = sheet?.querySelector('h2')?.parentElement || null
  const stop = [...document.querySelectorAll('button')].find((b) => (b.textContent || '').trim() === '停止') || null
  const primary = [...document.querySelectorAll('button')].find((b) => /开始处理|已有操作进行中/.test(b.textContent || '')) || null
  return {
    headerRow: rectOf(headerRow),
    search: rectOf(search),
    tools: rectOf(tools),
    stop: rectOf(stop),
    primary: rectOf(primary),
    searchHit: hitOf(search),
    toolsHit: hitOf(tools),
    stopHit: hitOf(stop),
    primaryHit: hitOf(primary),
    sheetOpen: Boolean(sheet) && !sheet.hasAttribute('inert'),
  }
})()`

/**
 * 打桩平台剪贴板（真机差异正在这里，界面行为才是被测对象）：
 * - `ok`：`navigator.clipboard.writeText` 成功 → 应用走成功分支弹成功提示；
 * - `fail`：Clipboard API 抛错 **且** `document.execCommand('copy')` 返回 false
 *   → 应用走兜底失败分支弹错误提示（两条路径都失败，不依赖 headless 的剪贴板状态）。
 */
const stubClipboardJs = (mode) => `(() => {
  window.__clipMode = ${JSON.stringify(mode)}
  window.__copiedText = null
  if (!window.__origExecCommand) window.__origExecCommand = document.execCommand
  Object.defineProperty(navigator, 'clipboard', {
    configurable: true,
    value: {
      writeText: async (text) => {
        if (window.__clipMode === 'fail') throw new Error('mock: 剪贴板权限被拒')
        window.__copiedText = text
      },
    },
  })
  document.execCommand = () => window.__clipMode !== 'fail'
  return true
})()`

const restoreClipboardJs = `(() => {
  try { delete navigator.clipboard } catch { /* ignore */ }
  if (window.__origExecCommand) document.execCommand = window.__origExecCommand
  return true
})()`

/**
 * 手机端提示区域（占正常布局空间）的真实几何 + 关键内容的位置。
 *
 * ISSUE-05 的判据不是「提示条偏移多少像素」，而是「提示区域有没有把关键内容挤下去、
 * 有没有盖住它们」：所以同时给出区域矩形、`--phone-notice-*` 变量、标题栏/主内容
 * 的相对位置、每条提示的矩形、日志按钮中心命中谁。
 */
const noticeRegionProbeJs = `(() => {
  const rectOf = (el) => {
    if (!el) return null
    const r = el.getBoundingClientRect()
    return { left: r.left, top: r.top, right: r.right, bottom: r.bottom, width: r.width, height: r.height }
  }
  const region = document.querySelector('[data-phone-notice-region]')
  const rect = rectOf(region)
  const toasts = [...document.querySelectorAll('[data-sonner-toast]')]
    .filter((el) => {
      const r = el.getBoundingClientRect()
      const s = getComputedStyle(el)
      return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && Number(s.opacity) > 0
    })
    .map((el) => ({ text: (el.innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 60), rect: rectOf(el) }))
  const fab = document.querySelector('[data-log-fab]')
  const fr = fab ? fab.getBoundingClientRect() : null
  const fx = fr ? Math.min(Math.max(fr.left + fr.width / 2, 1), Math.max(window.innerWidth - 1, 1)) : 0
  const fy = fr ? Math.min(Math.max(fr.top + fr.height / 2, 1), Math.max(window.innerHeight - 1, 1)) : 0
  const fabHit = fr ? document.elementFromPoint(fx, fy) : null
  const header = document.querySelector('header')
  const main = document.querySelector('main')
  const rootStyle = getComputedStyle(region?.parentElement || document.documentElement)
  return {
    exists: Boolean(region),
    rect,
    height: rect ? Math.round(rect.height * 100) / 100 : null,
    maxHeightPx: region ? Number.parseFloat(getComputedStyle(region).maxHeight) || 0 : 0,
    scrollable: region ? region.scrollHeight > region.clientHeight + 1 : false,
    noticeHeightVar: rootStyle.getPropertyValue('--phone-notice-height').trim(),
    noticeBottomVar: rootStyle.getPropertyValue('--phone-notice-bottom').trim(),
    headerTop: header ? Math.round(header.getBoundingClientRect().top) : null,
    headerBottom: header ? Math.round(header.getBoundingClientRect().bottom) : null,
    mainTop: main ? Math.round(main.getBoundingClientRect().top) : null,
    toasts,
    fab: fr ? { left: fr.left, top: fr.top, right: fr.right, bottom: fr.bottom } : null,
    fabHitIsFab: Boolean(fabHit && fab && (fabHit === fab || fab.contains(fabHit))),
    fabHitDesc: fabHit ? (fabHit.tagName || '') : null,
    innerHeight: window.innerHeight,
    innerWidth: window.innerWidth,
  }
})()`

/** 标题栏状态行 / 失败原因横幅 / 待处理输入 / 底栏动作 / 弹窗的位置与可点性。 */
const phoneChromeProbeJs = `(() => {
  const rectOf = (el) => {
    if (!el) return null
    const r = el.getBoundingClientRect()
    return { left: r.left, top: r.top, right: r.right, bottom: r.bottom, width: r.width, height: r.height }
  }
  const hitOf = (el) => {
    if (!el) return null
    const r = el.getBoundingClientRect()
    const x = Math.min(Math.max(r.left + r.width / 2, 1), Math.max(window.innerWidth - 1, 1))
    const y = Math.min(Math.max(r.top + r.height / 2, 1), Math.max(window.innerHeight - 1, 1))
    const hit = document.elementFromPoint(x, y)
    return { hitSelf: Boolean(hit && (hit === el || el.contains(hit))), tag: hit ? hit.tagName : null }
  }
  const visible = (el) => {
    if (!el) return false
    const r = el.getBoundingClientRect()
    const s = getComputedStyle(el)
    return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden'
  }
  // 权威状态区（批 2 之后只渲染一处）：任务工作台头部那一行 + 右侧状态胶囊。
  const h1 = [...document.querySelectorAll('h1')].find((el) => (el.textContent || '').includes('任务工作台'))
  const statusRow = h1 ? h1.parentElement?.parentElement : null
  const statusPill = statusRow ? statusRow.lastElementChild : null
  const statusDetail = h1 ? h1.nextElementSibling : null
  const banner = [...document.querySelectorAll('[role="alert"],[role="status"]')].find(visible) || null
  const pendingInput = [...document.querySelectorAll('input[placeholder^="输入最终地址"]')].find(visible) || null
  const buttons = [...document.querySelectorAll('button')]
  const primary = buttons.find((b) => /开始处理|已有操作进行中/.test(b.textContent || '')) || null
  const stop = buttons.find((b) => (b.textContent || '').trim() === '停止') || null
  return {
    status: {
      found: Boolean(statusRow),
      text: statusRow ? (statusRow.textContent || '').replace(/\\s+/g, ' ').trim().slice(0, 90) : '',
      detail: statusDetail ? (statusDetail.textContent || '').replace(/\\s+/g, ' ').trim().slice(0, 90) : '',
      rect: rectOf(statusRow),
      pillRect: rectOf(statusPill),
      hitSelf: hitOf(statusPill)?.hitSelf === true,
    },
    banner: { found: Boolean(banner), text: banner ? (banner.textContent || '').replace(/\\s+/g, ' ').trim().slice(0, 90) : '', rect: rectOf(banner), hitSelf: hitOf(banner)?.hitSelf === true },
    pendingInput: { found: Boolean(pendingInput), rect: rectOf(pendingInput), hitSelf: hitOf(pendingInput)?.hitSelf === true },
    primary: { rect: rectOf(primary), hitSelf: hitOf(primary)?.hitSelf === true, disabled: primary ? primary.disabled === true : null, hitTag: hitOf(primary)?.tag ?? null },
    stop: { rect: rectOf(stop), hitSelf: hitOf(stop)?.hitSelf === true, disabled: stop ? stop.disabled === true : null, hitTag: hitOf(stop)?.tag ?? null },
    sheetOpen: (() => { const sheet = document.getElementById('phone-log-sheet'); return Boolean(sheet) && !sheet.hasAttribute('inert') })(),
    sheetInert: (() => { const sheet = document.getElementById('phone-log-sheet'); return sheet ? sheet.hasAttribute('inert') : null })(),
    innerHeight: window.innerHeight,
    innerWidth: window.innerWidth,
  }
})()`

/** 可见模态弹窗的几何 + 每个按钮中心是否可命中（用于「提示不遮确认弹窗」）。 */
const dialogGeomJs = `(() => {
  const roots = [...document.querySelectorAll('[role="dialog"],[role="alertdialog"]')].filter((el) => {
    const r = el.getBoundingClientRect()
    const s = getComputedStyle(el)
    return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden' && !el.hasAttribute('inert')
  })
  if (roots.length === 0) return null
  const el = roots[0]
  const r = el.getBoundingClientRect()
  const buttons = [...el.querySelectorAll('button')].map((b) => {
    const br = b.getBoundingClientRect()
    const x = Math.min(Math.max(br.left + br.width / 2, 1), Math.max(window.innerWidth - 1, 1))
    const y = Math.min(Math.max(br.top + br.height / 2, 1), Math.max(window.innerHeight - 1, 1))
    const hit = document.elementFromPoint(x, y)
    return {
      label: (b.textContent || '').replace(/\\s+/g, ' ').trim(),
      disabled: b.disabled === true,
      hitSelf: Boolean(hit && (hit === b || b.contains(hit))),
    }
  })
  return {
    count: roots.length,
    rect: { left: r.left, top: r.top, right: r.right, bottom: r.bottom },
    text: (el.innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 80),
    buttons,
  }
})()`

function nearText(text, needle, span = 160) {
  const source = String(text || '')
  const at = source.indexOf(needle)
  if (at < 0) return `«未出现 ${needle}»`
  return source.slice(Math.max(0, at - span), at + span).replace(/\s+/g, ' ')
}

const clickByTextJs = (selector, text) => `(() => {
  const nodes = [...document.querySelectorAll(${JSON.stringify(selector)})].filter((el) => {
    const r = el.getBoundingClientRect(); const s = getComputedStyle(el)
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'
  })
  const target = nodes.find((el) => (el.textContent || '').replace(/\\s+/g, '').includes(${JSON.stringify(text)}))
  if (!target) return false
  target.focus?.()
  try {
    target.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true, cancelable: true, button: 0, pointerId: 1, pointerType: 'mouse' }))
    target.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true, button: 0 }))
  } catch { /* older engines */ }
  target.click()
  try {
    target.dispatchEvent(new PointerEvent('pointerup', { bubbles: true, cancelable: true, button: 0, pointerId: 1, pointerType: 'mouse' }))
    target.dispatchEvent(new MouseEvent('mouseup', { bubbles: true, cancelable: true, button: 0 }))
  } catch { /* older engines */ }
  return true
})()`

const setInputJs = (selector, value) => `(() => {
  const el = document.querySelector(${JSON.stringify(selector)})
  if (!el) return { ok: false, reason: 'not_found' }
  const proto = Object.getPrototypeOf(el)
  const desc = Object.getOwnPropertyDescriptor(proto, 'value')
    || Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')
  if (!desc || typeof desc.set !== 'function') return { ok: false, reason: 'no_setter' }
  desc.set.call(el, ${JSON.stringify(value)})
  el.dispatchEvent(new Event('input', { bubbles: true }))
  el.dispatchEvent(new Event('change', { bubbles: true }))
  const r = el.getBoundingClientRect()
  return { ok: el.value === ${JSON.stringify(value)} && r.width > 0 && r.height > 0, value: el.value, visible: r.width > 0 && r.height > 0 }
})()`

async function waitForMock(predicate, timeout = 4000) {
  const start = Date.now()
  while (Date.now() - start < timeout) {
    if (predicate()) return true
    await new Promise((resolve) => setTimeout(resolve, 60))
  }
  return false
}

/** 在 [role=dialog] / [role=alertdialog] 内点击文本匹配的按钮。 */
const dialogSelector = `(document.querySelector('[role="dialog"]') || document.querySelector('[role="alertdialog"]'))`
const clickDialogButtonJs = (text) => `(() => {
  const roots = [...document.querySelectorAll('[role="dialog"],[role="alertdialog"]')]
  const diag = roots.map((root) => {
    const r = root.getBoundingClientRect()
    return {
      w: Math.round(r.width), h: Math.round(r.height),
      buttons: [...root.querySelectorAll('button')].map((b) => (b.textContent || '').trim().slice(0, 14) + (b.disabled ? '(disabled)' : '')),
    }
  })
  for (const root of roots) {
    const r = root.getBoundingClientRect()
    if (r.width === 0 || r.height === 0) continue
    const nodes = [...root.querySelectorAll('button')].filter((el) => {
      const br = el.getBoundingClientRect(); const s = getComputedStyle(el)
      return br.width > 0 && br.height > 0 && s.visibility !== 'hidden' && !el.disabled
    })
    const target = nodes.find((el) => (el.textContent || '').replace(/\\s+/g, '').includes(${JSON.stringify(text)}))
    if (target) { target.click(); return { ok: true, diag } }
  }
  return { ok: false, diag }
})()`

/** 在弹窗内对文本匹配的按钮做 n 次同步连点（模拟 React 重渲染前的连点/双击）。 */
const burstDialogButtonJs = (text, times = 3) => `(() => {
  const roots = [...document.querySelectorAll('[role="dialog"],[role="alertdialog"]')]
  for (const root of roots) {
    const r = root.getBoundingClientRect()
    if (r.width === 0 || r.height === 0) continue
    const target = [...root.querySelectorAll('button')].find((el) => {
      const br = el.getBoundingClientRect(); const s = getComputedStyle(el)
      return br.width > 0 && br.height > 0 && s.visibility !== 'hidden'
        && (el.textContent || '').replace(/\\s+/g, '').includes(${JSON.stringify(text)})
    })
    if (!target) continue
    let fired = 0
    for (let i = 0; i < ${times}; i += 1) {
      if (target.disabled) break
      target.click()
      fired += 1
    }
    return { ok: true, fired }
  }
  return { ok: false, fired: 0 }
})()`

/** 勾选弹窗里的第一个 checkbox（明确确认句 / 结构核对）。 */
const clickDialogCheckboxJs = `(() => {
  const roots = [...document.querySelectorAll('[role="dialog"],[role="alertdialog"]')]
  for (const root of roots) {
    const box = root.querySelector('input[type="checkbox"]')
    if (!box) continue
    if (!box.checked) box.click()
    return box.checked === true
  }
  const anyBox = [...document.querySelectorAll('input[type="checkbox"]')].find((el) => {
    const r = el.getBoundingClientRect(); const s = getComputedStyle(el)
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden'
  })
  if (!anyBox) return false
  if (!anyBox.checked) anyBox.click()
  return anyBox.checked === true
})()`

const clickDialogRadioJs = (value) => `(() => {
  const dialog = ${dialogSelector}
  if (!dialog) return false
  const boxes = [...dialog.querySelectorAll('input[type="radio"]')]
  const target = boxes.find((el) => el.closest('label')?.textContent?.includes(${JSON.stringify(value)}))
  if (!target) return false
  target.click()
  return target.checked === true
})()`

const setDialogInputJs = (selector, value) => `(() => {
  const dialog = ${dialogSelector}
  if (!dialog) return { ok: false, reason: 'no_dialog' }
  const el = dialog.querySelector(${JSON.stringify(selector)})
  if (!el) return { ok: false, reason: 'not_found' }
  const proto = Object.getPrototypeOf(el)
  const desc = Object.getOwnPropertyDescriptor(proto, 'value')
    || Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value')
  if (!desc || typeof desc.set !== 'function') return { ok: false, reason: 'no_setter' }
  desc.set.call(el, ${JSON.stringify(value)})
  el.dispatchEvent(new Event('input', { bubbles: true }))
  el.dispatchEvent(new Event('change', { bubbles: true }))
  return { ok: el.value === ${JSON.stringify(value)} }
})()`

/** 点击页面可见按钮（不限于弹窗）。 */
const clickButtonJs = (text) => clickByTextJs('button', text)

/** 可靠重载：先埋一个旧文档标记，等新文档里该标记消失，避免在旧文档上误判。 */
/**
 * CDP 真实鼠标点击：走浏览器**命中测试**，被别的元素盖住就点不到。
 *
 * 与 `el.click()` 的区别正是本节要测的东西 —— 只调 `el.click()` 或只查 z-index
 * 都会漏掉「提示条压住日志按钮」这类问题。
 */
async function realClickAt(cdp, x, y) {
  const point = { x: Math.round(x), y: Math.round(y) }
  await cdp.send('Input.dispatchMouseEvent', { type: 'mouseMoved', ...point, button: 'none', buttons: 0 })
  await cdp.send('Input.dispatchMouseEvent', { type: 'mousePressed', ...point, button: 'left', buttons: 1, clickCount: 1 })
  await cdp.send('Input.dispatchMouseEvent', { type: 'mouseReleased', ...point, button: 'left', buttons: 0, clickCount: 1 })
  await new Promise((r) => setTimeout(r, 150))
}

async function reloadApp(cdp) {
  try { await cdp.eval('window.__reloadToken = "old-document"') } catch { /* ignore */ }
  await cdp.send('Page.reload', { ignoreCache: true })
  await cdp.waitFor('window.__reloadToken === undefined', 10_000)
  await cdp.waitFor(`document.body && document.body.innerText.includes('任务工作台')`, 10_000)
  await new Promise((r) => setTimeout(r, 300))
}

/** 切到云文档页签并等到指定标记文本出现（最多重试 8 次）。 */
async function ensureCloudPanel(cdp, marker) {
  for (let i = 0; i < 8; i += 1) {
    await cdp.eval(clickByTextJs('button', '云文档同步'))
    await new Promise((r) => setTimeout(r, 250))
    if (!marker) return true
    if (await cdp.eval(`document.body.innerText.includes(${JSON.stringify(marker)})`)) return true
  }
  return false
}

async function main() {
  assert.ok(fs.existsSync(path.join(DIST, 'index.html')), 'frontend/dist/index.html 不存在，请先 pnpm build')
  const chrome = findChrome()
  if (!chrome) {
    console.error('BLOCKED: 未找到本机 Chrome/Chromium；未安装依赖。需要人工真机/浏览器验证。')
    process.exit(2)
  }
  const apiPort = await freePort()
  const debugPort = await freePort()
  mock.deadPort = await freePort()
  const baseUrl = `http://127.0.0.1:${apiPort}/`
  const server = createMockServer()
  await new Promise((resolve) => server.listen(apiPort, '127.0.0.1', resolve))
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'yikou-fe-check-'))
  const chromeProc = spawn(chrome, [
    '--headless=new', '--disable-gpu', '--no-sandbox', '--disable-dev-shm-usage',
    '--no-first-run', '--hide-scrollbars', `--remote-debugging-port=${debugPort}`,
    `--user-data-dir=${profile}`, '--window-size=390,844', baseUrl,
  ], { stdio: 'ignore' })
  let cdp
  try {
    cdp = await Cdp.connect(debugPort, baseUrl)
    assert.ok(cdp.pageErrorsSubscribed, 'CDP 异常采集未订阅成功，不能开始浏览器检查')
    await cdp.viewport(390, 844)
    // R8：异常采集必须覆盖「页面导航 + 业务脚本执行」本身。
    // 订阅在 connect() 里已经完成，这里清空后用一次整页重载作为采集起点，
    // 之后每一次 reload/导航都被完整覆盖。
    resetPageErrors()
    enterScenario('启动/首次导航')
    await reloadApp(cdp)
    await cdp.waitFor(`document.body && document.body.innerText.includes('任务工作台')`)

    async function ensureCloudCliPathVisible() {
      for (let i = 0; i < 8; i += 1) {
        const visible = await cdp.eval(`(() => { const el = document.querySelector('#wps-cli-path'); if (!el) return false; const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0 })()`)
        if (visible) return true
        await cdp.eval(clickByTextJs('button', '云文档同步'))
        await new Promise((r) => setTimeout(r, 180))
        await cdp.eval(clickByTextJs('button', '高级设置与写入目标'))
        await new Promise((r) => setTimeout(r, 280))
      }
      return false
    }
    await captureScreenshot(cdp, 'fe-c3-phone-390x844.png')

    // ---------- 1. 布局：窄屏/横屏/平板/桌面无横向溢出 ----------
    enterScenario('1 布局视口')
    for (const [label, width, height] of [
      ['360x800', 360, 800], ['390x844', 390, 844],
      ['800x450 landscape', 800, 450],
      ['768x1024 tablet', 768, 1024], ['1280x800 desktop', 1280, 800],
    ]) {
      await cdp.viewport(width, height)
      const overflow = await cdp.eval(`({ sw: document.documentElement.scrollWidth, iw: window.innerWidth })`)
      record(`布局无横向溢出 ${label}`, overflow.sw <= overflow.iw + 1, JSON.stringify(overflow))
      if (label.includes('landscape')) await captureScreenshot(cdp, 'fe-c3-landscape-800x450.png')
    }

    // ---------- 2. Tab 键盘导航 ----------
    enterScenario('2 Tab 键盘导航')
    await cdp.viewport(390, 844)
    await cdp.eval(`document.getElementById('task-tab-order')?.focus()`)
    await cdp.eval(`document.getElementById('task-tab-order')?.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowRight', bubbles: true }))`)
    await new Promise((r) => setTimeout(r, 150))
    const activeTab = await cdp.eval(`document.activeElement?.id || ''`)
    record('Tab 方向键切换并聚焦', activeTab === 'task-tab-cloud', activeTab)

    // ---------- 3. 密码清除 order：成功/失败/网络中断/重复点击 ----------
    enterScenario('3 密码清除')
    async function openOrderPasswordAndSet(value) {
      await cdp.viewport(390, 844)
      await cdp.eval(clickByTextJs('button', '订单处理'))
      await new Promise((r) => setTimeout(r, 200))
      const visible = await cdp.eval(`(() => { const el = document.querySelector('#order-password'); if (!el) return false; const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0 })()`)
      if (!visible) {
        await cdp.eval(clickByTextJs('button', '管理网址与登录凭据'))
        await new Promise((r) => setTimeout(r, 300))
      }
      const setResult = await cdp.eval(setInputJs('#order-password', value))
      assert.ok(setResult && setResult.ok === true, `无法写入 order-password：${JSON.stringify(setResult)}`)
    }
    async function openClearDialogAndCount() {
      assert.ok(await cdp.eval(clickByTextJs('button', '更多')), '找不到更多按钮')
      const menuOpened = await cdp.waitFor(`document.body.innerText.includes('清除管理后台密码')`, 2500)
      if (!menuOpened) {
        const visibleText = await cdp.eval(`document.body.innerText.slice(0, 400)`)
        throw new Error(`更多菜单未打开；可见文本=${JSON.stringify(visibleText)}`)
      }
      const clicked = await cdp.eval(clickByTextJs('[role="menuitem"]', '清除管理后台密码'))
      assert.ok(clicked, '找不到清除密码菜单')
      await cdp.waitFor(`document.body.innerText.includes('确认清除')`, 2500)
      assert.ok(await cdp.eval(`document.body.innerText.includes('确认清除')`), '确认清除弹窗未出现')
      return cdp.eval(`document.querySelectorAll('[role="dialog"]').length`)
    }
    // order success
    mock.clearResult = 'success'
    await openOrderPasswordAndSet('draft-order-secret')
    await openClearDialogAndCount()
    await cdp.eval(clickByTextJs('button', '确认清除'))
    await new Promise((r) => setTimeout(r, 450))
    const orderAfterSuccess = await cdp.eval(`document.querySelector('#order-password')?.value ?? null`)
    record('order 密码清除成功后才清空草稿', orderAfterSuccess === '', JSON.stringify({ value: orderAfterSuccess }))
    // order failure keeps draft and shows next step
    mock.clearResult = 'fail'
    await openOrderPasswordAndSet('draft-order-keep')
    await openClearDialogAndCount()
    await cdp.eval(clickByTextJs('button', '确认清除'))
    await new Promise((r) => setTimeout(r, 450))
    const orderAfterFail = await cdp.eval(`document.querySelector('#order-password')?.value ?? null`)
    const failNotice = await cdp.eval(`document.body.innerText.includes('模拟删除失败') && document.body.innerText.includes('不要把失败当作已清除')`)
    record('order 密码清除失败保留草稿并提示检查/重试', orderAfterFail === 'draft-order-keep' && failNotice === true, JSON.stringify({ value: orderAfterFail, failNotice }))
    // order network keeps draft
    enterScenario('3 密码清除：网络中断（故意断网）')
    mock.clearResult = 'network'
    await cdp.eval(clickByTextJs('button', '返回检查'))
    await new Promise((r) => setTimeout(r, 200))
    await openOrderPasswordAndSet('draft-network-keep')
    await openClearDialogAndCount()
    await cdp.eval(clickByTextJs('button', '确认清除'))
    await new Promise((r) => setTimeout(r, 600))
    const networkDraft = await cdp.eval(`document.querySelector('#order-password')?.value ?? null`)
    record('order 密码清除网络中断保留草稿且不显示已清除', networkDraft === 'draft-network-keep', JSON.stringify({ value: networkDraft }))
    // duplicate clicks: count should increase exactly 1
    mock.clearResult = 'success'
    await cdp.eval(clickByTextJs('button', '返回检查'))
    await new Promise((r) => setTimeout(r, 200))
    await openOrderPasswordAndSet('draft-duplicate')
    await cdp.eval(clickByTextJs('button', '更多'))
    await new Promise((r) => setTimeout(r, 200))
    await cdp.eval(clickByTextJs('[role="menuitem"]', '清除管理后台密码'))
    await cdp.waitFor(`document.body.innerText.includes('确认清除')`)
    const beforeDup = mock.clearCalls
    await cdp.eval(`(() => { const btn = [...document.querySelectorAll('button')].find((b) => b.textContent.includes('确认清除')); if (!btn) return false; btn.click(); btn.click(); return true })()`)
    await new Promise((r) => setTimeout(r, 500))
    record('重复点击清除密码只提交一次', mock.clearCalls - beforeDup === 1, `delta=${mock.clearCalls - beforeDup}`)

    // order 文案成功/失败均已验证；sss 成功清空
    // 重载后再测 sss，避免上一轮 Radix 弹层/菜单的焦点残留串扰。
    mock.clearResult = 'success'
    await cdp.send('Page.reload', { ignoreCache: true })
    await cdp.waitFor(`document.body && document.body.innerText.includes('任务工作台')`)
    await new Promise((r) => setTimeout(r, 300))
    await cdp.eval(clickByTextJs('button', '闪时送下单'))
    await new Promise((r) => setTimeout(r, 300))
    if (!(await cdp.eval(`!!document.querySelector('#sss-password')`))) {
      await cdp.eval(clickByTextJs('button', '闪时送网址、账号与文件路径'))
      await new Promise((r) => setTimeout(r, 250))
    }
    const sssSetResult = await cdp.eval(setInputJs('#sss-password', 'draft-sss-secret'))
    assert.ok(sssSetResult && sssSetResult.ok === true, `无法写入 sss-password：${JSON.stringify(sssSetResult)}`)
    assert.ok(await cdp.eval(clickByTextJs('button[aria-label="闪时送更多工具"]', '更多')), '找不到 sss 更多按钮')
    await new Promise((r) => setTimeout(r, 200))
    assert.ok(await cdp.eval(clickByTextJs('[role="menuitem"]', '清除闪时送密码')), '找不到清除闪时送密码菜单')
    await cdp.waitFor(`document.body.innerText.includes('确认清除')`)
    await cdp.eval(clickByTextJs('button', '确认清除'))
    await new Promise((r) => setTimeout(r, 450))
    const sssAfterSuccess = await cdp.eval(`document.querySelector('#sss-password')?.value ?? null`)
    record('sss 密码清除成功后才清空草稿', sssAfterSuccess === '', JSON.stringify({ value: sssAfterSuccess, modes: mock.clearModes }))

    // ---------- 3b. 配置自动保存竞态：延迟期间继续编辑，最终以最新草稿落盘 ----------
    enterScenario('3b 配置自动保存竞态')
    mock.saveOrderPayloads = []
    mock.saveOrderActive = 0
    mock.saveOrderMaxActive = 0
    mock.saveOrderDelay = 350
    mock.saveOrderFail = false
    await cdp.send('Page.reload', { ignoreCache: true })
    await cdp.waitFor(`document.body && document.body.innerText.includes('任务工作台')`)
    await cdp.viewport(390, 844)
    if (!(await cdp.eval(`(() => { const el = document.querySelector('#order-excel'); if (!el) return false; const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0 })()`))) {
      await cdp.eval(clickByTextJs('button', '管理网址与登录凭据'))
      await new Promise((r) => setTimeout(r, 250))
    }
    assert.ok(await cdp.eval(setInputJs('#order-excel', '/tmp/race-A.xlsx')), '无法写入 A')
    assert.ok(await waitForMock(() => mock.saveOrderPayloads.length >= 1, 3000), 'A 保存未开始')
    // A 仍在途，用户继续编辑为 B
    assert.ok(await cdp.eval(setInputJs('#order-excel', '/tmp/race-B.xlsx')), '无法写入 B')
    assert.ok(await waitForMock(() => mock.saveOrderPayloads.length >= 2, 4000), 'B 未串行落盘')
    await cdp.waitFor(`document.body.innerText.includes('已保存')`, 4000)
    const orderPayloads = mock.saveOrderPayloads.map((p) => p.excel)
    const orderRaceUi = await cdp.eval(`({ input: document.querySelector('#order-excel')?.value ?? null, saved: document.body.innerText.includes('已保存') })`)
    record(
      '延迟保存期间继续编辑：界面/服务端/提示都对应最新 B',
      orderPayloads[orderPayloads.length - 1] === '/tmp/race-B.xlsx'
        && orderPayloads.length === 2
        && mock.saveOrderMaxActive === 1
        && orderRaceUi.input === '/tmp/race-B.xlsx'
        && orderRaceUi.saved === true,
      JSON.stringify({ orderPayloads, maxActive: mock.saveOrderMaxActive, ui: orderRaceUi }),
    )

    // 失败后继续编辑：重试/自动保存必须发送最新草稿，不能把旧失败当成功
    mock.saveOrderFail = true
    assert.ok(await cdp.eval(setInputJs('#order-excel', '/tmp/race-C-fail.xlsx')), '无法写入 C')
    assert.ok(await waitForMock(() => mock.saveOrderPayloads.some((p) => p.excel === '/tmp/race-C-fail.xlsx'), 4000), '失败请求未发出')
    await cdp.waitFor(`document.body.innerText.includes('保存失败')`, 3000)
    const failureUi = await cdp.eval(`document.body.innerText`)
    record('保存失败不会显示已保存', failureUi.includes('保存失败') && !failureUi.includes('已保存'), '')
    mock.saveOrderFail = false
    assert.ok(await cdp.eval(setInputJs('#order-excel', '/tmp/race-D-latest.xlsx')), '无法写入 D')
    assert.ok(await waitForMock(() => mock.saveOrderPayloads.some((p) => p.excel === '/tmp/race-D-latest.xlsx'), 4000), '最新草稿未重试')
    await cdp.waitFor(`document.body.innerText.includes('已保存')`, 4000)
    const lastPayload = mock.saveOrderPayloads[mock.saveOrderPayloads.length - 1]?.excel
    record('保存失败后继续编辑，重试保存最新草稿', lastPayload === '/tmp/race-D-latest.xlsx' && mock.saveOrderMaxActive === 1,
      JSON.stringify({ lastPayload, maxActive: mock.saveOrderMaxActive }))

    // 闪时送表单同样走版本化串行保存
    mock.saveSssPayloads = []
    mock.saveSssActive = 0
    mock.saveSssMaxActive = 0
    mock.saveSssDelay = 350
    mock.saveSssFail = false
    await cdp.send('Page.reload', { ignoreCache: true })
    await cdp.waitFor(`document.body && document.body.innerText.includes('任务工作台')`)
    for (let i = 0; i < 6; i += 1) {
      const sssVisible = await cdp.eval(`(() => { const el = document.querySelector('#sss-product'); if (!el) return false; const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0 })()`)
      if (sssVisible) break
      await cdp.eval(clickByTextJs('button', '闪时送下单'))
      await new Promise((r) => setTimeout(r, 250))
    }
    const sssA = await cdp.eval(setInputJs('#sss-product', '轻食-A'))
    assert.ok(sssA && sssA.ok === true, `无法写入 sss A：${JSON.stringify(sssA)}`)
    assert.ok(await waitForMock(() => mock.saveSssPayloads.length >= 1, 3000), 'sss A 保存未开始')
    const sssB = await cdp.eval(setInputJs('#sss-product', '轻食-B'))
    assert.ok(sssB && sssB.ok === true, `无法写入 sss B：${JSON.stringify(sssB)}`)
    assert.ok(await waitForMock(() => mock.saveSssPayloads.length >= 2, 4000), 'sss B 未串行落盘')
    await cdp.waitFor(`document.body.innerText.includes('已保存')`, 4000)
    const sssRace = await cdp.eval(`({ input: document.querySelector('#sss-product')?.value ?? null, saved: document.body.innerText.includes('已保存') })`)
    record(
      '闪时送表单延迟保存期间继续编辑：最新 B 落盘且无并发',
      mock.saveSssPayloads[mock.saveSssPayloads.length - 1]?.product_name === '轻食-B'
        && mock.saveSssPayloads.length === 2
        && mock.saveSssMaxActive === 1
        && sssRace.input === '轻食-B'
        && sssRace.saved === true,
      JSON.stringify({ payloads: mock.saveSssPayloads.map((p) => p.product_name), maxActive: mock.saveSssMaxActive, ui: sssRace }),
    )

    // 离开页面前未保存草稿必须有确认，不静默丢弃
    mock.saveOrderDelay = 0
    mock.saveOrderFail = false
    mock.saveOrderPayloads = []
    mock.saveOrderActive = 0
    mock.saveOrderMaxActive = 0
    await cdp.send('Page.reload', { ignoreCache: true })
    await cdp.waitFor(`document.body && document.body.innerText.includes('任务工作台')`)
    let orderExcelVisible = await cdp.eval(`(() => { const el = document.querySelector('#order-excel'); if (!el) return false; const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0 })()`)
    for (let i = 0; i < 5 && !orderExcelVisible; i += 1) {
      await cdp.eval(clickByTextJs('button', '管理网址与登录凭据'))
      await new Promise((r) => setTimeout(r, 300))
      orderExcelVisible = await cdp.eval(`(() => { const el = document.querySelector('#order-excel'); if (!el) return false; const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0 })()`)
    }
    const leaveSetResult = await cdp.eval(setInputJs('#order-excel', '/tmp/unsaved-leave.xlsx'))
    assert.ok(leaveSetResult && leaveSetResult.ok === true, `无法写入离开页面前草稿：${JSON.stringify(leaveSetResult)}`)
    await new Promise((r) => setTimeout(r, 100))
    const preventedDirty = await cdp.eval(`(() => { const event = new Event('beforeunload', { cancelable: true }); window.dispatchEvent(event); return event.defaultPrevented })()`)
    record('有未保存草稿时 beforeunload 会阻止静默离开', preventedDirty === true, `prevented=${preventedDirty}`)
    await cdp.waitFor(`document.body.innerText.includes('已保存')`, 4000)
    const preventedSaved = await cdp.eval(`(() => { const event = new Event('beforeunload', { cancelable: true }); window.dispatchEvent(event); return event.defaultPrevented })()`)
    record('无未保存草稿时 beforeunload 不误拦截', preventedSaved === false, `prevented=${preventedSaved}`)

    // ---------- 3c. 刷新不能覆盖请求发出后的新编辑 / 不能覆盖未落盘草稿 ----------
    enterScenario('3c 刷新与保存乱序')
    // 场景 A：刷新过程中用户编辑
    mock.saveWpsPayloads = []
    mock.saveWpsActive = 0
    mock.saveWpsMaxActive = 0
    mock.saveWpsDelay = 120
    mock.saveWpsFail = false
    mock.wpsStatusDelay = 450
    mock.wpsStatusCliPath = '/tmp/server-stale-cli'
    mock.wpsStatusAddressOrder = {}
    await cdp.send('Page.reload', { ignoreCache: true })
    await cdp.waitFor(`document.body && document.body.innerText.includes('任务工作台')`)
    await cdp.viewport(390, 844)
    assert.ok(await ensureCloudCliPathVisible(), '云同步高级设置未打开')
    assert.ok(await cdp.eval(clickByTextJs('button', '刷新状态（只读）')), '找不到刷新状态按钮')
    // 刷新请求在途时立即编辑；旧响应不得覆盖本地值
    await new Promise((r) => setTimeout(r, 80))
    const duringSet = await cdp.eval(setInputJs('#wps-cli-path', '/tmp/local-during-refresh'))
    assert.ok(duringSet && duringSet.ok === true, `无法写入 local-during-refresh：${JSON.stringify(duringSet)}`)
    await new Promise((r) => setTimeout(r, 700))
    const cliDuringRefresh = await cdp.eval(`document.querySelector('#wps-cli-path')?.value ?? null`)
    await waitForMock(() => mock.saveWpsPayloads.length >= 1, 3000)
    record('刷新期间编辑：本地输入不被旧刷新覆盖', cliDuringRefresh === '/tmp/local-during-refresh', JSON.stringify({ cliDuringRefresh }))

    // 场景 B：保存仍在途时刷新，旧状态响应也不能覆盖未落盘草稿
    mock.saveWpsPayloads = []
    mock.saveWpsActive = 0
    mock.saveWpsMaxActive = 0
    mock.saveWpsDelay = 350
    mock.wpsStatusDelay = 300
    mock.wpsStatusCliPath = '/tmp/server-stale-cli-2'
    await cdp.send('Page.reload', { ignoreCache: true })
    await cdp.waitFor(`document.body && document.body.innerText.includes('任务工作台')`)
    assert.ok(await ensureCloudCliPathVisible(), '云同步高级设置未打开')
    const saveThenSet = await cdp.eval(setInputJs('#wps-cli-path', '/tmp/local-save-then-refresh'))
    assert.ok(saveThenSet && saveThenSet.ok === true, `无法写入 save-then-refresh：${JSON.stringify(saveThenSet)}`)
    assert.ok(await waitForMock(() => mock.saveWpsPayloads.length >= 1, 3000), 'WPS 保存未开始')
    assert.ok(await cdp.eval(clickByTextJs('button', '刷新状态（只读）')), '找不到刷新状态按钮')
    await new Promise((r) => setTimeout(r, 700))
    const cliAfterSaveRefresh = await cdp.eval(`document.querySelector('#wps-cli-path')?.value ?? null`)
    await cdp.waitFor(`document.body.innerText.includes('已保存')`, 4000)
    record('保存未返回时刷新：未落盘草稿不被旧服务端值覆盖', cliAfterSaveRefresh === '/tmp/local-save-then-refresh',
      JSON.stringify({ cliAfterSaveRefresh, lastSave: mock.saveWpsPayloads[mock.saveWpsPayloads.length - 1]?.cli_path }))
    record('刷新与保存乱序时保存请求仍串行', mock.saveWpsMaxActive === 1, `maxActive=${mock.saveWpsMaxActive}`)

    // ---------- 4. pending interaction 刷新恢复 + 去重 + 恢复不等于自动提交 ----------
    enterScenario('4 pending 恢复')
    mock.pendingItems = [
      {
        interaction_id: 'd1', operation_id: 'op-test', kind: 'order_retry', status: 'pending',
        created_at: 1, expires_at: Date.now() / 1000 + 300,
        request: { title: '订单定位失败', message: '请选择安全处理方式', choices: [{ value: 'retry', label: '重试', style: 'primary' }, { value: 'stop', label: '停止', style: 'danger' }] },
      },
      {
        interaction_id: 'd1', operation_id: 'op-test', kind: 'order_retry', status: 'pending',
        created_at: 2, expires_at: Date.now() / 1000 + 300,
        request: { title: '重复不应恢复', message: '重复项', choices: [{ value: 'retry', label: '重试', style: 'primary' }] },
      },
    ]
    mock.resolveDecisionCalls = 0
    await cdp.send('Page.reload', { ignoreCache: true })
    await cdp.waitFor(`document.body && document.body.innerText.includes('任务工作台')`)
    await new Promise((r) => setTimeout(r, 800))
    await cdp.waitFor(`document.body.innerText.includes('订单定位失败')`)
    const alertCount = await cdp.eval(`document.querySelectorAll('[role="alertdialog"]').length`)
    record('刷新后只读恢复有效决策弹窗（interaction_id 去重）', alertCount === 1, `alertdialog=${alertCount}`)
    await captureScreenshot(cdp, 'fe-c3-restore-decision-390x844.png')
    record('恢复不等于自动提交', mock.resolveDecisionCalls === 0, `resolveCalls=${mock.resolveDecisionCalls}`)
    assert.ok(await cdp.eval(clickByTextJs('button', '重试')), '找不到恢复决策按钮')
    await new Promise((r) => setTimeout(r, 400))
    record('恢复后用户手动提交只发生一次', mock.resolveDecisionCalls === 1, `resolveCalls=${mock.resolveDecisionCalls}`)

    // resolved/expired/无权限 -> pending list empty, 不恢复
    mock.pendingItems = []
    await cdp.send('Page.reload', { ignoreCache: true })
    await cdp.waitFor(`document.body && document.body.innerText.includes('任务工作台')`)
    await new Promise((r) => setTimeout(r, 600))
    const noRestored = await cdp.eval(`document.querySelectorAll('[role="alertdialog"]').length === 0`)
    record('已解决/过期/无权限不恢复弹窗', noRestored === true)

    // ---------- 4b. captcha / address 只读恢复与真实组件交互 ----------
    enterScenario('4b 验证码与地址恢复')
    mock.pendingItems = [
      {
        interaction_id: 'c9', operation_id: 'op-test', kind: 'captcha', status: 'pending',
        created_at: 1, expires_at: Date.now() / 1000 + 300, request: { image: 'aGVsbG8=' },
      },
      {
        interaction_id: 'a9', operation_id: 'op-test', kind: 'address_input', status: 'pending',
        created_at: 2, expires_at: Date.now() / 1000 + 300,
        request: {
          title: '地址待确认', message: '请输入最终地址', 
          items: [{ raw_address: '应标 D2', order_numbers: ['W100'], campus: '', confidence: '', reason: '合成', suggested_point: '' }],
        },
      },
    ]
    mock.resolveCaptchaCalls = 0
    mock.resolveAddressCalls = 0
    mock.resolveCaptchaNetwork = false
    await cdp.send('Page.reload', { ignoreCache: true })
    await cdp.waitFor(`document.body && document.body.innerText.includes('任务工作台')`)
    await cdp.waitFor(`document.body.innerText.includes('闪时送登录验证')`)
    const restoredBoth = await cdp.eval(`document.body.innerText.includes('闪时送登录验证') && document.body.innerText.includes('待确认地址') && !!document.querySelector('input[placeholder="请输入验证码"]') && !!document.querySelector('input[placeholder="输入最终地址，如 D2 / 学三 / 教5"]')`)
    record('刷新后恢复验证码与地址补录弹窗/输入框', restoredBoth === true)
    assert.ok(await cdp.eval(setInputJs('input[placeholder="请输入验证码"]', '1234')), '无法写入验证码')
    assert.ok(await cdp.eval(clickByTextJs('button', '确定')), '找不到验证码确定按钮')
    await new Promise((r) => setTimeout(r, 500))
    record('验证码恢复后手动提交一次', mock.resolveCaptchaCalls === 1, `resolveCaptchaCalls=${mock.resolveCaptchaCalls}`)
    assert.ok(await cdp.eval(setInputJs('input[placeholder="输入最终地址，如 D2 / 学三 / 教5"]', 'D2')), '无法写入地址补录')
    assert.ok(await cdp.eval(clickByTextJs('button', '应用并排序')), '找不到应用并排序按钮')
    await new Promise((r) => setTimeout(r, 500))
    record('地址补录恢复后手动提交一次', mock.resolveAddressCalls === 1, `resolveAddressCalls=${mock.resolveAddressCalls}`)

    // captcha 网络中断：服务端已取消/未知，前端保留输入并提示重试，不自动重复提交
    mock.pendingItems = [{
      interaction_id: 'c10', operation_id: 'op-test', kind: 'captcha', status: 'pending',
      created_at: 1, expires_at: Date.now() / 1000 + 300, request: { image: 'aGVsbG8=' },
    }]
    enterScenario('4b 验证码提交：网络中断（故意断网）')
    mock.resolveCaptchaNetwork = true
    mock.resolveCaptchaCalls = 0
    await cdp.send('Page.reload', { ignoreCache: true })
    await cdp.waitFor(`document.body && document.body.innerText.includes('任务工作台')`)
    await cdp.waitFor(`document.body.innerText.includes('闪时送登录验证')`)
    assert.ok(await cdp.eval(setInputJs('input[placeholder="请输入验证码"]', '9999')), '无法写入验证码')
    assert.ok(await cdp.eval(clickByTextJs('button', '确定')), '找不到验证码确定按钮')
    await new Promise((r) => setTimeout(r, 700))
    const captchaRetained = await cdp.eval(`document.querySelector('input[placeholder="请输入验证码"]')?.value === '9999' && document.body.innerText.includes('输入已保留')`)
    record('验证码提交网络失败保留输入且不自动重提', captchaRetained === true, `calls=${mock.resolveCaptchaCalls}`)
    mock.resolveCaptchaNetwork = false
    mock.pendingItems = []

    // ---------- 5. WPS recovery 只读可见、刷新不触发上传 ----------
    enterScenario('5 WPS 只读恢复卡片')
    const recoveryOpLegacy = {
      operation_id: 'wps-1234567890abcdef', operation_ref: 'wps-op:abcdef123456',
      status: 'uncertain', pending: true, cloud_checked: true,
      created_at: '2026-09-19T10:00:00', updated_at: '2026-09-19T10:01:00',
      target_date: '2026-09-20', target_refs: ['wps-target:abc123def456'], sheet_count: 1,
      error_code: 'wps_recovery_uncertain', allowed_next_actions: ['manual_reconcile'],
      manual_required: true,
      sheets: [{
        target_date: '2026-09-20', target_ref: 'wps-target:abc123def456',
        status: 'uncertain', raw_status: 'uncertain', error_code: 'wps_recovery_uncertain',
        allowed_next_actions: ['manual_reconcile'], manual_required: true,
        cloud_checked: true, evidence: 'journal+cloud_read',
      }],
    }
    mock.wpsRecovery = {
      ok: true, contract_version: 1, source: 'local_journal', read_only: true,
      queried_cloud: false, contains_cloud_checked_records: true, scope: 'admin',
      counts: { planned: 0, writing: 0, ledger_pending: 0, uncertain: 1, verified: 0, failed: 0, not_started: 0 },
      next_action: 'manual_reconcile', error_code: 'wps_recovery_uncertain',
      summary: { operation_count: 1, pending_count: 1, uncertain_count: 1, failed_count: 0, not_started_count: 0, has_pending: true, needs_review: true, guidance: '存在不确定结果：请先只读核对云端与日志，不要直接重传' },
      operations: [recoveryOpLegacy], pending_operations: [recoveryOpLegacy],
    }
    mock.wpsUploadCalls = 0
    await cdp.eval(clickByTextJs('button', '云文档同步'))
    await new Promise((r) => setTimeout(r, 300))
    if (!(await cdp.eval(`document.body.innerText.includes('刷新状态（只读）')`))) {
      await cdp.eval(clickByTextJs('button', '高级设置与写入目标'))
      await new Promise((r) => setTimeout(r, 300))
    }
    // 先刷新一次让卡片反映本次 mock 的恢复状态，作为"刷新前"基线；
    // 否则"刷新前后对比"会退化成只看刷新后的快照。
    assert.ok(await cdp.eval(clickByTextJs('button', '刷新状态（只读）')), '找不到刷新状态按钮')
    await cdp.waitFor(`document.body.innerText.includes('待核对批次（只读可见）')`, 5000)
    assert.ok(
      await cdp.eval(`document.body.innerText.includes('待核对批次（只读可见）')`),
      '首次刷新后恢复卡片仍未显示待核对批次',
    )
    const uploadBefore = mock.wpsUploadCalls
    mock.saveWpsPayloads = []
    const resolveCallsBeforeRefresh = mock.wpsResolveCalls
    const recoveryBeforeRefresh = await cdp.eval('document.body.innerText')
    assert.ok(await cdp.eval(clickByTextJs('button', '刷新状态（只读）')), '找不到刷新状态按钮')
    await new Promise((r) => setTimeout(r, 600))
    const recoveryText = await cdp.eval(`document.body.innerText`)
    record('WPS 只读恢复卡片可见且标明下一步', recoveryText.includes('待核对批次（只读可见）') && recoveryText.includes('只读核对云端与日志') && recoveryText.includes('不会上传、不会解除阻断、不会自动重试'), '')
    record('管理员最小 DTO 不出现 problems/risk_reason/客户明细', !recoveryText.includes('risk_reason') && !recoveryText.includes('problems') && !recoveryText.includes('合成待核对'), '')
    record('管理员恢复卡片显示 allowed_next_actions 安全动作', documentTextIncludes(await cdp.eval('document.body.textContent'), ['允许动作', 'manual_reconcile']), '')
    await captureScreenshot(cdp, 'fe-c3-wps-recovery-390x844.png')
    record('刷新状态不触发 wps_upload', mock.wpsUploadCalls === uploadBefore && mock.wpsUploadCalls === 0, `uploads=${mock.wpsUploadCalls}`)
    record('纯刷新回填不触发无意义自动保存', mock.saveWpsPayloads.length === 0, `savePayloads=${mock.saveWpsPayloads.length}`)
    record(
      '刷新状态不解除阻断（刷新前后都仍是待核对，且未调用恢复写入）',
      recoveryBeforeRefresh.includes('待核对批次（只读可见）')
        && recoveryText.includes('待核对批次（只读可见）')
        && mock.wpsResolveCalls === resolveCallsBeforeRefresh,
      `before=${recoveryBeforeRefresh.includes('待核对批次（只读可见）')} after=${recoveryText.includes('待核对批次（只读可见）')} resolveDelta=${mock.wpsResolveCalls - resolveCallsBeforeRefresh}`,
    )
    record('本地 journal 与云端只读核对有区分', recoveryText.includes('本查询未调用云端') && recoveryText.includes('云端只读核对'), '')

    // recovery 损坏 -> 失败关闭
    mock.wpsRecovery = {
      ok: false, contract_version: 1, source: 'local_journal', read_only: true,
      queried_cloud: false, contains_cloud_checked_records: false, scope: 'admin',
      counts: { planned: 0, writing: 0, ledger_pending: 0, uncertain: 0, verified: 0, failed: 0, not_started: 0 },
      next_action: 'fix_journal', error_code: 'wps_recovery_journal_unreadable',
      summary: { needs_review: true, guidance: '恢复状态不可用：请联系管理员只读核对，不要直接重试或重新上传' },
    }
    assert.ok(await cdp.eval(clickByTextJs('button', '刷新状态（只读）')), '找不到刷新状态按钮')
    await cdp.waitFor(`document.body.innerText.includes('本地恢复记录不可读')`)
    const brokenText = await cdp.eval('document.body.innerText')
    record('recovery_status 损坏失败关闭，不伪装成无待恢复', brokenText.includes('本地恢复记录不可读') && brokenText.includes('不要直接重试'), '')

    // summary scope：普通用户安全摘要，不能因缺少 operations 崩溃，不得显示成功
    mock.wpsRecoveryForbidden = false
    mock.wpsRecovery = {
      ok: true, contract_version: 1, source: 'local_journal', read_only: true,
      queried_cloud: false, contains_cloud_checked_records: false, scope: 'summary',
      counts: { planned: 0, writing: 0, ledger_pending: 0, uncertain: 1, verified: 0, failed: 0, not_started: 0 },
      next_action: 'manual_reconcile', error_code: 'wps_recovery_uncertain',
      summary: { operation_count: 1, pending_count: 1, uncertain_count: 1, failed_count: 0, not_started_count: 0, has_pending: true, needs_review: true, guidance: '存在不确定结果：请先只读核对云端与日志，不要直接重传' },
    }
    assert.ok(await cdp.eval(clickByTextJs('button', '刷新状态（只读）')), '找不到刷新状态按钮')
    await cdp.waitFor(`document.body.innerText.includes('安全摘要视图')`)
    const summaryText = await cdp.eval(`document.body.innerText`)
    record('summary scope 无 operations 也能显示数量与下一步', summaryText.includes('安全摘要视图') && summaryText.includes('待核对') && summaryText.includes('只读核对云端与日志'), '')
    record('summary scope 缺明细不显示成功/重传', !summaryText.includes('本地恢复记录：无待核对批次') && mock.wpsUploadCalls === 0, `uploads=${mock.wpsUploadCalls}`)

    // 权限失败：准确权限提示，不误报网络故障；不得反复请求
    enterScenario('5 恢复查询：无权限 403（预期）')
    mock.wpsRecoveryForbidden = true
    await new Promise((r) => setTimeout(r, 500))
    const callsBeforeForbidden = mock.wpsRecoveryCalls
    assert.ok(await cdp.eval(clickByTextJs('button', '刷新状态（只读）')), '找不到刷新状态按钮')
    await cdp.waitFor(`document.body.innerText.includes('当前账号没有该权限')`)
    await new Promise((r) => setTimeout(r, 800))
    const forbiddenText = await cdp.eval(`document.body.innerText`)
    const forbiddenDelta = mock.wpsRecoveryCalls - callsBeforeForbidden
    record('恢复查询无权限显示准确权限提示，不误报网络故障', forbiddenText.includes('当前账号没有该权限') && !forbiddenText.includes('网络已断开'), '')
    record('恢复查询无权限不反复请求', forbiddenDelta === 1, `delta=${forbiddenDelta}`)
    mock.wpsRecoveryForbidden = false

    // 管理员只读查询到其他账号 pending 时只显示安全提示，不打开输入、不自动提交
    mock.pendingItems = [{
      interaction_id: 'x1', operation_id: 'op-x', kind: 'order_retry', status: 'pending',
      created_at: 1, expires_at: Date.now() / 1000 + 300, request: {}, request_redacted: true,
    }]
    mock.resolveDecisionCalls = 0
    await cdp.send('Page.reload', { ignoreCache: true })
    await cdp.waitFor(`document.body && document.body.innerText.includes('任务工作台')`)
    await cdp.waitFor(`document.body.innerText.includes('属于其他账号')`)
    const redactedState = await cdp.eval(`({ dialogs: document.querySelectorAll('[role="alertdialog"]').length, text: document.body.innerText })`)
    record('pending 权限脱敏只显示元数据提示，不打开输入', redactedState.dialogs === 0 && redactedState.text.includes('属于其他账号') && redactedState.text.includes('不会自动提交'), JSON.stringify({ dialogs: redactedState.dialogs }))
    record('pending 权限脱敏不自动提交', mock.resolveDecisionCalls === 0, `resolveCalls=${mock.resolveDecisionCalls}`)
    mock.pendingItems = []

    // ---------- 5b. W6：计划口径 vs 已核实完成 ----------
    enterScenario('5b W6 计划口径')
    async function ensureCloudTab() {
      await cdp.eval(clickButtonJs('云文档同步'))
      await new Promise((r) => setTimeout(r, 250))
    }

    async function runUploadFlow(uploadPayload, options = {}) {
      mock.wpsPreview = syntheticPreview()
      mock.wpsUpload = uploadPayload
      await ensureCloudTab()
      const clicked = await cdp.eval(clickButtonJs('生成只读预览')) || await cdp.eval(clickButtonJs('重新预览'))
      assert.ok(clicked, '找不到生成只读预览按钮')
      await cdp.waitFor(`document.body.innerText.includes('结构化预览')`)
      assert.ok(await cdp.eval(clickButtonJs('确认上传')), '找不到确认上传按钮')
      assert.ok(await cdp.waitFor(`document.body.innerText.includes('确认上传云文档')`), '确认弹窗未打开')
      assert.ok(await cdp.eval(clickDialogCheckboxJs), '无法勾选明确确认句')
      await new Promise((r) => setTimeout(r, 150))
      if (options.burst) {
        const burst = await cdp.eval(burstDialogButtonJs('确认上传', options.burst))
        assert.ok(burst?.ok, `找不到弹窗内的确认上传按钮：${JSON.stringify(burst)}`)
      } else {
        assert.ok((await cdp.eval(clickDialogButtonJs('确认上传')))?.ok, '找不到弹窗内的确认上传按钮')
      }
      await cdp.waitFor(`document.body.innerText.includes('确认上传云文档') === false`, 6000)
      await new Promise((r) => setTimeout(r, 400))
    }

    mock.isAdmin = true
    mock.operationStatus = null
    mock.wpsRecovery = {
      ok: true, contract_version: 1, source: 'local_journal', read_only: true,
      queried_cloud: false, contains_cloud_checked_records: false, scope: 'admin',
      counts: {}, next_action: 'none', error_code: '',
      summary: {
        operation_count: 0, pending_count: 0, uncertain_count: 0, failed_count: 0,
        not_started_count: 0, has_pending: false, needs_review: false, guidance: '没有待恢复操作',
      },
      operations: [], pending_operations: [],
    }
    await reloadApp(cdp)

    // 刷新回填回归：hasUnsaved 曾以未绑定方法导出 → refresh() 抛 TypeError，
    // 服务端配置永远回填不进来，而且显示假的“状态刷新失败”。这里直接断言回填生效。
    await ensureCloudTab()
    mock.wpsStatusCliPath = '/tmp/synthetic-kdocs-cli-refreshed'
    assert.ok(await cdp.eval(clickButtonJs('高级设置与写入目标')), '找不到高级设置按钮')
    await new Promise((r) => setTimeout(r, 250))
    assert.ok(await cdp.eval(clickButtonJs('刷新状态（只读）')), '找不到刷新状态按钮')
    await new Promise((r) => setTimeout(r, 700))
    const refreshState = await cdp.eval(`({
      cli: document.querySelector('#wps-cli-path')?.value ?? null,
      text: document.body.innerText,
    })`)
    record(
      '刷新状态能真正回填服务端值（hasUnsaved 绑定回归）',
      refreshState.cli === '/tmp/synthetic-kdocs-cli-refreshed',
      `cli=${refreshState.cli}`,
    )
    record(
      '刷新状态不出现假的“状态刷新失败/TypeError”',
      !refreshState.text.includes('状态刷新失败') && !refreshState.text.includes('reading \'phase\''),
      nearText(refreshState.text, '状态刷新失败'),
    )
    mock.wpsStatusCliPath = '/tmp/synthetic-kdocs-cli'
    assert.ok(await cdp.eval(clickButtonJs('刷新状态（只读）')), '找不到刷新状态按钮')
    await new Promise((r) => setTimeout(r, 600))

    // W6-A：uncertain 不得出现任何“已完成”暗示
    await runUploadFlow({
      ok: false, status: 'uncertain', code: '', reason: '可能已写入但无法确认',
      next_action: '只读核对云端与日志，不要重新上传',
      operation_id: 'op-mock-uncertain',
      uncertain: true, verification_missing: true,
      planned_summary: { kind: 'plan', rows: { to_update: 1, to_append: 0, unchanged: 0, skipped: 0, warned: 0 } },
      execution_summary: {
        kind: 'execution', status: 'uncertain', executed: true, counts_source: 'apply_plan',
        sheets: { total: 1, verified: 0, noop: 0, failed: 0, uncertain: 1, skipped: 0, blocked: 0, other: 0 },
        rows: { verified: null, failed: null, uncertain: null, skipped: null, planned: 1 },
        rows_unknown: true, proven_no_write: false, written_sheets: null, failed_sheets: null,
        next_action: '只读核对，不重新上传',
      },
    })
    const uncertainText = await cdp.eval('document.body.innerText')
    const uncertainAttrs = await cdp.eval(resultAttrsJs)
    record(
      'W6 uncertain 不显示“上传完成/已完成”',
      uncertainText.includes('上传结果不确定') && !uncertainText.includes('上传完成'),
      `hasUncertain=${uncertainText.includes('上传结果不确定')} hasComplete=${uncertainText.includes('上传完成')}`,
    )
    record(
      'W6 uncertain 机器可读语义：complete=false / rowsUnknown=true / 无已核实行数',
      uncertainAttrs?.status === 'uncertain'
        && uncertainAttrs?.complete === 'false'
        && uncertainAttrs?.rowsUnknown === 'true'
        && uncertainAttrs?.verifiedRows === 'unknown',
      JSON.stringify(uncertainAttrs),
    )
    record(
      'W6 uncertain 实际写入行数显示未知，不显示计划数冒充',
      uncertainText.includes('实际写入行数未知') && !/已核实写入\s*1\s*行/.test(uncertainText),
      '',
    )
    record(
      'W6 uncertain 计划数只以“计划”出现，不出现旧“更新 0 · 新增 0”',
      uncertainText.includes('计划更新 1') && !uncertainText.includes('更新 0 · 新增 0'),
      '',
    )
    record(
      'W6 uncertain 明示这不是已完成',
      uncertainText.includes('这不是“已完成”') && uncertainText.includes('下一步：只读核对云端与日志'),
      '',
    )
    await cdp.eval(`document.querySelector('[data-wps-result="upload"]')?.scrollIntoView({ block: 'center' })`)
    await new Promise((r) => setTimeout(r, 250))
    await captureScreenshot(cdp, 'fe-w6-uncertain-390x844.png')

    // W6-B：计划 1 行、实际未核实 → 不能显示“已更新 1 行”
    await runUploadFlow({
      ok: true, status: 'success', code: '', reason: '', next_action: '',
      operation_id: 'op-mock-success-unknown',
      planned_summary: { kind: 'plan', rows: { to_update: 1, to_append: 0, unchanged: 0, skipped: 0, warned: 0 } },
      execution_summary: {
        kind: 'execution', status: 'success', executed: true, counts_source: 'apply_plan',
        sheets: { total: 1, verified: 1, noop: 0, failed: 0, uncertain: 0, skipped: 0, blocked: 0, other: 0 },
        rows: { verified: null, failed: 0, uncertain: 0, skipped: 0, planned: 1 },
        rows_unknown: true, proven_no_write: false, written_sheets: 1, failed_sheets: 0,
      },
    })
    const planUnknownText = await cdp.eval('document.body.innerText')
    const planUnknownAttrs = await cdp.eval(resultAttrsJs)
    record(
      'W6 计划 1 行 + 实际未核实：机器可读语义仍为 rowsUnknown=true / verifiedRows=unknown',
      planUnknownAttrs?.rowsUnknown === 'true' && planUnknownAttrs?.verifiedRows === 'unknown',
      JSON.stringify(planUnknownAttrs),
    )
    record(
      'W6 计划 1 行 + 实际未核实：计划标注为计划、实际显示未知',
      planUnknownText.includes('计划更新 1') && planUnknownText.includes('实际写入行数未知'),
      '',
    )
    record(
      'W6 计划 1 行 + 实际未核实：不得显示“已核实写入 1 行”',
      !/已核实写入\s*1\s*行/.test(planUnknownText) && !/已更新\s*1\s*行/.test(planUnknownText),
      '',
    )
    record(
      'W6 旧缺陷反证：不再渲染“更新 0 · 新增 0 · 跳过 0 · 不变 0”',
      !planUnknownText.includes('更新 0 · 新增 0') && !planUnknownText.includes('更新 0·新增 0'),
      '',
    )
    record(
      'F1：ok+success 但实际未核实时，卡片本身不得显示“上传完成”',
      planUnknownAttrs?.complete === 'false' && !planUnknownText.includes('上传完成'),
      `complete=${planUnknownAttrs?.complete} hasComplete=${planUnknownText.includes('上传完成')}`,
    )
    record(
      'W6-B 正向对照：计划行标签确实渲染（避免只做否定断言）',
      planUnknownText.includes('计划变更行') && planUnknownText.includes('实际写入'),
      '',
    )

    // F2：上传确认按钮连点必须在 React 重渲染之前就被挡掉（一次性令牌不能被消费两次）。
    mock.wpsUploadDelay = 700
    mock.wpsUploadMaxActive = 0
    const uploadCallsBeforeBurst = mock.wpsUploadCalls
    await runUploadFlow({
      ok: true, status: 'success', code: '', reason: '', next_action: '',
      operation_id: 'op-mock-burst',
      planned_summary: { kind: 'plan', rows: { to_update: 1, to_append: 0, unchanged: 0, skipped: 0, warned: 0 } },
      execution_summary: {
        kind: 'execution', status: 'success', executed: true, counts_source: 'apply_plan',
        sheets: { total: 1, verified: 1, noop: 0, failed: 0, uncertain: 0, skipped: 0, blocked: 0, other: 0 },
        rows: { verified: 1, failed: 0, uncertain: 0, skipped: 0, planned: 1 },
        rows_unknown: false, proven_no_write: false, written_sheets: 1, failed_sheets: 0,
      },
    }, { burst: 3 })
    const uploadBurstDelta = mock.wpsUploadCalls - uploadCallsBeforeBurst
    record(
      'F2 上传确认连点/双击只提交一次（单飞闸门）',
      uploadBurstDelta === 1 && mock.wpsUploadMaxActive === 1,
      `calls=${uploadBurstDelta} maxActive=${mock.wpsUploadMaxActive}`,
    )
    record(
      'F2 连点后仍显示成功结果（不被 preview_consumed 覆盖成“未写入”）',
      (await cdp.eval('document.body.innerText')).includes('上传完成'),
      '',
    )
    mock.wpsUploadDelay = 0

    // W6-C：noop 是“无需写入”，不是成功更新 N 行
    await runUploadFlow({
      ok: true, status: 'noop', code: '', reason: '', next_action: '',
      operation_id: 'op-mock-noop',
      planned_summary: { kind: 'plan', rows: { to_update: 0, to_append: 0, unchanged: 1, skipped: 0, warned: 0 } },
      execution_summary: {
        kind: 'execution', status: 'noop', executed: true, counts_source: 'apply_plan',
        sheets: { total: 1, verified: 0, noop: 1, failed: 0, uncertain: 0, skipped: 0, blocked: 0, other: 0 },
        rows: { verified: 0, failed: 0, uncertain: 0, skipped: 0, planned: 0 },
        rows_unknown: false, proven_no_write: false, written_sheets: 0, failed_sheets: 0,
      },
    })
    const noopText = await cdp.eval('document.body.innerText')
    record('W6 noop 显示“无需写入”而不是上传完成', noopText.includes('本次无需写入') && !noopText.includes('上传完成'), '')

    // W6-D：0 次写入（服务端证明零写入）
    await runUploadFlow({
      ok: false, status: 'rejected', code: 'wps_disabled', reason: '云文档同步未启用',
      next_action: '在「云文档同步」中开启后再试', operation_id: '',
      executed: false,
      planned_summary: { kind: 'plan', rows: { to_update: 1, to_append: 0, unchanged: 0, skipped: 0, warned: 0 } },
      execution_summary: {
        kind: 'execution', status: 'rejected', executed: false, counts_source: 'rejected_before_write',
        sheets: { total: 1, verified: 0, noop: 0, failed: 0, uncertain: 0, skipped: 0, blocked: 0, other: 0 },
        rows: { verified: 0, failed: 0, uncertain: 0, skipped: 0, planned: 1 },
        rows_unknown: false, proven_no_write: true, written_sheets: 0, failed_sheets: 0,
      },
    })
    const rejectedText = await cdp.eval('document.body.innerText')
    record(
      'W6 零写入：明确“未写入任何内容”且不显示已核实行数',
      rejectedText.includes('本次未写入任何内容') && !/已核实写入\s*\d+\s*行/.test(rejectedText),
      '',
    )
    record(
      'W6 状态 wps_disabled：说明开关语义与下一步',
      rejectedText.includes('云文档同步已关闭') && rejectedText.includes('开启'),
      nearText(rejectedText, '云文档同步已关闭'),
    )

    // W6-E：持久化失败（journal 写入失败）保持阻断
    await runUploadFlow({
      ok: false, status: 'blocked', code: 'journal_write_failed',
      reason: 'journal_save_failed: 磁盘只读', next_action: '先修复本地日志后重新只读核对',
      operation_id: 'op-mock-journal',
      execution_summary: {
        kind: 'execution', status: 'blocked', executed: false, counts_source: 'unknown_after_exception',
        sheets: { total: 1, verified: 0, noop: 0, failed: 0, uncertain: 0, skipped: 0, blocked: 1, other: 0 },
        rows: { verified: null, failed: null, uncertain: null, skipped: null, planned: 1 },
        rows_unknown: true, proven_no_write: false, written_sheets: null, failed_sheets: null,
      },
    })
    const journalText = await cdp.eval('document.body.innerText')
    record(
      'W6 持久化失败：说明不生效/不能当成功，并保持阻断',
      journalText.includes('本地日志持久化失败') && journalText.includes('不能当作成功')
        && journalText.includes('阻断保持'),
      nearText(journalText, '本地日志持久化失败'),
    )

    // ---------- 5c. 预览失败：wps_disabled / journal 版本不受支持 ----------
    enterScenario('5c 预览失败')
    mock.wpsPreview = {
      ok: false, status: 'rejected', code: 'wps_disabled', reason: '云文档同步未启用',
      next_action: '在「云文档同步」中开启后再试',
    }
    await ensureCloudTab()
    assert.ok(await cdp.eval(clickButtonJs('生成只读预览')) || await cdp.eval(clickButtonJs('重新预览')), '找不到预览按钮')
    await cdp.waitFor(`document.body.innerText.includes('云文档同步已关闭')`)
    const previewDisabledText = await cdp.eval('document.body.innerText')
    record(
      '预览 wps_disabled：不读云端/不发令牌 + 下一步开关',
      previewDisabledText.includes('云文档同步已关闭')
        && previewDisabledText.includes('不读云端、不建计划、不发一次性令牌')
        && previewDisabledText.includes('本次没有写入任何云端内容'),
      '',
    )

    mock.wpsPreview = {
      ok: false, status: 'blocked', code: 'local_state_blocked',
      reason: '本地账本/意图日志不可用：不支持的意图日志版本',
      next_action: '先修复本地日志/账本',
    }
    assert.ok(await cdp.eval(clickButtonJs('生成只读预览')) || await cdp.eval(clickButtonJs('重新预览')), '找不到预览按钮')
    await cdp.waitFor(`document.body.innerText.includes('本地日志版本不受支持')`)
    const previewJournalText = await cdp.eval('document.body.innerText')
    record(
      '预览 journal 版本不受支持：单独文案 + 不提供删除日志捷径',
      previewJournalText.includes('本地日志版本不受支持')
        && previewJournalText.includes('不要删除日志')
        && previewJournalText.includes('不要删除日志，也不要直接重传'),
      '',
    )

    mock.wpsPreview = {
      ok: false, status: 'blocked', code: 'local_state_blocked',
      reason: '本地账本/意图日志不可用：意图日志 JSON 损坏',
      next_action: '先修复本地日志/账本',
    }
    assert.ok(await cdp.eval(clickButtonJs('生成只读预览')) || await cdp.eval(clickButtonJs('重新预览')), '找不到预览按钮')
    await cdp.waitFor(`document.body.innerText.includes('本地账本/意图日志损坏或不可读')`)
    const previewCorruptText = await cdp.eval('document.body.innerText')
    record(
      '预览 journal 损坏：区分于版本不受支持',
      previewCorruptText.includes('本地账本/意图日志损坏或不可读') && !previewCorruptText.includes('版本不受支持'),
      '',
    )

    // ---------- 5d. blocked_concurrent：不是失败，也不需要站内对账 ----------
    enterScenario('5d blocked_concurrent')
    mock.operationStatus = {
      ...idleOperation(), status: 'blocked_concurrent', active: false, mode: 'sss',
      mode_label: '闪时送下单',
      reason: '另一个任务正在运行，请等待后刷新（本次未发送任何下单请求）',
      next_action: '另一进程正在处理同一批次或跨进程锁不可用；未发送任何 POST，请等待锁释放后重试',
    }
    await reloadApp(cdp)
    await ensureCloudTab()
    await cdp.waitFor(`document.body.innerText.includes('另一个任务正在运行')`, 5000)
    const concurrentState = await cdp.eval(`({
      text: document.body.innerText,
      tone: (() => {
        const nodes = [...document.querySelectorAll('*')].filter((el) => (el.textContent || '').includes('另一个任务正在运行'))
        return nodes.length
      })(),
    })`)
    record(
      'blocked_concurrent 显示“另一个任务正在运行/等待后刷新”',
      concurrentState.text.includes('另一个任务正在运行') && concurrentState.text.includes('请等待后刷新'),
      nearText(concurrentState.text, '另一个任务正在运行') + ' | ' + nearText(concurrentState.text, '请等待后刷新'),
    )
    record(
      'blocked_concurrent 不落入失败/待核对分支',
      !concurrentState.text.includes('闪时送下单失败') && !concurrentState.text.includes('任务被阻断 · 待核对'),
      '',
    )
    mock.operationStatus = null
    await reloadApp(cdp)

    // ---------- 5g. FE-1 / R6-8：权威操作状态 uncertain 不得被渲染成「已完成」 ----------
    enterScenario('5g FE-1 uncertain 权威状态')
    // 缺口来源：docs/OPTIMIZATION-FINAL-ACCEPTANCE-R7.md §5.4（任务 T4）——
    // 把 lib/operationStatus.ts 的 uncertain 分支改成 success/“已完成”后，前端单测会失败，
    // 但浏览器检查仍然 95 PASS。这里用 mock 后端返回**权威** status=uncertain，
    // 断言真实渲染出来的状态胶囊/详情/权威状态卡片与真实交互（点击 + 请求计数），
    // 不检查任何静态源码字符串。
    const dangerousCalls = () => mock.wpsUploadCalls + mock.startOrderCalls + mock.startSssCalls
    const uncertainOperation = (mode) => ({
      ...idleOperation(),
      status: 'uncertain', active: false, phase: 'finished', mode,
      operation_id: `op-mock-${mode}-uncertain`,
      // 故意留空 next_action / reason：页面必须自己渲染 uncertain 的安全文案，
      // 否则这条断言只是在测 mock 的字符串，抓不到 operationStatus.ts 的回归。
      reason: '', next_action: '',
      summary: { status: 'uncertain' },
      started_at: '2026-09-20T09:00:00', finished_at: '2026-09-20T09:05:00',
    })
    const misleadingCompletion = ['已完成', '上传完成', '处理完成', '下单完成', '写入完成']
    const oneClickRerun = /再次上传|重新上传|重试上传|直接重传|重新下单|再次下单|重跑|再次执行|重新执行/

    // A) 闪时送下单：权威 uncertain 必须在默认页签上显式可读
    mock.operationStatus = uncertainOperation('sss')
    mock.wpsUploadCalls = 0
    mock.startOrderCalls = 0
    mock.startSssCalls = 0
    await reloadApp(cdp)
    const uncertainSurface = await cdp.eval(operationSurfaceJs)
    record(
      'FE-1 uncertain 权威状态渲染“结果不确定 · 待核对”（真实 DOM）',
      // 断言名与变异场景绑定，保持不变；条件按批 2 收紧：
      // 保留的权威状态区必须仍显示「待核对」，且页签内**不再**有重复状态块、
      // 说明不被截断（原来这里要求 callouts.length > 0，即「重复块存在」）。
      Boolean(uncertainSurface) && uncertainSurface.label.includes('结果不确定')
        && uncertainSurface.label.includes('待核对')
        && uncertainSurface.callouts.length === 0
        && uncertainSurface.detailClipped === false,
      JSON.stringify(uncertainSurface),
    )
    record(
      'FE-1 uncertain 权威状态渲染“不要重试/重跑”防重复提示',
      Boolean(uncertainSurface) && /不要.{0,4}(重试|重跑)/.test(uncertainSurface.surfaceText),
      JSON.stringify(uncertainSurface?.surfaceText || ''),
    )
    const uncertainPageText = await cdp.eval('document.body.innerText')
    record(
      'FE-1 uncertain 权威状态区不出现“已完成/成功”完成文案',
      Boolean(uncertainSurface) && !/已完成|成功/.test(uncertainSurface.surfaceText),
      JSON.stringify(uncertainSurface?.surfaceText || ''),
    )
    record(
      'FE-1 uncertain 整页不出现“已完成/上传完成”等误导性完成文案',
      misleadingCompletion.every((word) => !uncertainPageText.includes(word)),
      `hits=${JSON.stringify(misleadingCompletion.filter((word) => uncertainPageText.includes(word)))} :: ${nearText(uncertainPageText, '任务工作台', 180)}`,
    )
    await captureScreenshot(cdp, 'fe-fe1-authority-uncertain-390x844.png')

    // 状态区之外的第二处「完成暗示」：流程条的「结果」步骤。
    // uncertain 必须停在待核对步骤（渲染序号），不能画成打勾的完成步骤。
    const flowResultStep = await cdp.eval(flowResultStepJs)
    record(
      'FE-1 uncertain 流程条“结果”步骤不显示成功勾选（仍是待核对步骤）',
      Boolean(flowResultStep) && flowResultStep.hasCheckIcon === false && /结果$/.test(flowResultStep.step),
      JSON.stringify(flowResultStep),
    )

    // 日志面板开合：常驻 DOM 的 role=dialog 不能靠 rect 判可见（R6-8 收尾要求 4）。
    const closedLogProbe = await cdp.eval(openDialogProbeJs)
    record(
      'FE-1 弹窗可见性判据：收起的日志面板不算可见弹窗（尺寸/visibility/display/opacity/inert/命中测试）',
      closedLogProbe === null,
      JSON.stringify(closedLogProbe),
    )
    const toggleOnce = await cdp.eval(logFabClickJs)
    await new Promise((r) => setTimeout(r, 600))
    const openLogProbe = await cdp.eval(openDialogProbeJs)
    record(
      'FE-1 uncertain 日志面板展开后仍然显示不确定状态（不显示已完成）',
      toggleOnce === true && openLogProbe?.count === 1 && openLogProbe.text.includes('运行日志')
        && openLogProbe.text.includes('结果不确定') && openLogProbe.text.includes('待核对')
        && !openLogProbe.text.includes('已完成'),
      JSON.stringify(openLogProbe?.text?.slice(0, 160) || null),
    )
    record(
      'FE-1 弹窗可见性判据：日志面板展开时不会被误判成确认弹窗（找不到危险确认按钮）',
      openLogProbe?.count === 1
        && !(openLogProbe.buttons || []).some((item) => /确认开始处理|确认上传|确认正式下单|停止任务/.test(item.label)),
      JSON.stringify((openLogProbe?.buttons || []).map((item) => item.label)),
    )
    await captureScreenshot(cdp, 'fe-fe1-log-sheet-uncertain-390x844.png')
    const toggleTwice = await cdp.eval(logFabClickJs)
    await new Promise((r) => setTimeout(r, 700))
    const reclosedLogProbe = await cdp.eval(openDialogProbeJs)
    record(
      'FE-1 弹窗可见性判据：日志面板收起后不再被判为可见弹窗',
      toggleTwice === true && reclosedLogProbe === null,
      JSON.stringify(reclosedLogProbe),
    )

    // 视口矩阵：手机窄屏 / 横屏 / 更窄，都必须是待核对而不是完成。
    for (const [label, width, height] of [
      ['390x844 窄屏', 390, 844], ['800x450 横屏', 800, 450], ['360x800 更窄', 360, 800],
    ]) {
      await cdp.viewport(width, height)
      const surface = await cdp.eval(operationSurfaceJs)
      const layout = await cdp.eval(`({
        sw: document.documentElement.scrollWidth,
        iw: window.innerWidth,
        text: document.body.innerText,
        buttons: [...document.querySelectorAll('button')].filter((el) => {
          const r = el.getBoundingClientRect(); const s = getComputedStyle(el)
          return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'
        }).map((el) => (el.textContent || '').replace(/\\s+/g, ' ').trim()),
      })`)
      record(
        `FE-1 uncertain ${label}：显示“结果不确定 · 待核对 + 不要重试/重跑”且无完成文案/无横向溢出`,
        Boolean(surface) && surface.label.includes('结果不确定') && surface.label.includes('待核对')
          && /不要.{0,4}(重试|重跑)/.test(surface.surfaceText)
          && misleadingCompletion.every((word) => !layout.text.includes(word))
          && layout.sw <= layout.iw + 1
          && !layout.buttons.some((text) => oneClickRerun.test(text)),
        `${JSON.stringify(surface)} :: sw=${layout.sw} iw=${layout.iw}`,
      )
      if (String(label).includes('横屏')) await captureScreenshot(cdp, 'fe-fe1-uncertain-landscape-800x450.png')
    }
    await cdp.viewport(390, 844)
    await new Promise((r) => setTimeout(r, 250))

    const uncertainButtons = await cdp.eval(visibleButtonsJs)
    const rerunLabels = (uncertainButtons || [])
      .map((item) => item.label)
      .filter((label) => oneClickRerun.test(label))
    record(
      'FE-1 uncertain 页面不存在“再次上传/重跑”等一键重跑按钮',
      rerunLabels.length === 0,
      JSON.stringify(rerunLabels),
    )

    // 真实交互：把订单表单补齐到可提交，单击主按钮后必须**没有**任何危险请求，
    // 危险执行仍然被「确认弹窗 + 必须勾选的明确确认句」挡住。
    await openOrderPasswordAndSet('draft-uncertain-order')
    const primaryBeforeClick = await cdp.eval(primaryDockButtonJs)
    const dangerousBeforeClick = dangerousCalls()
    assert.ok(primaryBeforeClick?.count === 1, `底部主按钮不唯一：${JSON.stringify(primaryBeforeClick)}`)
    assert.ok(
      await cdp.eval(clickByTextJs('button', primaryBeforeClick.label)),
      `找不到主按钮 ${primaryBeforeClick.label}`,
    )
    await new Promise((r) => setTimeout(r, 450))
    const dangerousAfterClick = dangerousCalls()
    record(
      'FE-1 uncertain 单击主要动作不触发危险执行（0 次下单/上传请求）',
      dangerousAfterClick - dangerousBeforeClick === 0,
      `primary=${JSON.stringify(primaryBeforeClick)} delta=${dangerousAfterClick - dangerousBeforeClick}`,
    )
    const confirmProbe = await cdp.eval(openDialogProbeJs)
    assert.ok(confirmProbe, '单击主按钮后没有出现二次确认弹窗')
    const dangerConfirm = (confirmProbe.buttons || [])
      .find((item) => /确认开始处理|确认正式下单|开始预检|开始模拟/.test(item.label))
    assert.ok(dangerConfirm, `确认弹窗里找不到危险确认按钮：${JSON.stringify(confirmProbe)}`)
    await cdp.eval(clickDialogButtonJs(dangerConfirm.label))
    await new Promise((r) => setTimeout(r, 300))
    record(
      'FE-1 uncertain 危险执行按钮在勾选明确确认句之前不可点击',
      dangerConfirm.disabled === true && dangerousCalls() === dangerousBeforeClick,
      `danger=${JSON.stringify(dangerConfirm)} delta=${dangerousCalls() - dangerousBeforeClick} dialog=${JSON.stringify(confirmProbe.text.slice(0, 120))}`,
    )
    const ackChecked = await cdp.eval(clickDialogCheckboxJs)
    await new Promise((r) => setTimeout(r, 250))
    const dangerAfterAck = (await cdp.eval(openDialogProbeJs))?.buttons
      ?.find((item) => item.label === dangerConfirm.label) || null
    record(
      'FE-1 uncertain 勾选明确确认句后危险按钮才可用（对照组，证明前面确实被闸门挡住）',
      ackChecked === true && dangerAfterAck?.disabled === false,
      JSON.stringify({ ackChecked, dangerAfterAck }),
    )
    assert.ok(await cdp.eval(clickDialogButtonJs('返回检查')), '找不到返回检查按钮')
    await new Promise((r) => setTimeout(r, 300))
    record(
      'FE-1 uncertain 关闭确认弹窗后危险请求仍为 0',
      dangerousCalls() === dangerousBeforeClick,
      `delta=${dangerousCalls() - dangerousBeforeClick}`,
    )

    // A2) 停止任务：不确定状态下不可点；运行中必须显式二次确认才提交
    mock.stopTaskCalls = 0
    const stopWhileUncertain = await cdp.eval(stopButtonJs)
    await cdp.eval(`(() => { const el = [...document.querySelectorAll('button')].find((node) => (node.textContent || '').trim() === '停止'); if (el) el.click(); return true })()`)
    await new Promise((r) => setTimeout(r, 300))
    record(
      'FE-1 uncertain 状态下“停止”不可点击且不发 stop_task',
      stopWhileUncertain.found === true && stopWhileUncertain.disabled === true && mock.stopTaskCalls === 0,
      JSON.stringify({ stopWhileUncertain, stopTaskCalls: mock.stopTaskCalls }),
    )

    mock.operationStatus = {
      ...idleOperation(), status: 'running', active: true, mode: 'sss', phase: 'running',
      operation_id: 'op-mock-running-sss', reason: '正在执行闪时送下单', next_action: '',
    }
    mock.stopTaskCalls = 0
    await reloadApp(cdp)
    await new Promise((r) => setTimeout(r, 400))
    // 运行中手机端会自动展开日志面板（App.tsx openRemembered）；先复核它的开合，再收起，
    // 避免展开的日志层遮住底栏。
    const autoLogOpened = await cdp.waitFor(
      `(() => { const sheet = document.getElementById('phone-log-sheet'); return Boolean(sheet) && !sheet.hasAttribute('inert') })()`,
      4000,
    )
    if (autoLogOpened) {
      assert.ok(await cdp.eval(logFabClickJs), '找不到日志开合按钮')
      await new Promise((r) => setTimeout(r, 800))
    }
    const logClosedAfterRunning = await cdp.eval(openDialogProbeJs)
    record(
      'FE-1 running 日志面板自动展开后能收起，收起后不再被判为可见弹窗',
      autoLogOpened === true && logClosedAfterRunning === null,
      JSON.stringify({ autoLogOpened, closedProbe: logClosedAfterRunning }),
    )
    const stopWhileRunning = await cdp.eval(stopButtonJs)
    assert.ok(await cdp.eval(clickByTextJs('button', '停止')), '找不到停止按钮')
    await new Promise((r) => setTimeout(r, 400))
    const stopDialog = await cdp.eval(openDialogProbeJs)
    record(
      'FE-1 running 对照：单击“停止”只打开显式二次确认（0 次 stop_task）',
      stopWhileRunning.found === true && stopWhileRunning.disabled === false
        && mock.stopTaskCalls === 0 && stopDialog?.text.includes('停止当前任务') === true,
      JSON.stringify({ stopWhileRunning, stopTaskCalls: mock.stopTaskCalls, dialog: stopDialog?.text?.slice(0, 80) || null }),
    )
    assert.ok(await cdp.eval(clickDialogButtonJs('停止任务')), '找不到弹窗内的停止任务按钮')
    await cdp.waitFor('document.body.innerText.includes("停止当前任务") === false', 5000)
    await new Promise((r) => setTimeout(r, 400))
    record(
      'FE-1 running 只有弹窗内显式确认才提交停止（且只提交一次）',
      mock.stopTaskCalls === 1,
      `stopTaskCalls=${mock.stopTaskCalls}`,
    )

    // A3) 错误恢复流程：operation_status 断线 → 明确错误提示 → 重新连接 → 回到待核对
    mock.operationStatus = uncertainOperation('wps_upload')
    enterScenario('5g 断线恢复：operation_status 网络失败（故意断网）')
    mock.operationStatusNetwork = true
    mock.wpsUploadCalls = 0
    mock.startOrderCalls = 0
    mock.startSssCalls = 0
    mock.stopTaskCalls = 0
    await reloadApp(cdp)
    const reconnectAppeared = await cdp.waitFor(`document.body.innerText.includes('重新连接')`, 25000)
    const errorRecoveryText = await cdp.eval('document.body.innerText')
    record(
      'FE-1 uncertain 断线错误恢复：operation_status 失败时给出明确错误与“重新连接”，不显示成功',
      reconnectAppeared === true && errorRecoveryText.includes('重新连接')
        && !errorRecoveryText.includes('已完成') && dangerousCalls() === 0,
      `${nearText(errorRecoveryText, '重新连接')} :: dangerous=${dangerousCalls()}`,
    )
    await captureScreenshot(cdp, 'fe-fe1-error-recovery-390x844.png')
    mock.operationStatusNetwork = false
    assert.ok(await cdp.eval(clickByTextJs('button', '重新连接')), '找不到重新连接按钮')
    const recoveredToUncertain = await cdp.waitFor(`(() => {
      const h1 = [...document.querySelectorAll('h1')].find((el) => (el.textContent || '').includes('任务工作台'))
      const pill = h1?.parentElement?.parentElement?.lastElementChild
      return Boolean(pill && (pill.textContent || '').includes('结果不确定'))
    })()`, 12000)
    await new Promise((r) => setTimeout(r, 600))
    const recoveredSurface = await cdp.eval(operationSurfaceJs)
    record(
      'FE-1 uncertain 断线恢复后回到“结果不确定 · 待核对”，且不会自动重传',
      recoveredToUncertain === true && Boolean(recoveredSurface)
        && recoveredSurface.label.includes('结果不确定') && recoveredSurface.label.includes('待核对')
        && !recoveredSurface.surfaceText.includes('已完成')
        && dangerousCalls() === 0 && mock.stopTaskCalls === 0,
      JSON.stringify({ recoveredSurface, dangerous: dangerousCalls(), stopTaskCalls: mock.stopTaskCalls }),
    )

    // B) 云文档上传：权威 uncertain + 一次以 uncertain 结束的真实上传
    mock.operationStatus = uncertainOperation('wps_upload')
    const uncertainUpload = {
      ok: false, status: 'uncertain', code: '', reason: '可能已写入但无法确认',
      next_action: '只读核对云端与日志，不要重新上传',
      operation_id: 'op-mock-uncertain-live', uncertain: true, verification_missing: true,
      planned_summary: { kind: 'plan', rows: { to_update: 1, to_append: 0, unchanged: 0, skipped: 0, warned: 0 } },
      execution_summary: {
        kind: 'execution', status: 'uncertain', executed: true, counts_source: 'apply_plan',
        sheets: { total: 1, verified: 0, noop: 0, failed: 0, uncertain: 1, skipped: 0, blocked: 0, other: 0 },
        rows: { verified: null, failed: null, uncertain: null, skipped: null, planned: 1 },
        rows_unknown: true, proven_no_write: false, written_sheets: null, failed_sheets: null,
      },
    }
    await reloadApp(cdp)
    await ensureCloudTab()
    const cloudUncertainSurface = await cdp.eval(operationSurfaceJs)
    record(
      'FE-1 uncertain 云文档页签同样渲染“结果不确定 · 待核对”',
      Boolean(cloudUncertainSurface) && cloudUncertainSurface.label.includes('结果不确定')
        && cloudUncertainSurface.label.includes('待核对'),
      JSON.stringify(cloudUncertainSurface),
    )
    const dangerousBeforeUpload = dangerousCalls()
    await runUploadFlow(uncertainUpload)
    const dangerousAfterUpload = dangerousCalls()
    record(
      'FE-1 uncertain 前置条件：这次上传确实发生且只发生一次',
      dangerousAfterUpload - dangerousBeforeUpload === 1,
      `delta=${dangerousAfterUpload - dangerousBeforeUpload}`,
    )
    const uploadUncertainAttrs = await cdp.eval(resultAttrsJs)
    const buttonsAfterUncertain = await cdp.eval(visibleButtonsJs)
    const primaryAfterUncertain = await cdp.eval(primaryDockButtonJs)
    const enabledConfirmUpload = (buttonsAfterUncertain || [])
      .filter((item) => item.label === '确认上传' && item.disabled === false)
    record(
      'FE-1 uncertain 上传结束后“确认上传”危险按钮不再可用（必须先重新预览）',
      enabledConfirmUpload.length === 0
        && primaryAfterUncertain.count === 1
        && primaryAfterUncertain.disabled === false
        && !primaryAfterUncertain.label.includes('上传'),
      JSON.stringify({ primary: primaryAfterUncertain, enabledConfirmUpload, attrs: uploadUncertainAttrs }),
    )
    const afterUncertainText = await cdp.eval('document.body.innerText')
    record(
      'FE-1 uncertain 主要动作指向核对/只读刷新，而不是“再次上传”',
      !oneClickRerun.test((buttonsAfterUncertain || []).map((item) => item.label).join('|'))
        && (afterUncertainText.includes('重新核对') || afterUncertainText.includes('只读核对'))
        && !afterUncertainText.includes('上传完成'),
      JSON.stringify({
        labels: (buttonsAfterUncertain || []).map((item) => item.label),
        primary: primaryAfterUncertain.label,
      }),
    )
    const dangerousBeforeRepreview = dangerousCalls()
    assert.ok(
      await cdp.eval(clickByTextJs('button', primaryAfterUncertain.label)),
      `找不到重新预览按钮 ${primaryAfterUncertain.label}`,
    )
    await new Promise((r) => setTimeout(r, 700))
    record(
      'FE-1 uncertain 单击主要动作只做只读预览，不重新上传（危险请求 0 次）',
      dangerousCalls() - dangerousBeforeRepreview === 0,
      `primary=${primaryAfterUncertain.label} delta=${dangerousCalls() - dangerousBeforeRepreview} previewCalls=${mock.wpsPreviewCalls}`,
    )
    const primaryAfterRepreview = await cdp.eval(primaryDockButtonJs)
    const dangerousBeforeSecondClick = dangerousCalls()
    if (primaryAfterRepreview.count === 1) {
      await cdp.eval(clickByTextJs('button', primaryAfterRepreview.label))
      await new Promise((r) => setTimeout(r, 400))
    }
    const reuploadDialog = await cdp.eval(openDialogProbeJs)
    record(
      'FE-1 uncertain 重新预览后仍需显式确认句才能上传（单击主按钮 0 次上传）',
      primaryAfterRepreview.count === 1 && primaryAfterRepreview.label === '确认上传'
        && dangerousCalls() - dangerousBeforeSecondClick === 0
        && reuploadDialog?.buttons?.some((item) => item.label === '确认上传' && item.disabled === true) === true,
      `primary=${JSON.stringify(primaryAfterRepreview)} delta=${dangerousCalls() - dangerousBeforeSecondClick} dialog=${JSON.stringify(reuploadDialog?.buttons || null)}`,
    )
    if (reuploadDialog) {
      assert.ok(await cdp.eval(clickDialogButtonJs('返回检查')), '找不到返回检查按钮')
      await new Promise((r) => setTimeout(r, 300))
    }
    const dangerousBeforeReload = dangerousCalls()
    await reloadApp(cdp)
    await ensureCloudTab()
    await new Promise((r) => setTimeout(r, 800))
    const reloadedUncertainSurface = await cdp.eval(operationSurfaceJs)
    record(
      'FE-1 uncertain 刷新页面不会自动重传（危险请求 0 次）',
      dangerousCalls() - dangerousBeforeReload === 0,
      `delta=${dangerousCalls() - dangerousBeforeReload} uploadCalls=${mock.wpsUploadCalls}`,
    )
    record(
      'FE-1 uncertain 刷新后权威状态仍是“结果不确定 · 待核对”',
      Boolean(reloadedUncertainSurface) && reloadedUncertainSurface.label.includes('结果不确定')
        && reloadedUncertainSurface.label.includes('待核对'),
      JSON.stringify(reloadedUncertainSurface),
    )

    // 复位：不能把本节的 mock 状态泄漏给后面的 W3 恢复流程。
    mock.operationStatus = null
    mock.operationStatusNetwork = false
    mock.wpsPreview = null
    mock.wpsUpload = null
    mock.wpsUploadDelay = 0
    await reloadApp(cdp)

    // ---------- 5e. W3：管理员恢复流程 ----------
    enterScenario('5e W3 管理员恢复')
    const recoveryOp = {
      operation_id: 'wps-1234567890abcdef', operation_ref: 'wps-op:abcdef123456',
      status: 'uncertain', pending: true, cloud_checked: true,
      created_at: '2026-09-20T09:00:00', updated_at: '2026-09-20T09:01:00',
      target_date: '2026-09-20', target_refs: ['wps-target:abc123def456'], sheet_count: 1,
      error_code: 'wps_recovery_uncertain', allowed_next_actions: ['manual_reconcile'],
      manual_required: true,
      sheets: [{
        target_date: '2026-09-20', target_ref: 'wps-target:abc123def456',
        status: 'uncertain', raw_status: 'uncertain', error_code: 'wps_recovery_uncertain',
        allowed_next_actions: ['manual_reconcile'], manual_required: true,
        cloud_checked: true, evidence: 'journal+cloud_read',
      }],
    }
    const adminRecoveryStatus = {
      ok: true, contract_version: 1, source: 'local_journal', read_only: true,
      queried_cloud: false, contains_cloud_checked_records: true, scope: 'admin',
      counts: { planned: 0, writing: 0, ledger_pending: 0, uncertain: 1, verified: 0, failed: 0, not_started: 0, retired_guarded: 0 },
      next_action: 'manual_reconcile', error_code: 'wps_recovery_uncertain',
      summary: {
        operation_count: 1, pending_count: 1, uncertain_count: 1, failed_count: 0,
        not_started_count: 0, retired_guarded_count: 0, has_pending: true, needs_review: true,
        guidance: '存在不确定结果：请先只读核对云端与日志，不要直接重传',
      },
      operations: [recoveryOp], pending_operations: [recoveryOp],
    }
    mock.wpsRecovery = adminRecoveryStatus
    mock.wpsResolveCalls = 0
    mock.wpsResolveMaxActive = 0
    mock.wpsResolvePayloads = []
    mock.wpsResolveTimes = []
    mock.wpsResolveResult = null
    mock.wpsResolveDelay = 0
    mock.wpsResolveNetwork = false
    mock.wpsResolveForbidden = false

    // 非管理员：没有恢复处置入口，也无法提交
    mock.isAdmin = false
    await reloadApp(cdp)
    assert.ok(await ensureCloudPanel(cdp, '待核对批次（只读可见）'), '云文档页签/恢复卡片未出现')
    const nonAdminState = await cdp.eval(`({
      text: document.body.innerText,
      resolveButtons: [...document.querySelectorAll('button')].filter((b) => (b.textContent || '').includes('管理员恢复处置')).length,
    })`)
    record(
      'W3 非管理员看不到恢复处置入口',
      nonAdminState.resolveButtons === 0
        && nonAdminState.text.includes('当前账号没有恢复处置权限')
        && !nonAdminState.text.includes('删除账本'),
      `resolveButtons=${nonAdminState.resolveButtons} :: ` + nearText(nonAdminState.text, '当前账号没有恢复处置权限')
        + ' :: hasDeleteLedger=' + nonAdminState.text.includes('删除账本'),
    )
    record('W3 非管理员刷新不会调用恢复写入', mock.wpsResolveCalls === 0, `resolveCalls=${mock.wpsResolveCalls}`)

    // 管理员：完整流程
    mock.isAdmin = true
    await reloadApp(cdp)
    assert.ok(await ensureCloudPanel(cdp, '待核对批次（只读可见）'), '云文档页签/恢复卡片未出现')
    const callsAfterRefresh = mock.wpsResolveCalls
    record('W3 刷新/加载不会自动调用恢复写入', callsAfterRefresh === 0, `resolveCalls=${callsAfterRefresh}`)
    const panelText = await cdp.eval('document.body.innerText')
    record(
      'W3 面板展示目标日期/操作标识/风险/允许动作',
      documentTextIncludes(panelText, [
        '目标日期 2026-09-20', 'wps-1234567890abcdef', '风险：', 'manual_reconcile', '人工只读核对',
      ]),
      `len=${panelText.length} ` + nearText(panelText, '待核对批次（只读可见）', 320),
    )
    assert.ok(await cdp.eval(clickButtonJs('管理员恢复处置')), '找不到管理员恢复处置按钮')
    await cdp.waitFor(`document.body.innerText.includes('管理员恢复/退场处置')`)
    const dialogProbe = await cdp.eval(`(() => {
      const dialog = document.querySelector('[role="dialog"]')
      const text = document.body.innerText
      const buttons = [...(dialog?.querySelectorAll('button') || [])].map((b) => (b.textContent || '').trim())
      return {
        text,
        buttons,
        decisions: [...(dialog?.querySelectorAll('input[type="radio"]') || [])].length,
      }
    })()`)
    const dialogText = dialogProbe.text
    // 「删除账本/直接重传」只允许出现在否定说明里，绝不允许作为可点按钮存在。
    const shortcutButtons = (dialogProbe.buttons || []).filter((label) => /删除账本|删除日志|直接重传|强制清除|重新上传|重传/.test(label))
    record(
      'W3 弹窗展示目标日期/操作标识/风险/允许动作/无捷径',
      documentTextIncludes(dialogText, [
        '目标日期：', '2026-09-20', 'wps-1234567890abcdef', '操作引用：', '风险：',
        '允许动作：', '退场不会解除同一目标日期',
      ])
        && dialogText.includes('没有「删除账本」或「直接重传」的捷径')
        && shortcutButtons.length === 0
        && dialogProbe.decisions >= 2,
      `shortcutButtons=${JSON.stringify(shortcutButtons)} decisions=${dialogProbe.decisions}`,
    )
    const dialogFocus = await cdp.eval(`!!document.activeElement?.closest?.('[role="dialog"]')`)
    record('W3 弹窗打开后焦点在弹窗内', dialogFocus === true)
    await captureScreenshot(cdp, 'fe-w3-recovery-resolve-390x844.png')

    // 本地校验：备注过短不提交
    assert.ok(await cdp.eval(clickDialogRadioJs('keep')), '找不到 keep 决策单选项')
    await new Promise((r) => setTimeout(r, 120))
    assert.ok(await cdp.eval(setDialogInputJs('#wps-resolve-note', '短')), '无法写入备注')
    assert.ok(await cdp.eval(setDialogInputJs('#wps-resolve-confirm', 'keep')), '无法写入确认串')
    assert.ok((await cdp.eval(clickDialogButtonJs('提交处置')))?.ok, '找不到提交处置按钮')
    await new Promise((r) => setTimeout(r, 250))
    const shortNoteText = await cdp.eval('document.body.innerText')
    record(
      'W3 备注过短本地拦截，不发出请求',
      shortNoteText.includes('至少 4 个字') && mock.wpsResolveCalls === 0,
      `resolveCalls=${mock.wpsResolveCalls}`,
    )

    // 逐字确认不匹配
    assert.ok(await cdp.eval(setDialogInputJs('#wps-resolve-note', '已人工只读核对云端排单表')), '无法写入备注')
    assert.ok(await cdp.eval(setDialogInputJs('#wps-resolve-confirm', 'Keep')), '无法写入确认串')
    assert.ok((await cdp.eval(clickDialogButtonJs('提交处置')))?.ok, '找不到提交处置按钮')
    await new Promise((r) => setTimeout(r, 250))
    record(
      'W3 逐字确认不匹配本地拦截',
      (await cdp.eval('document.body.innerText')).includes('请逐字输入') && mock.wpsResolveCalls === 0,
      `resolveCalls=${mock.wpsResolveCalls}`,
    )

    // 选择 retire_guarded：必须勾选“已核对云端表结构”
    assert.ok(await cdp.eval(clickDialogRadioJs('retire_guarded')), '找不到 retire_guarded 单选项')
    await new Promise((r) => setTimeout(r, 150))
    assert.ok(await cdp.eval(setDialogInputJs('#wps-resolve-note', '已人工只读核对云端排单表，仍无法判定')), '无法写入备注')
    assert.ok(await cdp.eval(setDialogInputJs('#wps-resolve-confirm', 'retire_guarded')), '无法写入确认串')
    assert.ok((await cdp.eval(clickDialogButtonJs('提交处置')))?.ok, '找不到提交处置按钮')
    await new Promise((r) => setTimeout(r, 250))
    record(
      'W3 退场未确认已核对表结构时被拦截',
      (await cdp.eval('document.body.innerText')).includes('已人工核对云端表结构') && mock.wpsResolveCalls === 0,
      `resolveCalls=${mock.wpsResolveCalls}`,
    )

    // 正常提交 + 双击保护
    assert.ok(await cdp.eval(clickDialogCheckboxJs), '无法勾选结构确认')
    await new Promise((r) => setTimeout(r, 120))
    mock.wpsResolveDelay = 500
    const burst = await cdp.eval(`(() => {
      const roots = [...document.querySelectorAll('[role="dialog"],[role="alertdialog"]')]
      for (const root of roots) {
        const target = [...root.querySelectorAll('button')].find((el) => !el.disabled && (el.textContent || '').includes('提交处置'))
        if (!target) continue
        target.click(); target.click(); target.click()
        return true
      }
      return false
    })()`)
    assert.ok(burst, '找不到提交处置按钮')
    await cdp.waitFor(`document.body.innerText.includes('恢复处置：')`, 6000)
    await new Promise((r) => setTimeout(r, 400))
    record(
      'W3 双击/连点只提交一次且无并发',
      mock.wpsResolveCalls === 1 && mock.wpsResolveMaxActive === 1,
      `calls=${mock.wpsResolveCalls} maxActive=${mock.wpsResolveMaxActive}`,
    )
    const retiredOutcome = await cdp.eval('document.body.innerText')
    record(
      'W3 退场结果仍保留阻断且不声称可重传',
      retiredOutcome.includes('已带审计退场') && retiredOutcome.includes('阻断仍然保留')
        && retiredOutcome.includes('不会因此变成可以重传') && !retiredOutcome.includes('强制清除'),
      '',
    )
    record(
      'W3 请求体是契约对象（含 confirm/note/confirm_structure_checked）',
      (() => {
        const payload = mock.wpsResolvePayloads[0]
        return payload && payload.operation_id === 'wps-1234567890abcdef'
          && payload.decision === 'retire_guarded'
          && payload.confirm === 'retire_guarded'
          && String(payload.note || '').length >= 4
          && payload.confirm_structure_checked === true
          && !('delete_ledger' in payload) && !('force' in payload)
      })(),
      JSON.stringify(mock.wpsResolvePayloads[0] || null),
    )
    // 权限不足（服务端 403）：保留阻断、不显示成功、不重试
    enterScenario('5e 恢复处置：无权限 403（预期）')
    mock.wpsResolveForbidden = true
    mock.wpsResolveDelay = 0
    const callsBeforeResolveForbidden = mock.wpsResolveCalls
    assert.ok(await cdp.eval(clickButtonJs('管理员恢复处置')), '找不到管理员恢复处置按钮')
    await cdp.waitFor(`document.body.innerText.includes('管理员恢复/退场处置')`)
    assert.ok(await cdp.eval(clickDialogRadioJs('keep')), '找不到 keep 决策单选项')
    assert.ok(await cdp.eval(setDialogInputJs('#wps-resolve-note', '已人工只读核对云端排单表')), '无法写入备注')
    assert.ok(await cdp.eval(setDialogInputJs('#wps-resolve-confirm', 'keep')), '无法写入确认串')
    assert.ok((await cdp.eval(clickDialogButtonJs('提交处置')))?.ok, '找不到提交处置按钮')
    await cdp.waitFor(`document.body.innerText.includes('恢复处置：')`, 6000)
    await new Promise((r) => setTimeout(r, 900))
    const resolveForbiddenText = await cdp.eval('document.body.innerText')
    record(
      'W3 权限不足无法提交恢复：准确权限提示 + 阻断保持 + 不显示成功',
      resolveForbiddenText.includes('当前账号没有该权限')
        && resolveForbiddenText.includes('阻断仍然保留')
        && !resolveForbiddenText.includes('恢复处置：已带审计退场')
        && mock.wpsResolveCalls - callsBeforeResolveForbidden === 1,
      `delta=${mock.wpsResolveCalls - callsBeforeResolveForbidden}`,
    )
    mock.wpsResolveForbidden = false

    // 网络失败：不误报成功、自动不重试、保留阻断
    enterScenario('5e 恢复处置：网络失败（故意断网）')
    mock.wpsResolveNetwork = true
    const callsBeforeNetwork = mock.wpsResolveCalls
    assert.ok(await cdp.eval(clickButtonJs('管理员恢复处置')), '找不到管理员恢复处置按钮')
    await cdp.waitFor(`document.body.innerText.includes('管理员恢复/退场处置')`)
    assert.ok(await cdp.eval(clickDialogRadioJs('keep')), '找不到 keep 决策单选项')
    assert.ok(await cdp.eval(setDialogInputJs('#wps-resolve-note', '已人工只读核对云端排单表')), '无法写入备注')
    assert.ok(await cdp.eval(setDialogInputJs('#wps-resolve-confirm', 'keep')), '无法写入确认串')
    assert.ok((await cdp.eval(clickDialogButtonJs('提交处置')))?.ok, '找不到提交处置按钮')
    await cdp.waitFor(`document.body.innerText.includes('恢复请求结果未知')`, 6000)
    await new Promise((r) => setTimeout(r, 1200))
    const networkText = await cdp.eval('document.body.innerText')
    record(
      'W3 网络失败：结果未知 + 阻断保持 + 不显示成功',
      networkText.includes('恢复请求结果未知（阻断保持）') && networkText.includes('阻断仍然保留')
        && !networkText.includes('恢复处置：已带审计退场'),
      '',
    )
    record(
      'W3 网络失败后不自动重试（只发生一次请求）',
      mock.wpsResolveCalls - callsBeforeNetwork === 1,
      `delta=${mock.wpsResolveCalls - callsBeforeNetwork} maxActive=${mock.wpsResolveMaxActive} times=${JSON.stringify(mock.wpsResolveTimes.slice(-4))}`,
    )
    mock.wpsResolveNetwork = false

    // 幂等重复提交：already_retired / changed=false 不重复写盘
    mock.wpsResolveResult = {
      ok: true, status: 'already_retired', code: 'already_retired', reason: '该批次此前已退场',
      next_action: 'manual_reconcile', cloud_write: false, changed: false, verified_on_disk: true,
      scope: { operation_ref: 'wps-op:abcdef123456', target_dates: ['2026-09-20'], target_refs: [], sheet_count: 1, guard_retained: true, blocking: 'retired_guarded' },
      audit: { actor: 'admin@example.com', at: '2026-09-20T10:05:00', decision: 'retire_guarded', note_recorded: true, duplicate: true, effects: { cloud_written: false, guard_retained: true, blocking: 'retired_guarded', auto_retry_allowed: false } },
    }
    assert.ok(await cdp.eval(clickButtonJs('管理员恢复处置')), '找不到管理员恢复处置按钮')
    await cdp.waitFor(`document.body.innerText.includes('管理员恢复/退场处置')`)
    assert.ok(await cdp.eval(clickDialogRadioJs('keep')), '找不到 keep 决策单选项')
    assert.ok(await cdp.eval(setDialogInputJs('#wps-resolve-note', '已人工只读核对云端排单表')), '无法写入备注')
    assert.ok(await cdp.eval(setDialogInputJs('#wps-resolve-confirm', 'keep')), '无法写入确认串')
    assert.ok((await cdp.eval(clickDialogButtonJs('提交处置')))?.ok, '找不到提交处置按钮')
    await cdp.waitFor(`document.body.innerText.includes('此前已退场')`, 6000)
    const idempotentText = await cdp.eval('document.body.innerText')
    record(
      'W3 重复提交幂等：不重复写盘且阻断保持',
      idempotentText.includes('此前已退场') && idempotentText.includes('没有再次写盘')
        && idempotentText.includes('阻断仍然保留'),
      '',
    )
    mock.wpsResolveResult = null

    // 恢复面板：retired_guarded 计数与专用文案
    mock.wpsRecovery = {
      ...adminRecoveryStatus,
      counts: { ...adminRecoveryStatus.counts, uncertain: 0, retired_guarded: 1 },
      pending_operations: [],
      operations: [{ ...recoveryOp, status: 'retired_guarded', pending: false, error_code: 'wps_recovery_retired_guarded', allowed_next_actions: ['manual_reconcile'] }],
      summary: { ...adminRecoveryStatus.summary, pending_count: 0, uncertain_count: 0, retired_guarded_count: 1, has_pending: false, needs_review: true },
    }
    await reloadApp(cdp)
    assert.ok(await ensureCloudPanel(cdp, '已带审计退场'), 'retired_guarded 卡片未出现')
    const retiredCardText = await cdp.eval('document.body.innerText')
    record(
      'W3 retired_guarded 展示计数与保留闸门文案',
      retiredCardText.includes('已带审计退场（闸门保留）')
        && retiredCardText.includes('仍被防重复闸门阻断')
        && retiredCardText.includes('证实之前不能重新上传'),
      nearText(retiredCardText, '已带审计退场') + ' | ' + nearText(retiredCardText, '待核对批次'),
    )

    // 恢复状态失败关闭：journal 版本不受支持
    mock.wpsRecovery = {
      ok: false, contract_version: 1, source: 'local_journal', read_only: true,
      queried_cloud: false, contains_cloud_checked_records: false, scope: 'admin',
      counts: {}, next_action: 'fix_journal', error_code: 'wps_recovery_journal_unreadable',
      summary: {
        operation_count: 0, pending_count: 0, uncertain_count: 0, failed_count: 0,
        not_started_count: 0, has_pending: false, needs_review: true,
        guidance: '恢复状态不可用：本地账本/意图日志不可用：不支持的意图日志版本',
      },
    }
    await reloadApp(cdp)
    assert.ok(await ensureCloudPanel(cdp, '本地恢复记录不可读'), '恢复记录不可读卡片未出现')
    const journalRecoveryText = await cdp.eval('document.body.innerText')
    record(
      'recovery 状态 journal 版本不受支持：失败关闭 + 明确下一步',
      journalRecoveryText.includes('本地日志版本不受支持')
        && journalRecoveryText.includes('不要删除日志')
        && !journalRecoveryText.includes('本地恢复记录：无待核对批次'),
      `len=${journalRecoveryText.length} text=${JSON.stringify(journalRecoveryText.slice(0, 300))}`,
    )

    // 恢复状态失败关闭：持久化失败
    mock.wpsRecovery = {
      ...mock.wpsRecovery,
      error_code: 'journal_write_failed',
      summary: { ...mock.wpsRecovery.summary, guidance: '本地日志写入失败：journal_save_failed' },
    }
    await reloadApp(cdp)
    assert.ok(await ensureCloudPanel(cdp, '本地恢复记录不可读'), '恢复记录不可读卡片未出现')
    record(
      'recovery 状态持久化失败：说明写入失败与下一步',
      (await cdp.eval('document.body.innerText')).includes('本地日志持久化失败'),
      '',
    )

    // ---------- 5f. 弹窗窄屏/横屏/长错误提示/可达性 ----------
    enterScenario('5f 弹窗窄屏横屏可达性')
    mock.wpsRecovery = adminRecoveryStatus
    mock.isAdmin = true
    await reloadApp(cdp)
    for (const [label, width, height] of [['360x800', 360, 800], ['800x450 landscape', 800, 450]]) {
      await cdp.viewport(width, height)
      assert.ok(await ensureCloudPanel(cdp, '待核对批次（只读可见）'), `云文档页签/恢复卡片未出现 ${label}`)
      if (await cdp.eval(clickButtonJs('管理员恢复处置'))) {
        await cdp.waitFor(`document.body.innerText.includes('管理员恢复/退场处置')`)
        await new Promise((r) => setTimeout(r, 250))
        const dialogLayout = await cdp.eval(`(() => {
          const dialog = document.querySelector('[role="dialog"]')
          const sw = document.documentElement.scrollWidth, iw = window.innerWidth
          const submit = [...(dialog?.querySelectorAll('button') || [])].find((b) => (b.textContent || '').includes('提交处置'))
          const before = submit?.getBoundingClientRect()
          const dr0 = dialog?.getBoundingClientRect()
          // 真实可达性：把弹窗滚到底，再看按钮是否完整落在弹窗可视区内。
          // 不能只判断 overflowY !== 'visible'（对 overflow-y-auto 恒为真，等于没断言）。
          if (dialog) dialog.scrollTop = dialog.scrollHeight
          const after = submit?.getBoundingClientRect()
          const dr = dialog?.getBoundingClientRect()
          return {
            overflow: sw <= iw + 1,
            dialogOverflow: dr ? dr.width <= iw + 1 : false,
            found: !!submit,
            // 滚动前按钮确实在弹窗可视区之外（证明"滚到底"这一步是必需的）
            wasBelowFold: !!(before && dr0 && before.bottom > dr0.bottom + 1),
            reachable: !!(after && dr && after.width > 0 && after.height > 0
              && after.top >= dr.top - 1 && after.bottom <= dr.bottom + 1),
            bottom: after ? after.bottom : -1,
            inner: window.innerHeight,
          }
        })()`)
        record(
          `W3 恢复弹窗 ${label} 无横向溢出且操作区可达（滚到底后按钮完整可见）`,
          dialogLayout.overflow && dialogLayout.dialogOverflow && dialogLayout.found
            && dialogLayout.reachable && dialogLayout.wasBelowFold,
          JSON.stringify(dialogLayout),
        )
        if (label.includes('landscape')) await captureScreenshot(cdp, 'fe-w3-resolve-landscape-800x450.png')
        await cdp.send('Input.dispatchKeyEvent', { type: 'keyDown', key: 'Escape', code: 'Escape', windowsVirtualKeyCode: 27, nativeVirtualKeyCode: 27 })
        await cdp.send('Input.dispatchKeyEvent', { type: 'keyUp', key: 'Escape', code: 'Escape', windowsVirtualKeyCode: 27, nativeVirtualKeyCode: 27 })
        await new Promise((r) => setTimeout(r, 300))
        record(
          `W3 恢复弹窗 ${label} 可用 Escape 关闭`,
          (await cdp.eval(`!document.body.innerText.includes('管理员恢复/退场处置')`)) === true,
          '',
        )
      } else {
        record(`W3 恢复弹窗 ${label} 无横向溢出且操作区可达`, false, '找不到管理员恢复处置按钮')
      }
    }

    // 长错误提示：不得横向溢出
    await cdp.viewport(360, 800)
    mock.wpsRecovery = {
      ...adminRecoveryStatus,
      summary: {
        ...adminRecoveryStatus.summary,
        guidance: '存在不确定结果：' + '请先只读核对云端与日志，不要直接重传；'.repeat(12),
      },
    }
    await reloadApp(cdp)
    assert.ok(await ensureCloudPanel(cdp, '请先只读核对云端与日志'), '超长指引卡片未出现')
    const longTextLayout = await cdp.eval(`(() => {
      const text = document.body.innerText
      return {
        sw: document.documentElement.scrollWidth,
        iw: window.innerWidth,
        repeats: (text.match(/请先只读核对云端与日志/g) || []).length,
        len: text.length,
      }
    })()`)
    // 与第 1 节的布局断言区分：这里额外证明"超长指引确实渲染出来了"，
    // 否则一条普通的无溢出断言与前面重复、等于没测。
    record(
      'W3 超长恢复指引确实渲染（≥6 段重复文案）且不横向溢出（360 窄屏）',
      longTextLayout.repeats >= 6 && longTextLayout.sw <= longTextLayout.iw + 1,
      JSON.stringify(longTextLayout),
    )

    // ---------- 6. 缩小可视视口：底部主按钮仍可达、无横向溢出 ----------
    enterScenario('6 缩小可视视口')
    await cdp.viewport(390, 430)
    await new Promise((r) => setTimeout(r, 300))
    const small = await cdp.eval(`(() => {
      const sw = document.documentElement.scrollWidth, iw = window.innerWidth
      const buttons = [...document.querySelectorAll('button')].filter((b) => {
        const r = b.getBoundingClientRect(); const s = getComputedStyle(b)
        return r.width > 0 && r.height > 0 && s.visibility !== 'hidden'
      })
      const primary = buttons.find((b) => /生成只读预览|开始处理|重新预览|查询操作状态/.test(b.textContent || ''))
      const r = primary?.getBoundingClientRect()
      return { overflow: sw <= iw + 1, found: !!primary, bottom: r ? r.bottom : -1, inner: window.innerHeight, iw }
    })()`)
    // 前置条件：视口必须真的缩小了，否则这条断言会退化成"在 800 高下按钮当然可见"。
    record(
      '缩小可视视口前置条件成立（innerHeight 确实变小）',
      small.inner <= 460 && small.iw <= 400,
      JSON.stringify(small),
    )
    record('缩小可视视口后主按钮/输入区仍可达且无横向溢出', small.overflow && small.found && small.bottom <= small.inner + 1, JSON.stringify(small))

    // 长错误提示不横向溢出
    const longOverflow = await cdp.eval(`(() => {
      const sw = document.documentElement.scrollWidth, iw = window.innerWidth
      return sw <= iw + 1
    })()`)
    record('长页面/错误提示无横向溢出', longOverflow === true)

    // 焦点进入弹窗并可用 Escape 关闭/焦点恢复
    mock.clearResult = 'fail'
    await cdp.viewport(390, 844)
    assert.ok(await cdp.eval(clickByTextJs('button', '订单处理')), '找不到订单处理页签')
    await new Promise((r) => setTimeout(r, 200))
    if (!(await cdp.eval(`!!document.querySelector('#order-password')`))) {
      await cdp.eval(clickByTextJs('button', '管理网址与登录凭据'))
      await new Promise((r) => setTimeout(r, 200))
    }
    await cdp.eval(clickByTextJs('button', '更多'))
    await new Promise((r) => setTimeout(r, 200))
    await cdp.eval(clickByTextJs('[role="menuitem"]', '清除管理后台密码'))
    await cdp.waitFor(`document.body.innerText.includes('确认清除')`)
    const focusInside = await cdp.eval(`!!document.activeElement?.closest?.('[role="dialog"]')`)
    record('弹窗打开后焦点在组件内', focusInside === true)
    await cdp.send('Input.dispatchKeyEvent', { type: 'keyDown', key: 'Escape', code: 'Escape', windowsVirtualKeyCode: 27, nativeVirtualKeyCode: 27 })
    await cdp.send('Input.dispatchKeyEvent', { type: 'keyUp', key: 'Escape', code: 'Escape', windowsVirtualKeyCode: 27, nativeVirtualKeyCode: 27 })
    await new Promise((r) => setTimeout(r, 300))
    const dialogGone = await cdp.eval(`!document.body.innerText.includes('确认清除')`)
    record('弹窗可用 Escape 关闭', dialogGone === true)

    // ---------- 7. 手机端日志：右上角悬浮按钮是唯一入口，开合/降级都不困住用户 ----------
    // 背景：日志入口曾被挪到底栏，而水波圆心与全屏覆盖层仍按右上角按钮设计 ——
    // 结果是「展开后底栏被盖住、面板内又没有关闭入口」，用户退不出来。
    // 本节用真实渲染 + 真实点击锁住修复后的行为；纯圆心计算测试挡不住这类回归。
    enterScenario('7 手机日志悬浮按钮')
    mock.operationStatus = idleOperation()
    mock.isAdmin = true
    mock.clearResult = 'success'
    await cdp.viewport(390, 844)
    await reloadApp(cdp)
    assert.ok(await cdp.waitFor(`!!document.querySelector('[data-log-fab]')`, 4000), '手机布局没有渲染右上角日志按钮')

    const phoneFab = await cdp.eval(logFabProbeJs)
    record(
      '手机竖屏：日志按钮在右上角可见、可点、在可视视口内，且全页只此一个',
      phoneFab?.count === 1 && phoneFab.visible === true && phoneFab.hitSelf === true
        && phoneFab.inViewport === true && phoneFab.expanded === 'false'
        && phoneFab.controls === 'phone-log-sheet',
      JSON.stringify(phoneFab),
    )

    // 底栏只留业务动作：主按钮 + 停止，不再有第二颗「日志」按钮。
    const dockButtons = await cdp.eval(`(() => {
      const stop = [...document.querySelectorAll('button')].find((b) => (b.textContent || '').trim() === '停止')
      const row = stop?.parentElement
      if (!row) return null
      return [...row.querySelectorAll('button')].map((b) => (b.textContent || '').trim() || (b.getAttribute('aria-label') || ''))
    })()`)
    record(
      '底栏只保留业务动作（主按钮 + 停止），日志入口唯一化到右上角按钮',
      Array.isArray(dockButtons) && dockButtons.length === 2
        && dockButtons.some((label) => /开始处理|已有操作进行中/.test(label))
        && dockButtons.includes('停止'),
      JSON.stringify(dockButtons),
    )

    // 收起状态：面板不占位、不可点；主题切换不被按钮压住。
    const closedSheet = await cdp.eval(logSheetProbeJs)
    const closedClearance = await cdp.eval(logFabClearanceJs)
    record(
      '收起状态：日志面板隐藏且不可交互（裁剪半径 0 / visibility hidden / inert）',
      closedSheet?.visibility === 'hidden' && closedSheet.inert === true
        && closedSheet.pointerEvents === 'none' && closedSheet.radiusPx === 0
        && closedSheet.coversCenter === false,
      JSON.stringify(closedSheet),
    )
    record(
      '右上角按钮不遮标题栏「主题切换」（无矩形重叠 + 中心命中自身）',
      closedClearance?.theme?.found === true && closedClearance.theme.overlap === false
        && closedClearance.theme.hitSelf === true,
      JSON.stringify(closedClearance?.theme || null),
    )

    // 展开：点按钮 → 面板铺满；同一颗按钮仍在最上层。
    assert.ok(await cdp.eval(logFabClickJs), '点击日志按钮失败')
    await new Promise((r) => setTimeout(r, 900))
    const openedSheet = await cdp.eval(logSheetProbeJs)
    const openedFab = await cdp.eval(logFabProbeJs)
    const openedOrigin = await cdp.eval(logRevealOriginJs)
    record(
      '点右上角按钮展开：面板可见可交互、中心可命中，按钮层级在其之上',
      openedSheet?.visibility === 'visible' && openedSheet.pointerEvents === 'auto'
        && openedSheet.inert === false && openedSheet.coversCenter === true
        && openedSheet.ariaModal === 'true' && openedFab?.expanded === 'true'
        && openedFab.hitSelf === true && openedFab.zIndex > openedSheet.zIndex,
      JSON.stringify({ sheetZ: openedSheet?.zIndex, fabZ: openedFab?.zIndex, expanded: openedFab?.expanded }),
    )
    record(
      '水波圆心 = 右上角按钮中心（真实布局解析，误差 ≤1px）',
      openedOrigin?.ok === true && openedOrigin.dx <= 1 && openedOrigin.dy <= 1,
      JSON.stringify(openedOrigin),
    )
    const openedClearance = await cdp.eval(logFabClearanceJs)
    record(
      '展开后按钮不遮常驻头部（搜索入口 / 工具菜单入口 / 任务状态都不被压住）',
      openedClearance?.search?.found === true && openedClearance.search.overlap === false && openedClearance.search.hitSelf === true
        && openedClearance.tools?.found === true && openedClearance.tools.overlap === false && openedClearance.tools.hitSelf === true
        && openedClearance.status?.found === true && openedClearance.status.overlap === false,
      JSON.stringify(openedClearance),
    )
    await captureScreenshot(cdp, 'fe-log-fab-open-390x844.png')

    // 收回：点同一颗按钮。
    assert.ok(await cdp.eval(logFabClickJs), '再次点击日志按钮失败')
    await new Promise((r) => setTimeout(r, 800))
    const reclosedSheet = await cdp.eval(logSheetProbeJs)
    const reclosedFab = await cdp.eval(logFabProbeJs)
    record(
      '再点同一按钮收回：面板回到隐藏不可交互，按钮状态复位',
      reclosedSheet?.visibility === 'hidden' && reclosedSheet.pointerEvents === 'none'
        && reclosedSheet.inert === true && reclosedSheet.radiusPx === 0 && reclosedSheet.coversCenter === false
        && reclosedFab?.expanded === 'false' && reclosedFab.hitSelf === true,
      JSON.stringify({ sheet: reclosedSheet, fab: reclosedFab }),
    )

    // ---------- 7b. 运行中自动展开 → 能退出，且退出后任务状态/页面内容保留 ----------
    enterScenario('7b 运行中自动展开后可退出')
    mock.operationStatus = {
      ...idleOperation(), status: 'running', active: true, mode: 'sss', phase: 'running',
      operation_id: 'op-mock-running-log', reason: '正在执行闪时送下单', next_action: '',
    }
    mock.stopTaskCalls = 0
    await reloadApp(cdp)
    const autoOpenedForLog = await cdp.waitFor(
      `(() => { const sheet = document.getElementById('phone-log-sheet'); return Boolean(sheet) && !sheet.hasAttribute('inert') })()`,
      5000,
    )
    assert.ok(autoOpenedForLog, '运行中日志面板未自动展开')
    await new Promise((r) => setTimeout(r, 800))
    assert.ok(await cdp.eval(`(() => { const el = document.querySelector('#phone-log-sheet [data-log-search-open]'); if (!el) return false; el.click(); return true })()`), '打不开日志搜索入口')
    assert.ok(await cdp.waitFor(`!!document.querySelector('#phone-log-sheet input[aria-label="搜索运行日志"]')`, 2500), '搜索输入框未出现')
    const filterSet = await cdp.eval(setInputJs('#phone-log-sheet input[aria-label="搜索运行日志"]', 'kept-after-close'))
    const runningSheet = await cdp.eval(logSheetProbeJs)
    const runningFab = await cdp.eval(logFabProbeJs)
    record(
      '运行中：日志自动展开，按钮仍在最上层且可点（呼吸 LED 用权威运行状态）',
      runningSheet?.coversCenter === true && runningFab?.hitSelf === true && runningFab.expanded === 'true',
      JSON.stringify({ sheet: runningSheet, fab: runningFab }),
    )
    assert.ok(await cdp.eval(logFabClickJs), '运行中点击日志按钮失败')
    await new Promise((r) => setTimeout(r, 800))
    const afterRunningClose = await cdp.eval(logSheetProbeJs)
    const stopAfterClose = await cdp.eval(stopButtonJs)
    // 状态必须来自仍在屏幕上的权威状态区（标题栏），不是被隐藏的日志层文案。
    const runningTextAfter = await cdp.eval(`document.body.innerText.includes('闪时送下单执行中')`)
    record(
      '运行中点按钮可退出日志；退出后任务仍在运行、停止按钮仍可用（关闭不停止任务）',
      afterRunningClose?.visibility === 'hidden' && afterRunningClose.coversCenter === false
        && stopAfterClose.found === true && stopAfterClose.disabled === false
        && runningTextAfter === true && mock.stopTaskCalls === 0,
      JSON.stringify({ sheet: afterRunningClose, stop: stopAfterClose, runningTextAfter, stopTaskCalls: mock.stopTaskCalls }),
    )
    assert.ok(await cdp.eval(logFabClickJs), '重新打开日志失败')
    await new Promise((r) => setTimeout(r, 700))
    const reopenedFilter = await cdp.eval(`document.querySelector('#phone-log-sheet input[aria-label="搜索运行日志"]')?.value ?? null`)
    record(
      '重新展开后日志面板内容原样保留（筛选草稿仍在，未被清空/重挂载）',
      filterSet?.ok === true && reopenedFilter === 'kept-after-close',
      JSON.stringify({ filterSet, reopenedFilter }),
    )
    assert.ok(await cdp.eval(logFabClickJs), '收起日志失败')
    await new Promise((r) => setTimeout(r, 700))

    // ---------- 7c. 快速连续开关：不留遮罩、不停在中间态 ----------
    enterScenario('7c 快速连续开关')
    mock.operationStatus = idleOperation()
    await reloadApp(cdp)
    const rapidToggle = async (times, gapMs) => {
      for (let i = 0; i < times; i += 1) {
        assert.ok(await cdp.eval(logFabClickJs), `快速开关第 ${i + 1} 次点击失败`)
        await new Promise((r) => setTimeout(r, gapMs))
      }
      await new Promise((r) => setTimeout(r, 900))
    }
    await rapidToggle(5, 60)
    const afterOdd = await cdp.eval(logSheetProbeJs)
    record(
      '快速点 5 次（奇数次）后停在「展开」一致态，没有中间态残留',
      afterOdd?.visibility === 'visible' && afterOdd.pointerEvents === 'auto' && afterOdd.inert === false
        && afterOdd.coversCenter === true && afterOdd.ariaHidden === 'false',
      JSON.stringify(afterOdd),
    )
    await rapidToggle(1, 0)
    const afterEven = await cdp.eval(logSheetProbeJs)
    record(
      '再点 1 次（偶数次）后完全收起：无遮罩、无 pointer-events 拦截',
      afterEven?.visibility === 'hidden' && afterEven.pointerEvents === 'none'
        && afterEven.inert === true && afterEven.radiusPx === 0 && afterEven.coversCenter === false,
      JSON.stringify(afterEven),
    )
    const primaryHitAfterRapid = await cdp.eval(`(() => {
      const btn = [...document.querySelectorAll('button')].find((b) => /开始处理|已有操作进行中/.test(b.textContent || ''))
      if (!btn) return null
      const r = btn.getBoundingClientRect()
      const cx = Math.min(Math.max(r.left + r.width / 2, 1), Math.max(window.innerWidth - 1, 1))
      const cy = Math.min(Math.max(r.top + r.height / 2, 1), Math.max(window.innerHeight - 1, 1))
      const hit = document.elementFromPoint(cx, cy)
      return { hitSelf: Boolean(hit && (hit === btn || btn.contains(hit))), tag: hit ? hit.tagName : null }
    })()`)
    record('快速开关后底栏主按钮仍可点（没有残留遮罩拦截点击）', primaryHitAfterRapid?.hitSelf === true, JSON.stringify(primaryHitAfterRapid))

    // ---------- 7d. 减弱动效 / 动画 API 缺失或抛错：开合都必须可靠 ----------
    enterScenario('7d 减弱动效与动画 API 降级')
    await cdp.send('Emulation.setEmulatedMedia', { features: [{ name: 'prefers-reduced-motion', value: 'reduce' }] })
    assert.ok(await cdp.eval(logFabClickJs), '减弱动效下点击日志按钮失败')
    await new Promise((r) => setTimeout(r, 250))
    const reducedOpen = await cdp.eval(logSheetProbeJs)
    assert.ok(await cdp.eval(logFabClickJs), '减弱动效下收起日志失败')
    await new Promise((r) => setTimeout(r, 250))
    const reducedClosed = await cdp.eval(logSheetProbeJs)
    record(
      '系统「减弱动态效果」下开合仍然可靠（不依赖动画也能落终态）',
      reducedOpen?.coversCenter === true && reducedOpen.pointerEvents === 'auto'
        && reducedClosed?.visibility === 'hidden' && reducedClosed.coversCenter === false,
      JSON.stringify({ reducedOpen, reducedClosed }),
    )
    await cdp.send('Emulation.setEmulatedMedia', { features: [] })

    const patchAnimateJs = (mode) => `(() => {
      if (!window.__origAnimate) window.__origAnimate = Element.prototype.animate
      if (${JSON.stringify(mode)} === 'missing') delete Element.prototype.animate
      else Element.prototype.animate = function () { throw new Error('mock: animate 不可用') }
      return typeof Element.prototype.animate
    })()`
    const restoreAnimateJs = `(() => {
      if (window.__origAnimate) Element.prototype.animate = window.__origAnimate
      return typeof Element.prototype.animate
    })()`
    for (const mode of ['missing', 'throwing']) {
      await cdp.eval(patchAnimateJs(mode))
      const opened = await cdp.eval(logFabClickJs)
      await new Promise((r) => setTimeout(r, 300))
      const sheetOpen = await cdp.eval(logSheetProbeJs)
      const closed = await cdp.eval(logFabClickJs)
      await new Promise((r) => setTimeout(r, 450))
      const sheetClosed = await cdp.eval(logSheetProbeJs)
      record(
        `动画 API ${mode === 'missing' ? '缺失' : '抛错'}时仍能展开、且能可靠收起（不困在日志层）`,
        opened === true && sheetOpen?.coversCenter === true && sheetOpen.pointerEvents === 'auto'
          && closed === true && sheetClosed?.visibility === 'hidden' && sheetClosed.coversCenter === false
          && sheetClosed.pointerEvents === 'none',
        JSON.stringify({ mode, sheetOpen, sheetClosed }),
      )
    }
    await cdp.eval(restoreAnimateJs)

    // ---------- 7e. 视口矩阵：竖屏 / 横屏 / 安全区 / 软键盘 ----------
    enterScenario('7e 视口与安全区')
    for (const [label, width, height] of [['390x844 竖屏', 390, 844], ['568x320 横屏', 568, 320], ['360x800 窄屏', 360, 800]]) {
      await cdp.viewport(width, height)
      await new Promise((r) => setTimeout(r, 250))
      const fab = await cdp.eval(logFabProbeJs)
      record(
        `${label}：日志按钮可见、可点且在可视视口内`,
        fab?.visible === true && fab.hitSelf === true && fab.inViewport === true,
        JSON.stringify(fab),
      )
    }

    // 安全区（刘海/圆角/手势条）：按钮整体让开 --safe-right/--safe-top，圆心跟着按钮走。
    await cdp.viewport(568, 320)
    await cdp.eval(`(() => {
      document.documentElement.style.setProperty('--safe-right', '44px')
      document.documentElement.style.setProperty('--safe-top', '24px')
      document.documentElement.style.setProperty('--safe-left', '44px')
      return true
    })()`)
    await new Promise((r) => setTimeout(r, 250))
    const notchFab = await cdp.eval(logFabProbeJs)
    const notchOrigin = await cdp.eval(logRevealOriginJs)
    record(
      '横屏 + 刘海安全区：按钮让开 --safe-right/--safe-top，水波圆心仍等于按钮中心',
      notchFab?.inViewport === true && Math.abs(notchFab.rect.right - (notchFab.innerWidth - 44 - 12)) <= 1
        && Math.abs(notchFab.rect.top - 24) <= 1
        && notchOrigin?.ok === true && notchOrigin.dx <= 1 && notchOrigin.dy <= 1,
      JSON.stringify({ fab: notchFab, origin: notchOrigin }),
    )
    await cdp.eval(`(() => {
      document.documentElement.style.removeProperty('--safe-right')
      document.documentElement.style.removeProperty('--safe-top')
      document.documentElement.style.removeProperty('--safe-left')
      return true
    })()`)

    // 软键盘：可视高度变小（App 会把底栏抬到 --keyboard-inset 之上），右上角按钮不受影响。
    await cdp.viewport(390, 430)
    await new Promise((r) => setTimeout(r, 300))
    const keyboardFab = await cdp.eval(logFabProbeJs)
    record(
      '软键盘顶起（390x430）：日志按钮仍完整可见可点，不被键盘/底栏遮挡',
      keyboardFab?.visible === true && keyboardFab.hitSelf === true && keyboardFab.inViewport === true
        && keyboardFab.rect.bottom <= keyboardFab.innerHeight + 1,
      JSON.stringify(keyboardFab),
    )
    assert.ok(await cdp.eval(logFabClickJs), '键盘场景下点击日志按钮失败')
    await new Promise((r) => setTimeout(r, 800))
    const keyboardSheet = await cdp.eval(logSheetProbeJs)
    const keyboardOrigin = await cdp.eval(logRevealOriginJs)
    assert.ok(await cdp.eval(logFabClickJs), '键盘场景下收起日志失败')
    await new Promise((r) => setTimeout(r, 800))
    const keyboardClosed = await cdp.eval(logSheetProbeJs)
    record(
      '软键盘场景：日志能开能关，圆心仍对齐按钮（可视高度变化不影响几何）',
      keyboardSheet?.coversCenter === true && keyboardOrigin?.ok === true
        && keyboardOrigin.dx <= 1 && keyboardOrigin.dy <= 1 && keyboardClosed?.visibility === 'hidden',
      JSON.stringify({ keyboardSheet, keyboardOrigin, keyboardClosed }),
    )

    // ---------- 7f. 日志重复展示：折叠/展开/搜索/复制（合成数据） ----------
    // 背景：旧实现用 `lines.length > 1 || msg.length > 120` 判断「可展开」——
    // 长单行与订单摘要（字段已显示完整）都多出一个「展开明细」，点开是同一份内容的
    // 副本；展开又是**追加**在摘要下面，同屏出现两份。本节用合成日志 + 真实渲染
    // + 真实点击把修复锁死：能不能展开只看「是否真的有隐藏内容」。
    enterScenario('7f 日志重复展示与搜索')
    mock.operationStatus = idleOperation()
    await cdp.viewport(390, 844)
    await reloadApp(cdp)
    assert.ok(await cdp.eval(logFabClickJs), '打开日志失败')
    assert.ok(await cdp.waitFor(`document.getElementById('phone-log-sheet')?.hasAttribute('inert') === false`, 4000), '日志面板未展开')
    queueSyntheticLogSet()
    assert.ok(
      await cdp.waitFor(`document.querySelectorAll('#phone-log-sheet [data-log-row="true"]').length >= 4`, 8000),
      '合成日志没有送达渲染层',
    )
    const deliveredRows = await cdp.eval(logRowsProbeJs)
    const longRow = deliveredRows?.rows.find((row) => row.text.includes('WPS 只读核对完成'))
    const multiRow = deliveredRows?.rows.find((row) => row.text.includes('地址无法自动识别'))
    const orderRow = deliveredRows?.rows.find((row) => row.text.includes('W123456'))
    const errorRow = deliveredRows?.rows.find((row) => row.text.includes('验证码错误'))
    assert.ok(longRow && multiRow && orderRow && errorRow, `合成日志没有全部渲染：${JSON.stringify(deliveredRows?.rows?.map((r) => r.level))}`)

    record(
      '长单行（>120 字、只有一行）直接显示完整内容，不给「展开明细」',
      longRow.expandable === false && longRow.toggle === null
        && longRow.lines.length === 1 && longRow.lines[0] === SYNTHETIC_LONG_LINE,
      JSON.stringify(longRow),
    )
    record(
      '多行日志折叠时只显示首行，展开入口标明还有几行',
      multiRow.expandable === true && multiRow.toggle === 'expand' && multiRow.full === false
        && multiRow.lines.length === 1 && multiRow.lines[0] === SYNTHETIC_MULTI_LINE.split('\n')[0],
      JSON.stringify(multiRow),
    )
    record(
      '订单摘要逐字段显示完整，不再提供重复全文的「展开明细」',
      orderRow.expandable === false && orderRow.toggle === null && orderRow.lines.length === 4
        && orderRow.lines.join('|') === 'W123456|张三|小份|已下单',
      JSON.stringify(orderRow),
    )
    record(
      '错误行默认可见（级别默认「全部」，警告与错误不被默认过滤）',
      errorRow.level === 'ERROR' && deliveredRows.rows.some((row) => row.level === 'WARN'),
      JSON.stringify(deliveredRows.rows.map((row) => row.level)),
    )
    record(
      '日志尾部装饰标语已删除（不占正文、不帮助操作）',
      deliveredRows.slogan === false,
      JSON.stringify({ slogan: deliveredRows.slogan }),
    )

    // 展开：必须是**替换**摘要，不能摘要 + 全文两份同屏。
    const multiIndex = await cdp.eval(logRowIndexJs('地址无法自动识别'))
    assert.ok(multiIndex >= 0, '找不到多行日志行')
    assert.ok(await cdp.eval(clickLogToggleJs(multiIndex)), '点击「展开明细」失败')
    const expandedRow = (await cdp.eval(logRowsProbeJs))?.rows[multiIndex]
    record(
      '展开明细 = 原位替换摘要：完整内容只出现一份，首行不重复',
      expandedRow?.full === true && expandedRow.toggle === 'collapse'
        && expandedRow.lines.length === SYNTHETIC_MULTI_LINE.split('\n').length
        && expandedRow.text.split(SYNTHETIC_MULTI_LINE.split('\n')[0]).length - 1 === 1,
      JSON.stringify(expandedRow),
    )
    assert.ok(await cdp.eval(clickLogToggleJs(multiIndex)), '点击「收起明细」失败')
    const collapsedAgain = (await cdp.eval(logRowsProbeJs))?.rows[multiIndex]
    record(
      '收起后回到首行摘要（不残留第二份全文）',
      collapsedAgain?.full === false && collapsedAgain.lines.length === 1 && collapsedAgain.toggle === 'expand',
      JSON.stringify(collapsedAgain),
    )

    // 搜索：命中被折叠的行时必须让命中内容可见。
    assert.ok(await cdp.eval(clickSelectorJs('#phone-log-sheet [data-log-search-open]')), '打不开搜索入口')
    assert.ok(await cdp.waitFor(`!!document.querySelector('#phone-log-sheet input[aria-label="搜索运行日志"]')`, 2500), '搜索输入框未出现')
    const hiddenHitSet = await cdp.eval(setInputJs('#phone-log-sheet input[aria-label="搜索运行日志"]', SYNTHETIC_HIDDEN_NEEDLE))
    assert.ok(hiddenHitSet?.ok === true, `搜索词写入失败：${JSON.stringify(hiddenHitSet)}`)
    await new Promise((r) => setTimeout(r, 300))
    const searched = await cdp.eval(logRowsProbeJs)
    const hitRow = searched?.rows.find((row) => row.text.includes(SYNTHETIC_HIDDEN_NEEDLE))
    record(
      '搜索命中隐藏行：自动展开让命中内容可见，并标出命中行',
      searched?.count === 1 && hitRow?.full === true && hitRow.hit === true
        && hitRow.forced === true && hitRow.highlighted >= 1
        && hitRow.lines.some((line) => line.includes(SYNTHETIC_HIDDEN_NEEDLE)),
      JSON.stringify({ count: searched?.count, hitRow }),
    )
    const hitScroll = await cdp.eval(`(() => {
      const paper = document.querySelector('#phone-log-sheet .receipt-paper')
      const hit = paper?.querySelector('[data-log-hit="true"]')
      if (!paper || !hit) return null
      const box = paper.getBoundingClientRect()
      const target = hit.getBoundingClientRect()
      return { visible: target.top >= box.top - 1 && target.bottom <= box.bottom + 1, boxTop: box.top, boxBottom: box.bottom, targetTop: target.top, targetBottom: target.bottom }
    })()`)
    record(
      '搜索命中的那一条被滚进可视区（不是只渲染出来让用户自己找）',
      hitScroll?.visible === true,
      JSON.stringify(hitScroll),
    )
    const hitCountText = await cdp.eval(`document.querySelector('#phone-log-sheet [data-log-count]')?.textContent?.replace(/\\s+/g, ' ').trim() ?? null`)
    record(
      '搜索时显示命中条数（上下文统计，不常驻占位）',
      hitCountText === '显示 1/4',
      JSON.stringify({ hitCountText }),
    )

    const noMatchSet = await cdp.eval(setInputJs('#phone-log-sheet input[aria-label="搜索运行日志"]', SYNTHETIC_NO_MATCH))
    assert.ok(noMatchSet?.ok === true, '无匹配搜索词写入失败')
    await new Promise((r) => setTimeout(r, 300))
    const noMatch = await cdp.eval(logRowsProbeJs)
    record(
      '无匹配时给出空状态说明（不是空白一片），且不删除数据',
      noMatch?.count === 0 && noMatch.emptyHint === true,
      JSON.stringify({ count: noMatch?.count, emptyHint: noMatch?.emptyHint }),
    )
    assert.ok(await cdp.eval(clickSelectorJs('#phone-log-sheet [data-log-search-close]')), '关闭搜索失败')
    await new Promise((r) => setTimeout(r, 300))
    const afterSearchClose = await cdp.eval(logRowsProbeJs)
    const searchInputGone = await cdp.eval(`!document.querySelector('#phone-log-sheet input[aria-label="搜索运行日志"]')`)
    record(
      '关闭搜索同时清除筛选：不会留下「看不见的筛选」让日志少一截',
      afterSearchClose?.count === 4 && searchInputGone === true,
      JSON.stringify({ count: afterSearchClose?.count, searchInputGone }),
    )

    // 复制：拦截 writeText 记录实际写入的文本。真实剪贴板链路（含 execCommand 兜底）
    // 由 src/lib/clipboard.test.ts 单测覆盖；headless 读真实剪贴板需要焦点+权限，易抖动。
    await cdp.eval(`(() => {
      window.__copiedLogs = null
      const original = navigator.clipboard && navigator.clipboard.writeText
        ? navigator.clipboard.writeText.bind(navigator.clipboard)
        : null
      window.__restoreClipboard = () => {
        try { delete navigator.clipboard } catch { /* ignore */ }
        return true
      }
      Object.defineProperty(navigator, 'clipboard', {
        configurable: true,
        value: {
          writeText: async (text) => {
            window.__copiedLogs = text
            if (original) { try { await original(text) } catch { /* 真实剪贴板失败不影响断言 */ } }
          },
        },
      })
      return true
    })()`)
    assert.ok(await cdp.eval(clickSelectorJs('#phone-log-sheet [data-log-tools]')), '打不开日志工具菜单')
    assert.ok(await cdp.waitFor(`!!document.querySelector('[data-log-copy="true"]')`, 2500), '工具菜单没有出现复制项')
    assert.ok(await cdp.eval(clickSelectorJs('[data-log-copy="true"]')), '点击复制失败')
    const copiedReady = await cdp.waitFor(`typeof window.__copiedLogs === 'string' && window.__copiedLogs.length > 0`, 3000)
    const copied = copiedReady ? await cdp.eval(`window.__copiedLogs`) : null
    record(
      '复制保留完整原文：隐藏行、订单摘要分隔符、长单行都不丢，且每条只出现一次',
      typeof copied === 'string'
        && copied.includes('处理办法：在待确认地址里补全后重试，不要直接重跑本批')
        && copied.includes(SYNTHETIC_ORDER_SUMMARY)
        && copied.includes(SYNTHETIC_LONG_LINE)
        && copied.split('闪时送下单失败：地址无法自动识别').length - 1 === 1,
      JSON.stringify({ length: copied?.length ?? 0, head: String(copied ?? '').slice(0, 80) }),
    )

    // 工具菜单：级别筛选默认「全部」（警告/错误不被默认过滤），低频操作都在这里。
    assert.ok(await cdp.eval(clickSelectorJs('#phone-log-sheet [data-log-tools]')), '再次打开工具菜单失败')
    assert.ok(await cdp.waitFor(`!!document.querySelector('[data-log-level="ALL"]')`, 2500), '工具菜单没有级别筛选')
    const menuState = await cdp.eval(`(() => ({
      allChecked: document.querySelector('[data-log-level="ALL"]')?.getAttribute('aria-checked'),
      warnChecked: document.querySelector('[data-log-level="WARN"]')?.getAttribute('aria-checked'),
      warnCount: document.querySelector('[data-log-level="WARN"]')?.textContent?.trim() ?? '',
      hasAutoscroll: Boolean(document.querySelector('[data-log-autoscroll="true"]')),
      hasClear: Boolean(document.querySelector('[data-log-clear="true"]')),
      problems: document.querySelector('#phone-log-sheet [data-log-problems]')?.textContent?.trim() ?? null,
    }))()`)
    record(
      '低频操作都在工具菜单里，级别默认「全部」；警告/错误条数留在入口角标上（不是藏起来）',
      menuState?.allChecked === 'true' && menuState.warnChecked === 'false'
        && menuState.hasAutoscroll === true && menuState.hasClear === true
        && menuState.problems === '2' && menuState.warnCount.includes('1'),
      JSON.stringify(menuState),
    )
    assert.ok(await cdp.eval(clickSelectorJs('[data-log-level="ERROR"]')), '选择 ERROR 级别失败')
    await new Promise((r) => setTimeout(r, 300))
    const errorOnly = await cdp.eval(logRowsProbeJs)
    record(
      '级别筛选生效：只看错误时仍能看到错误行（可筛、不是默认过滤）',
      errorOnly?.count === 1 && errorOnly.rows[0]?.level === 'ERROR',
      JSON.stringify({ count: errorOnly?.count, levels: errorOnly?.rows.map((row) => row.level) }),
    )
    assert.ok(await cdp.eval(clickSelectorJs('#phone-log-sheet [data-log-tools]')), '打开工具菜单失败')
    assert.ok(await cdp.waitFor(`!!document.querySelector('[data-log-level="ALL"]')`, 2500), '工具菜单没有级别筛选')
    assert.ok(await cdp.eval(clickSelectorJs('[data-log-level="ALL"]')), '恢复全部级别失败')
    await new Promise((r) => setTimeout(r, 300))

    // 清理常规：只清 INFO/OK，警告与错误必须留下。
    assert.ok(await cdp.eval(clickSelectorJs('#phone-log-sheet [data-log-tools]')), '打开工具菜单失败')
    assert.ok(await cdp.waitFor(`!!document.querySelector('[data-log-clear="true"]')`, 2500), '工具菜单没有清理项')
    assert.ok(await cdp.eval(clickSelectorJs('[data-log-clear="true"]')), '点击清理失败')
    await new Promise((r) => setTimeout(r, 400))
    const afterClear = await cdp.eval(logRowsProbeJs)
    record(
      '清理常规日志后警告与错误仍在（清理不等于把风险一起删掉）',
      afterClear?.count === 2 && afterClear.rows.some((row) => row.level === 'WARN')
        && afterClear.rows.some((row) => row.level === 'ERROR'),
      JSON.stringify({ count: afterClear?.count, levels: afterClear?.rows.map((row) => row.level) }),
    )

    // ---------- 7g. 运行中开关日志：实时追加、关掉不弹回、任务不受影响 ----------
    enterScenario('7g 运行中开关日志')
    mock.operationStatus = {
      ...idleOperation(), status: 'running', active: true, mode: 'sss', phase: 'running',
      operation_id: 'op-mock-log-running', reason: '正在执行闪时送下单', next_action: '',
    }
    mock.stopTaskCalls = 0
    await reloadApp(cdp)
    assert.ok(
      await cdp.waitFor(`document.getElementById('phone-log-sheet')?.hasAttribute('inert') === false`, 5000),
      '运行中日志未自动展开',
    )
    // 水波是 480ms 的 clip-path 动画：`inert` 在动画一开始就翻了，此时面板中心
    // 可能还在圆外。等「中心真的被面板盖住」再探，否则探到的是动画中途的快照。
    assert.ok(
      await cdp.waitFor(`(() => {
        const el = document.getElementById('phone-log-sheet')
        if (!el) return false
        const r = el.getBoundingClientRect()
        const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2)
        return Boolean(hit && (hit === el || el.contains(hit)))
      })()`, 3000),
      '日志面板没有铺满（水波未完成）',
    )
    queueLogs([{ ts: '12:01:01', level: 'INFO', msg: '运行中新增日志 A' }])
    assert.ok(await cdp.waitFor(`document.body.innerText.includes('运行中新增日志 A')`, 8000), '运行中新增日志没有实时出现')
    const runningSheetOpen = await cdp.eval(logSheetProbeJs)
    record(
      '运行中：日志实时追加到已展开的面板，面板保持展开',
      runningSheetOpen?.coversCenter === true && runningSheetOpen.inert === false,
      JSON.stringify({ visibility: runningSheetOpen?.visibility, filterValue: runningSheetOpen?.filterValue }),
    )
    assert.ok(await cdp.eval(logFabClickJs), '运行中收起日志失败')
    await new Promise((r) => setTimeout(r, 800))
    queueLogs([{ ts: '12:01:02', level: 'WARN', msg: '收起后新增日志 B' }])
    await new Promise((r) => setTimeout(r, 2400))
    const closedWhileStreaming = await cdp.eval(logSheetProbeJs)
    const stopWhileStreaming = await cdp.eval(stopButtonJs)
    const hiddenLogNotOnScreen = await cdp.eval(`!document.body.innerText.includes('收起后新增日志 B')`)
    record(
      '运行中收起日志后不再自动弹回；任务继续运行（关闭日志不停止任务）',
      closedWhileStreaming?.visibility === 'hidden' && closedWhileStreaming.coversCenter === false
        && hiddenLogNotOnScreen === true
        && stopWhileStreaming.found === true && stopWhileStreaming.disabled === false
        && mock.stopTaskCalls === 0,
      JSON.stringify({ visibility: closedWhileStreaming?.visibility, hiddenLogNotOnScreen, stop: stopWhileStreaming, stopTaskCalls: mock.stopTaskCalls }),
    )
    assert.ok(await cdp.eval(logFabClickJs), '运行中重新打开日志失败')
    await new Promise((r) => setTimeout(r, 900))
    const reopenedStreamed = await cdp.eval(logRowsProbeJs)
    record(
      '重新展开：收起期间新增的日志仍在（关面板不丢日志数据）',
      reopenedStreamed?.rows.some((row) => row.text.includes('收起后新增日志 B')) === true
        && reopenedStreamed.rows.some((row) => row.text.includes('运行中新增日志 A')) === true,
      JSON.stringify({ count: reopenedStreamed?.count, text: reopenedStreamed?.text }),
    )
    await cdp.eval(`(() => { try { window.__restoreClipboard?.() } catch { /* ignore */ } return true })()`)
    mock.operationStatus = idleOperation()

    // 桌面/平板不回退：没有悬浮按钮，日志仍是常驻分栏。
    await cdp.viewport(1280, 800)
    await new Promise((r) => setTimeout(r, 300))
    const desktopLogLayout = await cdp.eval(`(() => ({
      fabs: document.querySelectorAll('[data-log-fab]').length,
      sheet: Boolean(document.getElementById('phone-log-sheet')),
      dockedTitle: [...document.querySelectorAll('h2')].some((h) => (h.textContent || '').includes('运行日志')),
    }))()`)
    record(
      '桌面布局不回退：无悬浮按钮、无全屏面板，日志仍是常驻分栏',
      desktopLogLayout.fabs === 0 && desktopLogLayout.sheet === false && desktopLogLayout.dockedTitle === true,
      JSON.stringify(desktopLogLayout),
    )
    await cdp.viewport(390, 844)
    await reloadApp(cdp)

    // ---------- 8. 手机端提示条与日志退出按钮（真实鼠标点击，不是 z-index 检查） ----------
    // 背景（C 报告 P1）：手机端复制日志触发的 toast 贴在顶部，压住右上角日志按钮，
    // 提示期间点按钮没反应 —— 退出入口被短时遮住。修法是让提示条整条落到按钮与
    // 日志头部**之下**（不改层级、不缩短时长、不取消提示、桌面行为不变）。
    // 本节用 CDP 真实鼠标事件走浏览器命中测试：被盖住就点不到，能开合才算过。
    enterScenario('8 手机端提示条与日志按钮')
    mock.operationStatus = idleOperation()
    mock.isAdmin = true
    mock.stopTaskCalls = 0
    await cdp.viewport(390, 844)
    await reloadApp(cdp)
    assert.ok(await cdp.waitFor(`!!document.querySelector('[data-log-fab]')`, 4000), '手机布局没有渲染日志按钮')
    queueSyntheticLogSet()

    const logOpenNow = async () => cdp.eval(`document.getElementById('phone-log-sheet')?.hasAttribute('inert') === false`)
    const toggleByRealClick = async () => {
      const before = await logOpenNow()
      const probe = await cdp.eval(toastProbeJs)
      assert.ok(probe?.fabCenter, '找不到日志按钮中心')
      await realClickAt(cdp, probe.fabCenter.x, probe.fabCenter.y)
      await new Promise((r) => setTimeout(r, 900))
      const after = await logOpenNow()
      return { before, after, changed: before !== after }
    }
    const ensureLogOpen = async () => { if (!(await logOpenNow())) await toggleByRealClick() }
    const ensureLogClosed = async () => { if (await logOpenNow()) await toggleByRealClick() }
    const copyFromTools = async () => {
      assert.ok(await cdp.eval(clickSelectorJs('#phone-log-sheet [data-log-tools]')), '打不开日志工具菜单')
      assert.ok(await cdp.waitFor(`!!document.querySelector('[data-log-copy="true"]')`, 2500), '工具菜单没有复制项')
      assert.ok(await cdp.eval(clickSelectorJs('[data-log-copy="true"]')), '点击复制失败')
      await new Promise((r) => setTimeout(r, 400))
    }
    const overlaps = (a, b) => Boolean(a && b) && !(a.right <= b.left || a.left >= b.right || a.bottom <= b.top || a.top >= b.bottom)

    // 8.1 成功提示：真实点开日志 → 复制成功 → 提示显示期间按钮仍可点（能收起、能再展开）
    await toggleByRealClick()
    assert.ok(await logOpenNow(), '真实点击日志按钮没有展开面板')
    assert.ok(
      await cdp.waitFor(`document.querySelectorAll('#phone-log-sheet [data-log-row="true"]').length >= 4`, 8000),
      '合成日志没有送达渲染层',
    )
    await cdp.eval(stubClipboardJs('ok'))
    await copyFromTools()
    const successToast = await cdp.eval(toastProbeJs)
    record(
      '成功提示显示期间：提示条与日志按钮不相交，按钮中心命中按钮本身',
      successToast?.count >= 1 && successToast.toasts.some((item) => item.type === 'success')
        && successToast.overlapsFab === false && successToast.fabHitIsFab === true
        && successToast.toasts[0].rect.top > successToast.fab.bottom,
      JSON.stringify({ count: successToast?.count, types: successToast?.toasts.map((item) => item.type), toastRect: successToast?.toasts[0]?.rect, fab: successToast?.fab, fabHitIsFab: successToast?.fabHitIsFab, fabHitDesc: successToast?.fabHitDesc }),
    )
    const closeWithSuccessToast = await toggleByRealClick()
    const toastSurvivedClick = await cdp.eval(toastProbeJs)
    record(
      '成功提示显示期间：真实点击右上角按钮立即收起日志（状态确实变化，提示没被点掉）',
      closeWithSuccessToast.changed === true && closeWithSuccessToast.after === false
        && toastSurvivedClick?.count >= 1,
      JSON.stringify({ toggle: closeWithSuccessToast, toastCountAfter: toastSurvivedClick?.count }),
    )
    const reopenWithSuccessToast = await toggleByRealClick()
    record(
      '成功提示显示期间：再点一次仍能立即展开（双向都可点）',
      reopenWithSuccessToast.changed === true && reopenWithSuccessToast.after === true,
      JSON.stringify(reopenWithSuccessToast),
    )

    // 8.2 失败提示：Clipboard API 与 execCommand 两条路径都失败 → 应用自己弹错误提示
    await cdp.eval(stubClipboardJs('fail'))
    await copyFromTools()
    const failureToast = await cdp.eval(toastProbeJs)
    record(
      '失败提示显示期间：提示条与日志按钮不相交，按钮中心命中按钮本身',
      failureToast?.count >= 1 && failureToast.toasts.some((item) => item.type === 'error')
        && failureToast.toasts.some((item) => item.text.includes('复制'))
        && failureToast.overlapsFab === false && failureToast.fabHitIsFab === true,
      JSON.stringify({ count: failureToast?.count, types: failureToast?.toasts.map((item) => item.type), texts: failureToast?.toasts.map((item) => item.text.slice(0, 48)), fabHitIsFab: failureToast?.fabHitIsFab, fabHitDesc: failureToast?.fabHitDesc }),
    )
    const closeWithFailureToast = await toggleByRealClick()
    record(
      '失败提示显示期间：真实点击按钮立即收起日志（状态确实变化）',
      closeWithFailureToast.changed === true && closeWithFailureToast.after === false,
      JSON.stringify(closeWithFailureToast),
    )

    // 8.3 连续提示：短时间内叠多条提示，仍然能退出
    await cdp.eval(stubClipboardJs('ok'))
    await ensureLogOpen()
    for (let i = 0; i < 3; i += 1) {
      await copyFromTools()
      await new Promise((r) => setTimeout(r, 120))
    }
    const stackedToasts = await cdp.eval(toastProbeJs)
    const closeWithStack = await toggleByRealClick()
    record(
      '连续出现提示时仍可退出：叠了多条提示也能真实点击收起',
      stackedToasts?.count >= 2 && stackedToasts.overlapsFab === false && stackedToasts.fabHitIsFab === true
        && closeWithStack.changed === true && closeWithStack.after === false,
      JSON.stringify({ stacked: stackedToasts?.count, overlapsFab: stackedToasts?.overlapsFab, toggle: closeWithStack }),
    )

    // 8.4 提示条不遮日志搜索 / 工具入口（窄屏 + 搜索展开 = 头部折行的最坏情况）
    await cdp.viewport(360, 800)
    await new Promise((r) => setTimeout(r, 300))
    await ensureLogOpen()
    assert.ok(await cdp.eval(clickSelectorJs('#phone-log-sheet [data-log-search-open]')), '打不开日志搜索')
    assert.ok(await cdp.waitFor(`!!document.querySelector('#phone-log-sheet [data-log-search]')`, 2500), '搜索输入框没有出现')
    await cdp.eval(stubClipboardJs('ok'))
    await copyFromTools()
    const narrowToast = await cdp.eval(toastProbeJs)
    const narrowChrome = await cdp.eval(logChromeProbeJs)
    const narrowToastRect = narrowToast?.toasts?.[0]?.rect || null
    const narrowNotice = await cdp.eval(noticeRegionProbeJs)
    record(
      '窄屏 + 搜索展开（头部折行）时：日志头部整体让到提示区域之下，提示不遮搜索框/工具入口且两者仍可点',
      narrowToast?.count >= 1 && narrowChrome?.headerRow && narrowToastRect && narrowNotice?.rect
        && overlaps(narrowToastRect, narrowChrome.search) === false
        && overlaps(narrowToastRect, narrowChrome.tools) === false
        && narrowChrome.headerRow.top >= narrowNotice.rect.bottom - 1
        && narrowChrome.searchHit?.hitSelf === true && narrowChrome.toolsHit?.hitSelf === true,
      JSON.stringify({ toast: narrowToastRect, headerRow: narrowChrome?.headerRow, search: narrowChrome?.search, tools: narrowChrome?.tools, searchHit: narrowChrome?.searchHit, toolsHit: narrowChrome?.toolsHit }),
    )
    record(
      '手机端提示条仍在顶部区域（不是机械地挪到底部）',
      Boolean(narrowToastRect) && narrowToastRect.top < (narrowToast?.innerHeight ?? 0) / 2
        && narrowToastRect.bottom < (narrowToast?.innerHeight ?? 0) * 0.6,
      JSON.stringify({ toast: narrowToastRect, innerHeight: narrowToast?.innerHeight }),
    )
    assert.ok(await cdp.eval(clickSelectorJs('#phone-log-sheet [data-log-search-close]')), '关闭搜索失败')
    await new Promise((r) => setTimeout(r, 250))

    // 最坏情况：运行中的状态标签最长（「闪时送下单执行中」）+ 搜索展开 + 360 窄屏，
    // 头部可能折成两行 —— 提示条的偏移必须按这一档留量，而不是按收起态的量。
    mock.operationStatus = {
      ...idleOperation(), status: 'running', active: true, mode: 'sss', phase: 'running',
      operation_id: 'op-mock-toast-narrow', reason: '正在执行闪时送下单', next_action: '',
    }
    await reloadApp(cdp)
    assert.ok(
      await cdp.waitFor(`document.getElementById('phone-log-sheet')?.hasAttribute('inert') === false`, 5000),
      '运行中日志未自动展开',
    )
    assert.ok(await cdp.eval(clickSelectorJs('#phone-log-sheet [data-log-search-open]')), '打不开日志搜索')
    assert.ok(await cdp.waitFor(`!!document.querySelector('#phone-log-sheet [data-log-search]')`, 2500), '搜索输入框没有出现')
    await cdp.eval(stubClipboardJs('ok'))
    await copyFromTools()
    const worstToast = await cdp.eval(toastProbeJs)
    const worstChrome = await cdp.eval(logChromeProbeJs)
    const worstToastRect = worstToast?.toasts?.[0]?.rect || null
    const worstNotice = await cdp.eval(noticeRegionProbeJs)
    record(
      '最坏情况（运行中长状态 + 搜索展开 + 360 窄屏）：日志头部整体让到提示区域之下，提示不遮搜索/工具',
      worstToast?.count >= 1 && worstChrome?.headerRow && worstToastRect && worstNotice?.rect
        && overlaps(worstToastRect, worstChrome.search) === false
        && overlaps(worstToastRect, worstChrome.tools) === false
        && worstChrome.headerRow.top >= worstNotice.rect.bottom - 1
        && worstChrome.searchHit?.hitSelf === true && worstChrome.toolsHit?.hitSelf === true,
      JSON.stringify({ toast: worstToastRect, headerRow: worstChrome?.headerRow, search: worstChrome?.search, tools: worstChrome?.tools, searchHit: worstChrome?.searchHit, toolsHit: worstChrome?.toolsHit }),
    )
    mock.operationStatus = idleOperation()
    await cdp.viewport(390, 844)

    // 8.5 提示条不遮底部关键动作：复制出提示后立刻真实点击收起日志，再看底栏
    // 先 reload 把「运行中」彻底回到空闲：主按钮在 operationActive 时是 disabled，
    // 而 shadcn 的 `disabled:pointer-events-none` 会让命中测试落到父 DIV ——
    // 那样测到的是「按钮不可用」，不是「提示条挡住了按钮」。
    mock.operationStatus = idleOperation()
    await reloadApp(cdp)
    const primaryReady = await cdp.eval(`(() => {
      const b = [...document.querySelectorAll('button')].find((x) => /开始处理|已有操作进行中/.test(x.textContent || ''))
      return b ? { disabled: b.disabled === true, text: (b.textContent || '').trim() } : null
    })()`)
    assert.ok(primaryReady && primaryReady.disabled === false, `底栏主按钮不可用，无法验证提示条是否挡住它：${JSON.stringify(primaryReady)}`)
    // 主按钮的校验需要密码；缺密码时会停在字段校验（不弹确认框），
    // 那样就分不清「提示条挡住按钮」和「校验没过」。
    assert.ok(await cdp.eval(clickSelectorJs('#task-tab-order')), '切不到订单处理页签')
    if (!(await cdp.eval(`!!document.querySelector('#order-password')`))) {
      assert.ok(await cdp.eval(clickByTextJs('button', '管理网址与登录凭据')), '打不开高级设置')
      await new Promise((r) => setTimeout(r, 300))
    }
    const secretSet = await cdp.eval(setInputJs('#order-password', 'toast-dock-secret'))
    assert.ok(secretSet?.ok === true, `写入订单密码失败：${JSON.stringify(secretSet)}`)
    await new Promise((r) => setTimeout(r, 400))
    await ensureLogOpen()
    await copyFromTools()
    await ensureLogClosed()
    const dockToast = await cdp.eval(toastProbeJs)
    const dockChrome = await cdp.eval(logChromeProbeJs)
    const dockToastRect = dockToast?.toasts?.[0]?.rect || null
    record(
      '提示条不遮底部关键动作：提示还在时底栏矩形不相交，主按钮中心命中自己',
      dockToast?.count >= 1 && dockToastRect
        && overlaps(dockToastRect, dockChrome?.primary) === false
        && overlaps(dockToastRect, dockChrome?.stop) === false
        && dockChrome?.primaryHit?.hitSelf === true,
      JSON.stringify({ toast: dockToastRect, primary: dockChrome?.primary, stop: dockChrome?.stop, primaryHit: dockChrome?.primaryHit }),
    )
    // 更强的判据：真点一下（不是查矩形）。注意「停止」此时是 disabled，shadcn 的
    // `disabled:pointer-events-none` 会让它不参与命中测试，所以用主按钮做真实点击。
    assert.ok(dockChrome?.primary, '找不到底栏主按钮')
    await realClickAt(
      cdp,
      (dockChrome.primary.left + dockChrome.primary.right) / 2,
      (dockChrome.primary.top + dockChrome.primary.bottom) / 2,
    )
    await new Promise((r) => setTimeout(r, 500))
    const primaryDialog = await cdp.eval(openDialogProbeJs)
    const toastBeforeDialogClose = await cdp.eval(toastProbeJs)
    record(
      '提示条不遮底部关键动作：提示还在时真实点击「开始处理」仍能打开确认弹窗',
      dockToast?.count >= 1 && primaryDialog?.count === 1
        && primaryDialog.text.includes('确认开始订单处理') && toastBeforeDialogClose?.count >= 1,
      JSON.stringify({ dialog: primaryDialog?.text?.slice(0, 60) || null, toastCount: toastBeforeDialogClose?.count }),
    )
    await cdp.send('Input.dispatchKeyEvent', { type: 'keyDown', key: 'Escape', code: 'Escape', windowsVirtualKeyCode: 27, nativeVirtualKeyCode: 27 })
    await cdp.send('Input.dispatchKeyEvent', { type: 'keyUp', key: 'Escape', code: 'Escape', windowsVirtualKeyCode: 27, nativeVirtualKeyCode: 27 })
    await new Promise((r) => setTimeout(r, 300))

    // 8.8 横屏 + 安全区：偏移以 --safe-top 为基准，按钮与提示条一起下移，关系不变
    enterScenario('8d 横屏与安全区')
    mock.operationStatus = idleOperation()
    await cdp.viewport(568, 320)
    await new Promise((r) => setTimeout(r, 300))
    await ensureLogOpen()
    await cdp.eval(stubClipboardJs('ok'))
    await copyFromTools()
    const landscapeToast = await cdp.eval(toastProbeJs)
    record(
      '手机横屏（568x320）：提示条不压日志按钮，真实点击仍能收起',
      landscapeToast?.count >= 1 && landscapeToast.overlapsFab === false && landscapeToast.fabHitIsFab === true
        && landscapeToast.toasts[0].rect.bottom < landscapeToast.innerHeight,
      JSON.stringify({ count: landscapeToast?.count, toast: landscapeToast?.toasts[0]?.rect, fab: landscapeToast?.fab, innerHeight: landscapeToast?.innerHeight }),
    )
    const landscapeToggle = await toggleByRealClick()
    record(
      '手机横屏：提示显示期间真实点击按钮立即收起日志',
      landscapeToggle.changed === true && landscapeToggle.after === false,
      JSON.stringify(landscapeToggle),
    )

    // 安全区（刘海/圆角）：把 --safe-top 调大，按钮与提示条必须一起让开，相对关系不变
    await ensureLogOpen()
    await cdp.eval(`(() => {
      document.documentElement.style.setProperty('--safe-top', '24px')
      document.documentElement.style.setProperty('--safe-right', '44px')
      return true
    })()`)
    await new Promise((r) => setTimeout(r, 300))
    await copyFromTools()
    const notchToast = await cdp.eval(toastProbeJs)
    record(
      '刘海安全区（--safe-top=24px / --safe-right=44px）：提示条与按钮一起下移，仍不相交、按钮可点',
      notchToast?.count >= 1 && notchToast.overlapsFab === false && notchToast.fabHitIsFab === true
        && notchToast.toasts[0].rect.top >= notchToast.fab.bottom + 4
        && Math.abs(notchToast.fab.top - 24) <= 1,
      JSON.stringify({ count: notchToast?.count, toast: notchToast?.toasts[0]?.rect, fab: notchToast?.fab, fabHitIsFab: notchToast?.fabHitIsFab }),
    )
    const notchToggle = await toggleByRealClick()
    record(
      '刘海安全区：提示显示期间真实点击按钮立即收起日志',
      notchToggle.changed === true && notchToggle.after === false,
      JSON.stringify(notchToggle),
    )
    await cdp.eval(`(() => {
      document.documentElement.style.removeProperty('--safe-top')
      document.documentElement.style.removeProperty('--safe-right')
      return true
    })()`)
    await cdp.viewport(390, 844)

    // 8.6 模态弹窗仍然拦住它背后的日志按钮（层级没被抬高，也没被提示条改写）
    enterScenario('8b 模态弹窗仍拦住日志按钮')
    mock.operationStatus = idleOperation()
    // 用**决策**弹窗（DecisionDialog = Radix AlertDialog，`role="alertdialog"`）：
    // 它不响应外部点击，于是「弹窗还在 + 日志状态没变」能同时证明遮挡与不穿透。
    // （验证码弹窗是 `role="dialog"`，点遮罩会自行关闭，不适合做这条判据。）
    mock.pendingItems = [{
      interaction_id: 'toast-d1', operation_id: 'op-toast-modal', kind: 'order_retry', status: 'pending',
      created_at: 1, expires_at: Date.now() / 1000 + 300,
      request: {
        title: '订单定位失败', message: '请选择安全处理方式',
        choices: [{ value: 'retry', label: '重试', style: 'primary' }, { value: 'stop', label: '停止', style: 'danger' }],
      },
    }]
    // 8.5 里写入过订单密码 → 触发一次配置保存。未保存草稿会注册 beforeunload，
    // 刷新可能被拦下（页面根本没换），后面的弹窗自然就不会出现 —— 先等它落盘，
    // 再用带新鲜度断言的方式刷新，把「没刷新成功」和「刷新了但没弹窗」区分开。
    await cdp.waitFor(`document.body.innerText.includes('已保存')`, 5000)
    await cdp.eval('window.__reloadToken = "stale-document"')
    await cdp.send('Page.reload', { ignoreCache: true })
    assert.ok(await cdp.waitFor('window.__reloadToken === undefined', 10_000), '页面没有真正刷新（可能有 beforeunload 拦截）')
    await cdp.waitFor(`document.body && document.body.innerText.includes('任务工作台')`, 10_000)
    await new Promise((r) => setTimeout(r, 400))
    assert.ok(
      await cdp.waitFor(`document.querySelectorAll('[role="alertdialog"]').length === 1`, 5000),
      '决策模态弹窗未出现',
    )
    const modalFabProbe = await cdp.eval(toastProbeJs)
    const logBeforeModalClick = await logOpenNow()
    await realClickAt(cdp, modalFabProbe.fabCenter.x, modalFabProbe.fabCenter.y)
    await new Promise((r) => setTimeout(r, 400))
    const logAfterModalClick = await logOpenNow()
    const alertAfterClick = await cdp.eval(`document.querySelectorAll('[role="alertdialog"]').length`)
    record(
      '模态弹窗仍拦住背后的日志按钮：真实点击按钮坐标既不开合日志、也不关闭弹窗',
      modalFabProbe.fabHitIsFab === false && logAfterModalClick === logBeforeModalClick
        && alertAfterClick === 1,
      JSON.stringify({ fabHitIsFab: modalFabProbe.fabHitIsFab, fabHitDesc: modalFabProbe.fabHitDesc, before: logBeforeModalClick, after: logAfterModalClick, alertAfter: alertAfterClick }),
    )
    mock.pendingItems = []
    await reloadApp(cdp)

    // 8.7 桌面提示条行为不变：仍在右下角（手机端的顶部偏移不外溢）
    enterScenario('8c 桌面提示条位置不变')
    await cdp.viewport(1280, 800)
    await reloadApp(cdp)
    queueSyntheticLogSet()
    assert.ok(
      await cdp.waitFor(`document.querySelectorAll('.receipt-paper [data-log-row="true"]').length >= 4`, 8000),
      '桌面日志没有渲染出合成数据',
    )
    await cdp.eval(stubClipboardJs('ok'))
    assert.ok(await cdp.eval(clickSelectorJs('[data-log-tools]')), '桌面日志工具菜单打不开')
    assert.ok(await cdp.waitFor(`!!document.querySelector('[data-log-copy="true"]')`, 2500), '桌面工具菜单没有复制项')
    assert.ok(await cdp.eval(clickSelectorJs('[data-log-copy="true"]')), '桌面点击复制失败')
    await new Promise((r) => setTimeout(r, 500))
    const desktopToast = await cdp.eval(toastProbeJs)
    const desktopToastRect = desktopToast?.toasts?.[0]?.rect || null
    record(
      '桌面提示条行为不变：仍在右下角（手机端顶部偏移不外溢）',
      desktopToast?.count >= 1 && desktopToastRect
        && desktopToastRect.bottom > desktopToast.innerHeight * 0.6
        && desktopToastRect.right > desktopToast.innerWidth * 0.6,
      JSON.stringify({ count: desktopToast?.count, rect: desktopToastRect, innerHeight: desktopToast?.innerHeight, innerWidth: desktopToast?.innerWidth }),
    )
    await cdp.eval(restoreClipboardJs)
    await cdp.viewport(390, 844)
    await reloadApp(cdp)

    // ---------- 9. 手机端提示区域（ISSUE-05）：占正常布局空间，不遮关键内容 ----------
    // 背景：前一轮用「固定像素偏移」把提示条挪开日志按钮，但那只是把遮挡挪到别处
    // （任务状态行、失败原因横幅、日志头部、底栏动作都可能中招），长提示/多条提示/
    // 字体放大/横屏时偏移量还会失配。本轮改成：手机端提示**占正常布局空间**
    // （App.tsx 的 .phone-notice-region + index.css 的流式覆盖），它下面的内容整体下移，
    // 因此不可能盖住任何关键内容；桌面/平板仍是 sonner 默认的右下角浮层。
    enterScenario('9 手机端提示区域占布局')
    mock.operationStatus = idleOperation()
    mock.isAdmin = true
    mock.pendingItems = []
    await cdp.viewport(390, 844)
    await reloadApp(cdp)
    queueSyntheticLogSet()
    assert.ok(
      await cdp.waitFor(`!!document.querySelector('[data-phone-notice-region]')`, 4000),
      '手机布局没有渲染提示区域',
    )

    // 9.1 空闲态：区域不占布局（高度 0）—— 没有提示时布局与改动前完全一致
    const idleNotice = await cdp.eval(noticeRegionProbeJs)
    record(
      '无提示时提示区域完全不占布局（高度 0；标题栏仍从安全区起，主内容紧接标题栏）',
      idleNotice?.exists === true && idleNotice.height === 0
        && idleNotice.noticeHeightVar === '0px' && idleNotice.noticeBottomVar === '0px'
        && idleNotice.headerTop <= 1 && idleNotice.mainTop >= idleNotice.headerBottom - 1,
      JSON.stringify({ exists: idleNotice?.exists, height: idleNotice?.height, vars: [idleNotice?.noticeHeightVar, idleNotice?.noticeBottomVar], headerTop: idleNotice?.headerTop, headerBottom: idleNotice?.headerBottom, mainTop: idleNotice?.mainTop }),
    )

    /**
     * 复制一次日志拿到提示，然后把日志收起。
     *
     * 提示条由复制触发（入口在日志里），但「状态行/失败横幅/底栏动作有没有被遮住」
     * 必须在日志收起后才能判 —— 全屏日志层本身就会盖住它们，那时命中测试测的是日志层。
     */
    const toastThenCloseLog = async () => {
      await ensureLogOpen()
      await cdp.eval(stubClipboardJs('ok'))
      await copyFromTools()
      await ensureLogClosed()
    }

    // 9.2 有提示：区域占据布局空间，关键内容整体下移但不被覆盖
    await toastThenCloseLog()
    const noticeShown = await cdp.eval(noticeRegionProbeJs)
    const chromeShown = await cdp.eval(phoneChromeProbeJs)
    record(
      '有提示时：区域占据布局空间（高度 > 0），主内容从区域下方开始',
      noticeShown?.height > 0 && noticeShown.noticeHeightVar !== '0px' && noticeShown.noticeBottomVar !== '0px'
        && noticeShown.mainTop >= noticeShown.rect.bottom - 1,
      JSON.stringify({ height: noticeShown?.height, vars: [noticeShown?.noticeHeightVar, noticeShown?.noticeBottomVar], mainTop: noticeShown?.mainTop, regionBottom: noticeShown?.rect?.bottom }),
    )
    record(
      '提示不遮右上角日志按钮（区域/提示条都与按钮不相交，按钮中心命中按钮）',
      noticeShown?.toasts?.length >= 1 && noticeShown.fabHitIsFab === true
        && overlaps(noticeShown.rect, noticeShown.fab) === false
        && overlaps(noticeShown.toasts[0].rect, noticeShown.fab) === false,
      JSON.stringify({ toast: noticeShown?.toasts?.[0]?.rect, region: noticeShown?.rect, fab: noticeShown?.fab, fabHitIsFab: noticeShown?.fabHitIsFab, fabHitDesc: noticeShown?.fabHitDesc }),
    )
    record(
      '提示不遮任务工作台头部的权威状态区（不相交 + 状态胶囊命中自身 + 状态文案/说明仍在）',
      chromeShown?.status?.found === true && chromeShown.status.text.includes('任务工作台')
        && chromeShown.status.detail.length > 0 && chromeShown.status.pillRect !== null
        && overlaps(noticeShown?.rect, chromeShown.status.rect) === false
        && chromeShown.status.hitSelf === true,
      JSON.stringify(chromeShown?.status),
    )

    // 9.3 失败原因横幅（真实渲染：属于其他账号的待处理交互）不被遮挡
    enterScenario('9b 提示不遮失败原因横幅')
    mock.pendingItems = [{
      interaction_id: 'notice-r1', operation_id: 'op-notice', kind: 'captcha', status: 'pending',
      created_at: 1, expires_at: Date.now() / 1000 + 300, request: {}, request_redacted: true,
    }]
    await cdp.viewport(390, 844)
    await reloadApp(cdp)
    assert.ok(
      await cdp.waitFor(`document.body.innerText.includes('属于其他账号')`, 6000),
      '失败原因横幅（脱敏待处理）未出现',
    )
    await toastThenCloseLog()
    const bannerNotice = await cdp.eval(noticeRegionProbeJs)
    const bannerChrome = await cdp.eval(phoneChromeProbeJs)
    record(
      '提示不遮失败原因横幅（不相交 + 横幅文案仍在 + 中心命中横幅自身）',
      bannerChrome?.banner?.found === true && bannerChrome.banner.text.includes('属于其他账号')
        && overlaps(bannerNotice?.rect, bannerChrome.banner.rect) === false
        && bannerChrome.banner.hitSelf === true,
      JSON.stringify({ banner: bannerChrome?.banner, region: bannerNotice?.rect }),
    )
    mock.pendingItems = []

    // 9.4 日志层内的待处理输入（待确认地址）不被遮挡：提示区域不会盖住它
    enterScenario('9c 提示不遮待处理输入')
    mock.pendingItems = [{
      interaction_id: 'notice-a1', operation_id: 'op-notice', kind: 'address_input', status: 'pending',
      created_at: 2, expires_at: Date.now() / 1000 + 300,
      request: {
        title: '地址待确认', message: '请输入最终地址',
        items: [{ raw_address: '应标 D2', order_numbers: ['W100'], campus: '', confidence: '', reason: '合成', suggested_point: '' }],
      },
    }]
    await reloadApp(cdp)
    assert.ok(
      await cdp.waitFor(`!!document.querySelector('input[placeholder^="输入最终地址"]')`, 6000),
      '日志层内的待处理地址输入未出现',
    )
    await ensureLogOpen()
    await cdp.eval(stubClipboardJs('ok'))
    await copyFromTools()
    const inputNotice = await cdp.eval(noticeRegionProbeJs)
    const inputChrome = await cdp.eval(phoneChromeProbeJs)
    record(
      '提示不遮待处理输入（输入框在区域之下、不相交、中心命中自身）',
      inputChrome?.pendingInput?.found === true && inputNotice?.toasts?.length >= 1
        && inputChrome.pendingInput.rect.top >= inputNotice.rect.bottom - 1
        && overlaps(inputNotice.rect, inputChrome.pendingInput.rect) === false
        && inputChrome.pendingInput.hitSelf === true,
      JSON.stringify({ input: inputChrome?.pendingInput, region: inputNotice?.rect }),
    )
    mock.pendingItems = []
    await reloadApp(cdp)

    // 9.5 确认弹窗不被遮挡：提示还在时打开确认弹窗，弹窗在提示层之上且按钮可点
    enterScenario('9d 提示不遮确认弹窗')
    assert.ok(await cdp.eval(clickSelectorJs('#task-tab-order')), '切不到订单处理页签')
    if (!(await cdp.eval(`!!document.querySelector('#order-password')`))) {
      assert.ok(await cdp.eval(clickByTextJs('button', '管理网址与登录凭据')), '打不开高级设置')
      await new Promise((r) => setTimeout(r, 300))
    }
    const dialogSecret = await cdp.eval(setInputJs('#order-password', 'notice-dialog-secret'))
    assert.ok(dialogSecret?.ok === true, `写入订单密码失败：${JSON.stringify(dialogSecret)}`)
    await new Promise((r) => setTimeout(r, 400))
    await toastThenCloseLog()
    const dialogChromeBefore = await cdp.eval(phoneChromeProbeJs)
    assert.ok(dialogChromeBefore?.primary?.rect, '找不到底栏主按钮')
    await realClickAt(
      cdp,
      (dialogChromeBefore.primary.rect.left + dialogChromeBefore.primary.rect.right) / 2,
      (dialogChromeBefore.primary.rect.top + dialogChromeBefore.primary.rect.bottom) / 2,
    )
    await new Promise((r) => setTimeout(r, 500))
    const dialogGeom = await cdp.eval(dialogGeomJs)
    const dialogNotice = await cdp.eval(noticeRegionProbeJs)
    const enabledButtons = (dialogGeom?.buttons || []).filter((item) => item.disabled === false)
    record(
      '提示不遮确认弹窗（矩形不相交 + 弹窗按钮中心命中弹窗 + 提示仍在）',
      dialogGeom?.count === 1 && dialogGeom.text.includes('确认开始订单处理')
        && dialogNotice?.toasts?.length >= 1
        && overlaps(dialogNotice.rect, dialogGeom.rect) === false
        && enabledButtons.length >= 1 && enabledButtons.every((item) => item.hitSelf === true),
      JSON.stringify({ dialog: dialogGeom?.rect, text: dialogGeom?.text, buttons: dialogGeom?.buttons, region: dialogNotice?.rect }),
    )
    await cdp.send('Input.dispatchKeyEvent', { type: 'keyDown', key: 'Escape', code: 'Escape', windowsVirtualKeyCode: 27, nativeVirtualKeyCode: 27 })
    await cdp.send('Input.dispatchKeyEvent', { type: 'keyUp', key: 'Escape', code: 'Escape', windowsVirtualKeyCode: 27, nativeVirtualKeyCode: 27 })
    await new Promise((r) => setTimeout(r, 300))

    // 9.6 多条提示：区域内正常排布（互不重叠），区域高度随之增长
    enterScenario('9e 多条提示与字体放大')
    // 三次复制要挤在提示存活期内（sonner 默认可见 3 条）：先量单条高度，再连点两次。
    await toastThenCloseLog()
    await ensureLogOpen()
    await cdp.eval(stubClipboardJs('ok'))
    await copyFromTools()
    const singleHeight = (await cdp.eval(noticeRegionProbeJs))?.height ?? 0
    await copyFromTools()
    await copyFromTools()
    const manyNotice = await cdp.eval(noticeRegionProbeJs)
    const manyRects = (manyNotice?.toasts || []).map((item) => item.rect)
    let pairwiseOverlap = false
    for (let i = 0; i < manyRects.length; i += 1) {
      for (let j = i + 1; j < manyRects.length; j += 1) {
        if (overlaps(manyRects[i], manyRects[j])) pairwiseOverlap = true
      }
    }
    record(
      '多条提示在区域内依次排布（互不重叠）、区域高度随之增长，且不遮日志按钮',
      manyNotice?.toasts?.length >= 2 && pairwiseOverlap === false
        && manyNotice.height > singleHeight && manyNotice.fabHitIsFab === true,
      JSON.stringify({ count: manyNotice?.toasts?.length, singleHeight, manyHeight: manyNotice?.height, pairwiseOverlap, rects: manyRects }),
    )

    // 字体放大（模拟系统字体缩放）：提示变高但区域仍受 45vh 约束、关键内容不被覆盖
    await cdp.eval(`document.documentElement.style.fontSize = '20px'`)
    await new Promise((r) => setTimeout(r, 400))
    await copyFromTools()
    await ensureLogClosed()
    const zoomNotice = await cdp.eval(noticeRegionProbeJs)
    const zoomChrome = await cdp.eval(phoneChromeProbeJs)
    record(
      '字体放大（20px）后：提示区域仍受 45vh 上限约束（超出则区域内滚动），日志按钮与底栏主按钮仍可点',
      zoomNotice?.height > 0 && zoomNotice.maxHeightPx > 0 && zoomNotice.height <= zoomNotice.maxHeightPx + 1
        && zoomNotice.fabHitIsFab === true && zoomChrome?.primary?.hitSelf === true,
      JSON.stringify({ height: zoomNotice?.height, maxHeightPx: zoomNotice?.maxHeightPx, scrollable: zoomNotice?.scrollable, fabHitIsFab: zoomNotice?.fabHitIsFab, primaryHit: zoomChrome?.primary?.hitSelf, innerHeight: zoomNotice?.innerHeight }),
    )
    await cdp.eval(`document.documentElement.style.removeProperty('font-size')`)
    await new Promise((r) => setTimeout(r, 300))

    // 9.7 横屏：区域仍占布局空间、不越界、不压底栏
    enterScenario('9f 横屏与键盘')
    await cdp.viewport(568, 320)
    await new Promise((r) => setTimeout(r, 300))
    await toastThenCloseLog()
    const landscapeNotice = await cdp.eval(noticeRegionProbeJs)
    const landscapeChrome = await cdp.eval(phoneChromeProbeJs)
    record(
      '横屏（568x320）：提示区域 ≤45vh、不遮日志按钮，底栏主按钮仍可点',
      landscapeNotice?.height > 0 && landscapeNotice.height <= landscapeNotice.maxHeightPx + 1
        && landscapeNotice.fabHitIsFab === true
        && overlaps(landscapeNotice.rect, landscapeChrome?.primary?.rect) === false
        && landscapeChrome?.primary?.hitSelf === true,
      JSON.stringify({ height: landscapeNotice?.height, maxHeightPx: landscapeNotice?.maxHeightPx, rect: landscapeNotice?.rect, innerHeight: landscapeNotice?.innerHeight, primaryHit: landscapeChrome?.primary?.hitSelf }),
    )
    // 长内容兜底：横屏 320 高 + 字体放大 + 多条提示会把区域顶到 45vh 上限，
    // 此时必须是「区域内滚动」而不是继续挤压主内容。
    await cdp.eval(`document.documentElement.style.fontSize = '20px'`)
    await new Promise((r) => setTimeout(r, 300))
    await ensureLogOpen()
    await cdp.eval(stubClipboardJs('ok'))
    await copyFromTools()
    await copyFromTools()
    await ensureLogClosed()
    const cappedNotice = await cdp.eval(noticeRegionProbeJs)
    const cappedChrome = await cdp.eval(phoneChromeProbeJs)
    record(
      '内容超长（横屏 + 字体放大 + 多条提示）时：区域被 45vh 上限截住并可滚动，底栏主按钮仍可点',
      cappedNotice?.height > 0 && cappedNotice.maxHeightPx > 0
        && cappedNotice.height <= cappedNotice.maxHeightPx + 1
        && (cappedNotice.scrollable === true || cappedNotice.height < cappedNotice.maxHeightPx)
        && cappedChrome?.primary?.hitSelf === true && cappedNotice.fabHitIsFab === true,
      JSON.stringify({
        height: cappedNotice?.height, maxHeightPx: cappedNotice?.maxHeightPx,
        scrollable: cappedNotice?.scrollable, toasts: cappedNotice?.toasts?.length,
        region: cappedNotice?.rect, primary: cappedChrome?.primary, stop: cappedChrome?.stop,
        innerHeight: cappedChrome?.innerHeight,
      }),
    )
    await cdp.eval(`document.documentElement.style.removeProperty('font-size')`)
    await new Promise((r) => setTimeout(r, 300))
    await ensureLogClosed()

    // 运行中（「停止」可用）：提示显示期间底栏两个业务按钮都仍可点
    mock.operationStatus = {
      ...idleOperation(), status: 'running', active: true, mode: 'sss', phase: 'running',
      operation_id: 'op-notice-stop', reason: '正在执行闪时送下单', next_action: '',
    }
    await reloadApp(cdp)
    await toastThenCloseLog()
    const runningNotice = await cdp.eval(noticeRegionProbeJs)
    const runningChrome = await cdp.eval(phoneChromeProbeJs)
    record(
      '运行中提示显示期间：底栏不被提示遮挡，「停止」可命中（主按钮此时禁用是业务状态，不作命中判据）',
      runningNotice?.toasts?.length >= 1
        && overlaps(runningNotice.rect, runningChrome?.stop?.rect) === false
        && overlaps(runningNotice.rect, runningChrome?.primary?.rect) === false
        && runningChrome?.stop?.disabled === false && runningChrome.stop.hitSelf === true,
      JSON.stringify({ stop: runningChrome?.stop, primary: runningChrome?.primary, region: runningNotice?.rect }),
    )
    mock.operationStatus = idleOperation()
    await reloadApp(cdp)

    // 键盘顶起（可视高度变小）：提示区域仍在顶部，底栏仍可用
    await cdp.viewport(390, 430)
    await new Promise((r) => setTimeout(r, 300))
    await toastThenCloseLog()
    const keyboardNotice = await cdp.eval(noticeRegionProbeJs)
    const keyboardChrome = await cdp.eval(phoneChromeProbeJs)
    record(
      '软键盘顶起（390x430）：提示区域仍在可视区内，日志按钮与底栏主按钮仍可点',
      keyboardNotice?.height > 0 && keyboardNotice.rect.bottom <= keyboardNotice.innerHeight + 1
        && keyboardNotice.fabHitIsFab === true && keyboardChrome?.primary?.hitSelf === true,
      JSON.stringify({ rect: keyboardNotice?.rect, innerHeight: keyboardNotice?.innerHeight, fabHitIsFab: keyboardNotice?.fabHitIsFab, primaryHit: keyboardChrome?.primary?.hitSelf }),
    )
    await cdp.viewport(390, 844)

    // 9.8 桌面/平板：没有提示区域元素，提示仍是 sonner 默认的右下角浮层
    enterScenario('9g 桌面提示行为不变')
    await cdp.viewport(1280, 800)
    await reloadApp(cdp)
    queueSyntheticLogSet()
    assert.ok(
      await cdp.waitFor(`document.querySelectorAll('.receipt-paper [data-log-row="true"]').length >= 4`, 8000),
      '桌面日志没有渲染出合成数据',
    )
    await cdp.eval(stubClipboardJs('ok'))
    assert.ok(await cdp.eval(clickSelectorJs('[data-log-tools]')), '桌面日志工具菜单打不开')
    assert.ok(await cdp.waitFor(`!!document.querySelector('[data-log-copy="true"]')`, 2500), '桌面工具菜单没有复制项')
    assert.ok(await cdp.eval(clickSelectorJs('[data-log-copy="true"]')), '桌面点击复制失败')
    await new Promise((r) => setTimeout(r, 500))
    const desktopNotice = await cdp.eval(noticeRegionProbeJs)
    record(
      '桌面布局没有提示区域（不做流式占位），提示仍是右下角浮层',
      desktopNotice?.exists === false && desktopNotice?.toasts?.length >= 1
        && desktopNotice.toasts[0].rect.bottom > desktopNotice.innerHeight * 0.6
        && desktopNotice.toasts[0].rect.right > desktopNotice.innerWidth * 0.6,
      JSON.stringify({ exists: desktopNotice?.exists, toasts: desktopNotice?.toasts?.length, rect: desktopNotice?.toasts?.[0]?.rect, innerHeight: desktopNotice?.innerHeight }),
    )
    await cdp.eval(restoreClipboardJs)
    await cdp.viewport(390, 844)
    await reloadApp(cdp)

    /**
     * 运行中的状态会自动展开全屏日志层。批 2 的截图与断言都针对任务页本身，
     * 所以先等它展开、再点同一颗悬浮按钮收起（真实点击，不直接改状态）。
     */
    const closeLogSheetIfOpen = async (cdpClient, waitMs) => {
      const opened = await cdpClient.waitFor(
        `(() => { const s = document.getElementById('phone-log-sheet'); return Boolean(s) && !s.hasAttribute('inert') })()`,
        waitMs,
      )
      if (!opened) return false
      await cdpClient.eval(logFabClickJs)
      await cdpClient.waitFor(
        `document.getElementById('phone-log-sheet')?.hasAttribute('inert') === true`,
        3000,
      )
      return true
    }

    // ---------- 批 2：三页签 × 五状态（就绪／运行／成功／失败／待核对） ----------
    // 用户确认的方案：权威任务状态**只**出现在「任务工作台头部」一处。
    // 本段对每个页签 × 每个状态都真实渲染 + 真实点击，锁住三件事：
    //   1. 头部状态胶囊/说明是权威状态（label 必须匹配该状态）；
    //   2. **当前页签面板内**不得再有以该状态文案开头的重复块（= 状态不再同屏重复）；
    //   3. 说明行不被截断（横向省略号或纵向裁切都算失败）→ 失败原因必须完整可见。
    // 截图只在 CAPTURE_MOBILE_TASK=1 时产出（修改前后各跑一次即为前后对照）。
    enterScenario('批2 三页签状态矩阵')
    const BATCH2_CAPTURE = process.env.CAPTURE_MOBILE_TASK === '1'
    const BATCH2_LABEL = (process.env.CAPTURE_LABEL || 'run').replace(/[^a-z0-9-]/gi, '')
    const BATCH2_VIEWPORT = '390x844'
    /** 失败原因用真实长度的服务端文案：短说明看不出「被省略号截断」这个回归。 */
    const BATCH2_FAILURE_REASON =
      '登录管理后台失败：账号或密码错误，已连续失败 3 次，账号被临时锁定 15 分钟。'
      + '请先确认账号密码，或等待锁定解除后重试；不要重复提交以免延长锁定时间。'
    const BATCH2_TABS = [
      { key: 'order', mode: 'order' },
      { key: 'cloud', mode: 'wps_upload' },
      { key: 'sss', mode: 'sss' },
    ]
    const BATCH2_STATES = [
      {
        key: 'ready',
        expect: '就绪',
        allowComplete: true,
        view: () => idleOperation(),
      },
      {
        key: 'running',
        expect: '执行中',
        allowComplete: false,
        view: (mode) => ({
          ...idleOperation(), status: 'running', active: true, mode, phase: 'running',
          operation_id: `op-batch2-${mode}-running`, reason: '正在执行，日志会持续更新。', next_action: '',
        }),
      },
      {
        key: 'success',
        expect: '已完成',
        allowComplete: true,
        view: (mode) => ({
          ...idleOperation(), status: 'success', active: false, mode, phase: 'finished',
          operation_id: `op-batch2-${mode}-success`, summary: { status: 'success' },
          started_at: '2026-09-20T09:00:00', finished_at: '2026-09-20T09:05:00',
        }),
      },
      {
        key: 'error',
        expect: '失败',
        allowComplete: false,
        view: (mode) => ({
          ...idleOperation(), status: 'failed', active: false, mode, phase: 'finished',
          operation_id: `op-batch2-${mode}-failed`, reason: BATCH2_FAILURE_REASON, next_action: '',
          summary: { status: 'failed' },
        }),
      },
      {
        key: 'uncertain',
        expect: '待核对',
        allowComplete: false,
        view: (mode) => ({
          ...idleOperation(), status: 'uncertain', active: false, mode, phase: 'finished',
          operation_id: `op-batch2-${mode}-uncertain`, summary: { status: 'uncertain' },
          started_at: '2026-09-20T09:00:00', finished_at: '2026-09-20T09:05:00',
        }),
      },
    ]
    const batch2Matrix = {
      label: BATCH2_LABEL,
      viewport: BATCH2_VIEWPORT,
      capturedAt: new Date().toISOString(),
      cells: {},
    }
    for (const state of BATCH2_STATES) {
      for (const tab of BATCH2_TABS) {
        mock.operationStatus = state.view(tab.mode)
        await cdp.viewport(390, 844)
        await reloadApp(cdp)
        // 运行中会自动展开全屏日志层：等它真的展开，再点同一颗悬浮按钮收起 ——
        // 截图与断言都针对任务页本身。其它状态本就不会展开，只等很短时间。
        await closeLogSheetIfOpen(cdp, state.key === 'running' ? 3500 : 600)
        await new Promise((r) => setTimeout(r, 500))
        assert.ok(await cdp.eval(clickSelectorJs(`#task-tab-${tab.key}`)), `批2 切不到 ${tab.key} 页签`)
        await new Promise((r) => setTimeout(r, 450))
        const shotName = `batch2-${BATCH2_LABEL}-${state.key}-${tab.key}-390x844.png`
        if (BATCH2_CAPTURE) await captureScreenshot(cdp, shotName)
        const cell = await cdp.eval(taskStatusSurfaceJs)
        batch2Matrix.cells[`${state.key}/${tab.key}`] = { shot: BATCH2_CAPTURE ? shotName : null, ...cell }
        record(
          `批2 ${state.key} · ${tab.key}：权威状态只在任务工作台头部一处（页签内无重复状态块）`,
          Boolean(cell) && cell.headerLabel.includes(state.expect) && cell.duplicateBlocks.length === 0,
          JSON.stringify({ headerLabel: cell?.headerLabel, duplicateBlocks: cell?.duplicateBlocks }),
        )
        record(
          `批2 ${state.key} · ${tab.key}：状态说明完整可见（无横向省略号/纵向裁切）且无横向溢出`,
          Boolean(cell) && cell.headerDetail.length > 0 && cell.detailClipped === false && cell.overflow === false,
          JSON.stringify({
            detailClipped: cell?.detailClipped,
            detail: (cell?.headerDetail || '').slice(0, 48),
            overflow: cell?.overflow,
          }),
        )
        if (state.key === 'ready') {
          record(
            `批2 就绪 · ${tab.key}：同一句状态说明在整页只出现 1 次（普通状态不再同屏重复）`,
            Boolean(cell) && cell.detailOccurrences === 1,
            JSON.stringify({ detailOccurrences: cell?.detailOccurrences, detail: cell?.headerDetail }),
          )
        }
        if (!state.allowComplete) {
          record(
            `批2 ${state.key} · ${tab.key}：权威状态区不出现“已完成/上传完成”等完成文案`,
            Boolean(cell) && !/已完成|上传完成|处理完成|下单完成|写入完成/.test(cell.authoritativeText),
            JSON.stringify({ authoritativeText: cell?.authoritativeText }),
          )
        }
      }
    }
    // 窄屏与横屏：同一状态（待核对）+ 订单页签，确认无溢出、无遮挡。
    for (const [size, width, height] of [['narrow', 360, 800], ['landscape', 800, 450]]) {
      mock.operationStatus = BATCH2_STATES[4].view('order')
      await cdp.viewport(width, height)
      await reloadApp(cdp)
      await closeLogSheetIfOpen(cdp, 600)
      await new Promise((r) => setTimeout(r, 500))
      assert.ok(await cdp.eval(clickSelectorJs('#task-tab-order')), '批2 切不到订单页签')
      await new Promise((r) => setTimeout(r, 450))
      if (BATCH2_CAPTURE) await captureScreenshot(cdp, `batch2-${BATCH2_LABEL}-uncertain-order-${size}-${width}x${height}.png`)
      const cell = await cdp.eval(taskStatusSurfaceJs)
      batch2Matrix.cells[`uncertain/order@${size}`] = cell
      record(
        `批2 待核对 · 订单 @${width}x${height}：状态只在头部一处、说明完整、无横向溢出`,
        Boolean(cell) && cell.headerLabel.includes('待核对') && cell.duplicateBlocks.length === 0
          && cell.detailClipped === false && cell.overflow === false,
        JSON.stringify({ innerWidth: cell?.innerWidth, overflow: cell?.overflow, detailClipped: cell?.detailClipped }),
      )
    }
    if (BATCH2_CAPTURE) {
      // 高级设置展开后的样子（低频配置的归属位置：含移入的「保存到系统凭据管理器」）。
      mock.operationStatus = idleOperation()
      await cdp.viewport(390, 844)
      await reloadApp(cdp)
      await cdp.eval(clickSelectorJs('#task-tab-order'))
      await new Promise((r) => setTimeout(r, 400))
      // AdvancedSection 的 id 只在展开时才渲染，触发按钮用 aria-controls 定位。
      const advancedToggled = await cdp.eval(`(() => {
        const button = document.querySelector('button[aria-controls="order-advanced"]')
        if (!button) return false
        button.click()
        return true
      })()`)
      assert.ok(advancedToggled === true, '展开不了订单页高级设置')
      assert.ok(await cdp.waitFor(`!!document.getElementById('order-advanced')`, 2500), '高级设置展开后没有出现配置区')
      await new Promise((r) => setTimeout(r, 500))
      await captureScreenshot(cdp, `batch2-${BATCH2_LABEL}-order-advanced-390x844.png`)
      batch2Matrix.advancedToggled = advancedToggled
      batch2Matrix.advancedHasRememberSwitch = await cdp.eval(
        `!!document.querySelector('#order-advanced [role="switch"][aria-label="保存到系统凭据管理器"]')`,
      )
      batch2Matrix.advancedRememberChecked = await cdp.eval(
        `document.querySelector('#order-advanced [role="switch"][aria-label="保存到系统凭据管理器"]')?.getAttribute('data-state') ?? null`,
      )
      batch2Matrix.rememberSwitchOutsideAdvanced = await cdp.eval(`(() => {
        const panel = document.querySelector('[role="tabpanel"][data-hidden="false"]')
        if (!panel) return null
        const advanced = document.getElementById('order-advanced')
        return [...panel.querySelectorAll('[role="switch"][aria-label="保存到系统凭据管理器"]')]
          .some((el) => !advanced || !advanced.contains(el))
      })()`)
      fs.mkdirSync(SCREEN_DIR, { recursive: true })
      fs.writeFileSync(
        path.join(SCREEN_DIR, `mobile-task-matrix-${BATCH2_LABEL}.json`),
        `${JSON.stringify(batch2Matrix, null, 2)}\n`,
      )
      record(
        '批2 取证：三页签 × 五状态截图矩阵已产出（含高级设置展开）',
        Object.keys(batch2Matrix.cells).length >= 17,
        JSON.stringify({ dir: SCREEN_DIR, label: BATCH2_LABEL, cells: Object.keys(batch2Matrix.cells).length }),
      )
    }
    mock.operationStatus = idleOperation()

    // ---------- 批 2：页签切换不丢草稿 + 凭据开关移入高级设置后行为不变 ----------
    // 用户要求：页签切换不丢草稿；「保存到系统凭据管理器」只移动位置，
    // 默认值、保存语义与清除凭据行为都不能变。这里用真实点击与真实输入验证。
    enterScenario('批2 草稿与高级设置')
    mock.operationStatus = idleOperation()
    await cdp.viewport(390, 844)
    await reloadApp(cdp)
    await cdp.eval(clickSelectorJs('#task-tab-order'))
    await new Promise((r) => setTimeout(r, 350))

    // 订单草稿：点「增加」三次 → 待处理订单数 = 3（走真实交互，不直接写 state）
    for (let i = 0; i < 3; i += 1) {
      assert.ok(await cdp.eval(`(() => {
        const plus = [...document.querySelectorAll('#task-panel-order button')]
          .find((b) => (b.getAttribute('aria-label') || '') === '增加')
        if (!plus) return false
        plus.click()
        return true
      })()`), '订单页找不到「增加」按钮')
    }
    // 闪时送草稿：商品名称
    await cdp.eval(clickSelectorJs('#task-tab-sss'))
    await new Promise((r) => setTimeout(r, 300))
    const sssDraftSet = await cdp.eval(setInputJs('#sss-product', '草稿商品-不丢'))
    assert.ok(sssDraftSet?.ok === true, `闪时送草稿写入失败：${JSON.stringify(sssDraftSet)}`)
    // 三个页签来回切
    for (const tab of ['cloud', 'order', 'cloud', 'sss']) {
      await cdp.eval(clickSelectorJs(`#task-tab-${tab}`))
      await new Promise((r) => setTimeout(r, 250))
    }
    const drafts = await cdp.eval(`(() => ({
      orderCount: document.querySelector('#order-count')?.value ?? null,
      sssProduct: document.querySelector('#sss-product')?.value ?? null,
    }))()`)
    record(
      '批2 页签来回切换不丢草稿（订单数量 3 + 闪时送商品名都还在）',
      drafts?.orderCount === '3' && drafts.sssProduct === '草稿商品-不丢',
      JSON.stringify(drafts),
    )

    // 未展开高级设置时，订单页主流程里不应再出现凭据开关。
    await cdp.eval(clickSelectorJs('#task-tab-order'))
    await new Promise((r) => setTimeout(r, 300))
    const rememberBeforeExpand = await cdp.eval(`(() => {
      const panel = document.querySelector('[role="tabpanel"][data-hidden="false"]')
      return {
        switchesInPanel: panel ? panel.querySelectorAll('[role="switch"][aria-label="保存到系统凭据管理器"]').length : null,
        advancedOpen: Boolean(document.getElementById('order-advanced')),
      }
    })()`)
    record(
      '批2 凭据开关不再常驻主流程（未展开高级设置时订单页没有这个开关）',
      rememberBeforeExpand?.switchesInPanel === 0 && rememberBeforeExpand.advancedOpen === false,
      JSON.stringify(rememberBeforeExpand),
    )

    // 展开高级设置 → 开关应在这里，且默认仍是「保存」。
    assert.ok(await cdp.eval(`(() => {
      const button = document.querySelector('button[aria-controls="order-advanced"]')
      if (!button) return false
      button.click()
      return true
    })()`), '展开不了订单页高级设置')
    assert.ok(await cdp.waitFor(`!!document.getElementById('order-advanced')`, 2500), '高级设置展开后没有出现配置区')
    await new Promise((r) => setTimeout(r, 300))
    const rememberProbeJs = `(() => {
      const advanced = document.getElementById('order-advanced')
      const el = advanced?.querySelector('[role="switch"][aria-label="保存到系统凭据管理器"]')
      return {
        inside: Boolean(el),
        count: advanced ? advanced.querySelectorAll('[role="switch"][aria-label="保存到系统凭据管理器"]').length : 0,
        state: el?.getAttribute('data-state') ?? null,
      }
    })()`
    const switchBefore = await cdp.eval(rememberProbeJs)
    record(
      '批2 凭据开关已移入高级设置，且默认仍是「保存」（data-state=checked）',
      switchBefore?.inside === true && switchBefore.count === 1 && switchBefore.state === 'checked',
      JSON.stringify(switchBefore),
    )

    // 先在密码框里留一个草稿：切换开关不能顺手把它清掉（清除凭据是独立动作）。
    const passwordSet = await cdp.eval(setInputJs('#order-password', 'draft-keep-me'))
    assert.ok(passwordSet?.ok === true, `密码草稿写入失败：${JSON.stringify(passwordSet)}`)
    const clickRemember = `(() => {
      const el = document.getElementById('order-advanced')?.querySelector('[role="switch"][aria-label="保存到系统凭据管理器"]')
      if (!el) return false
      el.click()
      return true
    })()`
    assert.ok(await cdp.eval(clickRemember), '点不到凭据开关')
    await new Promise((r) => setTimeout(r, 250))
    const afterOff = await cdp.eval(`${rememberProbeJs}.state`)
    assert.ok(await cdp.eval(clickRemember), '第二次点不到凭据开关')
    await new Promise((r) => setTimeout(r, 250))
    const afterOn = await cdp.eval(`${rememberProbeJs}.state`)
    const passwordAfter = await cdp.eval(`document.querySelector('#order-password')?.value ?? null`)
    record(
      '批2 凭据开关行为不变：可开可关、默认值不改，且切换不会清掉密码草稿',
      afterOff === 'unchecked' && afterOn === 'checked' && passwordAfter === 'draft-keep-me',
      JSON.stringify({ afterOff, afterOn, passwordAfter }),
    )

    // ---------- R8：页面异常采集收尾（未预期异常 → 退出码非 0） ----------
    const { expected: expectedPageErrors, unexpected: unexpectedPageErrors } = classifyPageErrors()
    // 脱敏后的完整异常日志落盘（与截图同目录），便于人工复核与留档。
    try {
      fs.mkdirSync(SCREEN_DIR, { recursive: true })
      fs.writeFileSync(
        path.join(SCREEN_DIR, 'page-errors.json'),
        `${JSON.stringify({ totals: { collected: pageErrors.length, expected: expectedPageErrors.length, unexpected: unexpectedPageErrors.length }, expected: expectedPageErrors, unexpected: unexpectedPageErrors }, null, 2)}\n`,
      )
    } catch (error) {
      console.warn('页面异常日志落盘失败', error?.message || error)
    }
    console.log(`\n[R8] 页面异常采集：共 ${pageErrors.length} 条（预期 ${expectedPageErrors.length} / 未预期 ${unexpectedPageErrors.length}）；脱敏日志：${path.join(SCREEN_DIR, 'page-errors.json')}`)
    for (const item of expectedPageErrors) {
      console.log(`[R8] 预期内 ${item.entry.kind} @「${item.entry.scenario}」: ${item.entry.text} —— 白名单原因：${item.reason}`)
    }
    for (const item of unexpectedPageErrors) {
      console.log(`[R8] 未预期 ${item.kind} @「${item.scenario}」: ${item.text}${item.url ? ` (${item.url})` : ''}${item.stack ? ` | ${item.stack}` : ''}`)
    }
    if (process.env.UI_DEBUG_PAGE_ERRORS === '1') {
      console.log(`[R8] __PAGE_ERRORS__ ${JSON.stringify(pageErrors)}`)
    }
    record(
      'R8 异常采集通道自检：订阅已生效且基线确有预期内页面错误被采集',
      cdp.pageErrorsSubscribed === true && pageErrors.length > 0 && expectedPageErrors.length > 0,
      `subscribed=${cdp.pageErrorsSubscribed} total=${pageErrors.length} expected=${expectedPageErrors.length} kinds=${JSON.stringify(pageErrors.reduce((acc, item) => ({ ...acc, [item.kind]: (acc[item.kind] || 0) + 1 }), {}))}`,
    )
    record(
      'R8 页面无未预期异常（未捕获异常 / 未处理拒绝 / console.error / 资源错误）',
      unexpectedPageErrors.length === 0,
      `total=${pageErrors.length} expected=${expectedPageErrors.length} unexpected=${unexpectedPageErrors.length}`
        + (unexpectedPageErrors.length
          ? ` :: ${unexpectedPageErrors.slice(0, 5).map((item) => `${item.kind}@「${item.scenario}」: ${item.text.slice(0, 120)}`).join(' || ')}`
            + (unexpectedPageErrors.length > 5 ? ` || …(+${unexpectedPageErrors.length - 5} 条，见 page-errors.json)` : '')
          : ''),
    )

    console.log(`\n__RESULTS__ ${JSON.stringify(results)}`)
    const failed = results.filter((item) => !item.ok)
    if (failed.length > 0) process.exitCode = 1
  } finally {
    try { cdp?.ws?.close() } catch { /* ignore */ }
    chromeProc.kill('SIGKILL')
    server.close()
    try { fs.rmSync(profile, { recursive: true, force: true }) } catch { /* ignore */ }
  }
}

main().catch((error) => {
  console.error('BROWSER_CHECK_FAILED:', error)
  console.log(`\n__RESULTS__ ${JSON.stringify(results)}`)
  process.exitCode = 1
})
