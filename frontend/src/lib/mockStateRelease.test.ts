/**
 * B1 发布门禁：无后端 mock 初始状态不得携带任何真实手机号/账号或开发机绝对路径。
 *
 * 背景（`docs/OPTIMIZATION-DESKTOP-TEST-RELEASE-READINESS.md` §5.1）：
 * `mockState()` 的值会随 `frontend/dist/index.html` 内联进 APK 分发，
 * 之前这里是真实样式的手机号与 `/home/<user>/...` 开发机路径。
 *
 * 本文件只钉住"不含这类数据 + 无后端演示仍可用"两个不变式，
 * 不复制任何被移除的字面值（避免把疑似敏感值写回仓库）。
 */
import assert from 'node:assert/strict'
import test from 'node:test'

/** 中国大陆手机号样式；不放具体的号码。 */
const PHONE_LIKE = /(^|[^0-9])1[3-9][0-9]{9}([^0-9]|$)/
/** 开发机绝对路径样式：/home/<user>/... 或 /Users/<user>/... */
const DEV_HOME_PATH = /(^|\/)(home|Users)\/[A-Za-z0-9._-]+\//
/** Windows 用户目录样式，顺带覆盖。 */
const WIN_USER_PATH = /[A-Za-z]:\\Users\\/i

function stubBrowserGlobals(): void {
  const storage = new Map<string, string>()
  globalThis.window = {
    localStorage: {
      getItem: (key: string) => storage.get(key) ?? null,
      setItem: (key: string, value: string) => { storage.set(key, value) },
    },
    location: { search: '' },
  } as unknown as Window & typeof globalThis
  // 没有后端：握手必须失败，前端才会进入 mock 预览分支。
  globalThis.fetch = (async () => {
    throw new TypeError('Failed to fetch')
  }) as typeof fetch
}

test('B1：无后端 mock 状态不含真实手机号样式或开发机绝对路径', async () => {
  stubBrowserGlobals()
  const bridge = await import('./bridge.ts')
  const ready = await bridge.connectBridge()

  assert.equal(ready.mocked, true)
  assert.equal(ready.transport, 'mock')

  const config = ready.state.config as unknown as Record<string, unknown>
  const offenders: string[] = []
  for (const [key, value] of Object.entries(config)) {
    if (typeof value !== 'string' || value === '') continue
    if (PHONE_LIKE.test(value)) offenders.push(`${key}:手机号样式`)
    if (DEV_HOME_PATH.test(value)) offenders.push(`${key}:开发机绝对路径`)
    if (WIN_USER_PATH.test(value)) offenders.push(`${key}:Windows 用户目录路径`)
  }
  // 整个初始状态（含 passwords/version/operation）都要扫，不只 config。
  const serialized = JSON.stringify(ready.state)
  if (PHONE_LIKE.test(serialized)) offenders.push('state:手机号样式')
  if (DEV_HOME_PATH.test(serialized)) offenders.push('state:开发机绝对路径')
  if (WIN_USER_PATH.test(serialized)) offenders.push('state:Windows 用户目录路径')

  // 只报字段名与类别，不回显具体值。
  assert.deepEqual(offenders, [])
})

test('B1：凭据类与本地文件字段保持空值（不携带任何来源未确认的值）', async () => {
  stubBrowserGlobals()
  const bridge = await import('./bridge.ts')
  const { state } = await bridge.connectBridge()
  const config = state.config

  assert.equal(config.phone_number, '')
  assert.equal(config.sss_account, '')
  assert.equal(config.excel_path, '')
  assert.equal(config.sss_excel_path, '')
  assert.equal(config.wps_cli_path, '')
  assert.deepEqual(state.passwords, { order: '', sss: '' })
})

test('B1：无后端演示流程仍可用（有版本、有目标网址、操作状态为空闲）', async () => {
  stubBrowserGlobals()
  const bridge = await import('./bridge.ts')
  const { state } = await bridge.connectBridge()

  assert.equal(state.status, 'ready')
  assert.ok(state.version.length > 0)
  assert.equal(typeof state.config.target_url, 'string')
  assert.ok(state.config.target_url.startsWith('https://'))
  assert.equal(typeof state.config.sss_url, 'string')
  // 演示需要的非敏感默认值仍在（避免为了脱敏把演示弄空）。
  assert.equal(state.config.sss_order_source, 'wps')
  assert.equal(state.config.sss_dry_run, true)
  assert.equal(state.config.wps_test_mode, true)
  // 空闲操作：演示不会一进来就显示"执行中"。
  assert.equal(state.operation.active, false)
  assert.equal(state.operation.status, 'idle')
  assert.deepEqual(state.operations, [])
})
