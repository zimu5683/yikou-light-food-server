/**
 * `lib/clipboard.ts` 的回归锁。
 *
 * 复制日志跑在 Android WebView 里，`navigator.clipboard` 在 `file://` 或
 * 权限被拒时**不存在或抛错**。要锁住的是：
 * 1. 有 Clipboard API 时用它；
 * 2. 没有 / 抛错时退回 textarea + execCommand，而不是直接失败；
 * 3. 两条路都不通时给出可执行的下一步，不静默失败；
 * 4. 空文本不写剪贴板（会清掉用户原有内容），直接判失败。
 *
 * 与 `theme.test.ts` 同一套做法：给 globalThis 打桩，不新增依赖、不需要 jsdom。
 */
import assert from 'node:assert/strict'
import test from 'node:test'

type Stubs = {
  /** 记录写入剪贴板的文本。 */
  written: string[]
  /** true = writeText 抛错（模拟非安全上下文 / 权限被拒）。 */
  clipboardThrows: boolean
  /** false = 模拟没有 navigator.clipboard。 */
  hasClipboard: boolean
  /** execCommand('copy') 的返回值。 */
  execResult: boolean
  /** execCommand 收到的命令。 */
  execCommands: string[]
  /** 当前挂在 body 上的临时 textarea 数量（应为 0，验证清理）。 */
  appended: number
}

function installStubs(overrides: Partial<Stubs> = {}): Stubs {
  // navigator 在安装时就定型，因此开关必须通过 overrides 传入（不能装完再改）。
  const state: Stubs = {
    written: [],
    clipboardThrows: false,
    hasClipboard: true,
    execResult: true,
    execCommands: [],
    appended: 0,
    ...overrides,
  }
  // Node 的 globalThis.navigator 只有 getter，必须用 defineProperty 覆盖。
  const clipboard = state.hasClipboard
    ? {
      async writeText(value: string) {
        if (state.clipboardThrows) throw new Error('NotAllowedError')
        state.written.push(value)
      },
    }
    : undefined
  Object.defineProperty(globalThis, 'navigator', {
    configurable: true,
    writable: true,
    value: { clipboard } as unknown as Navigator,
  })
  globalThis.document = {
    body: {
      appendChild(node: { remove?: () => void }) {
        state.appended += 1
        node.remove = () => { state.appended -= 1 }
        return node
      },
    },
    createElement() {
      return {
        value: '',
        style: { cssText: '' },
        setAttribute() {},
        select() {},
        setSelectionRange() {},
        remove() {},
      }
    },
    execCommand(command: string) {
      state.execCommands.push(command)
      return state.execResult
    },
  } as unknown as Document
  return state
}

// 模块不持有状态（每次调用才读 navigator/document），import 一次即可。
const { copyText } = await import('./clipboard.ts')

test('有 Clipboard API 时用它，且不触碰兜底路径', async () => {
  const state = installStubs()
  const outcome = await copyText('12:00:01 INFO 下单成功')
  assert.deepEqual(outcome, { ok: true })
  assert.deepEqual(state.written, ['12:00:01 INFO 下单成功'])
  assert.deepEqual(state.execCommands, [], '正常路径不该走 execCommand')
})

test('Clipboard API 抛错时退回 execCommand，而不是直接判失败', async () => {
  const state = installStubs()
  state.clipboardThrows = true
  const outcome = await copyText('完整原文')
  assert.deepEqual(outcome, { ok: true })
  assert.deepEqual(state.written, [], 'API 路径确实失败了')
  assert.deepEqual(state.execCommands, ['copy'], '兜底路径被调用')
  assert.equal(state.appended, 0, '临时 textarea 用完必须移除，不留 DOM 垃圾')
})

test('没有 navigator.clipboard（非安全上下文）时直接走兜底', async () => {
  const state = installStubs({ hasClipboard: false })
  const outcome = await copyText('file:// 下的日志')
  assert.deepEqual(outcome, { ok: true })
  assert.deepEqual(state.execCommands, ['copy'])
})

test('两条路都不通时给出可执行的下一步，不静默失败', async () => {
  const state = installStubs({ hasClipboard: false, execResult: false })
  const outcome = await copyText('复制不了的日志')
  assert.equal(outcome.ok, false)
  assert.ok(outcome.error && outcome.error.includes('长按'), `错误提示要给出下一步：${outcome.error}`)
  assert.equal(state.appended, 0, '失败路径也要清理临时节点')
})

test('execCommand 抛错也不能把异常抛给调用方', async () => {
  installStubs({ hasClipboard: false })
  globalThis.document = {
    ...(globalThis.document as unknown as Record<string, unknown>),
    execCommand() { throw new Error('execCommand disabled') },
  } as unknown as Document
  const outcome = await copyText('x')
  assert.equal(outcome.ok, false)
  assert.ok(outcome.error)
})

test('空文本不写剪贴板：直接判失败，避免清掉用户原有内容', async () => {
  const state = installStubs()
  const outcome = await copyText('')
  assert.equal(outcome.ok, false)
  assert.deepEqual(state.written, [], '不能把空字符串写进剪贴板')
  assert.deepEqual(state.execCommands, [], '兜底也不该执行')
  assert.ok(outcome.error)
})
