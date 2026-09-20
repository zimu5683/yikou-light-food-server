/**
 * D 自动保存独立反证：延迟/乱序/失败后继续编辑/重复点击。
 *
 * 驱动真实的 SaveCoordinator 异步状态机与保存提示，不做 reducer 单点断言；
 * runner 模拟“服务端当前值”，验证最终保存值与最新输入一致。
 */
import assert from 'node:assert/strict'
import test from 'node:test'
import { SaveCoordinator, saveStateView, type ConfigSaveResult } from './saveState.ts'

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason?: unknown) => void
  const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej })
  return { promise, resolve, reject }
}

test('A 未结束时输入 B：旧完成不把 B 标成已保存，服务端最终值为 B', async () => {
  const coordinator = new SaveCoordinator()
  const first = deferred<ConfigSaveResult>()
  let latest = 'A'
  let serverValue = ''
  const requests: string[] = []
  const runner = async (): Promise<ConfigSaveResult> => {
    const current = latest
    requests.push(current)
    if (requests.length === 1) return first.promise
    serverValue = current
    return { ok: true }
  }

  coordinator.markEdited()
  const running = coordinator.run(runner)
  assert.deepEqual(requests, ['A'])

  latest = 'B'
  coordinator.markEdited()
  assert.equal(await coordinator.run(runner), false, '保存中重复触发不得并发')
  assert.match(saveStateView(coordinator.getState()).label, /新改动待保存/)

  first.resolve({ ok: true })
  assert.equal(await running, true)
  assert.deepEqual(requests, ['A', 'B'])
  assert.equal(serverValue, 'B')
  const state = coordinator.getState()
  assert.equal(state.savedVersion, state.draftVersion)
  assert.equal(state.pending, false)
  assert.equal(saveStateView(state).label, '已保存')
})

test('旧版本成功返回不能覆盖最新输入：旧刷新返回后再串行保存最新值', async () => {
  const coordinator = new SaveCoordinator()
  const first = deferred<ConfigSaveResult>()
  const second = deferred<ConfigSaveResult>()
  let latest = 'old'
  let serverValue = ''
  const requests: string[] = []
  const runner = async (): Promise<ConfigSaveResult> => {
    const current = latest
    requests.push(current)
    if (requests.length === 1) {
      return first.promise // 旧请求延迟返回
    }
    return second.promise.then(() => {
      serverValue = current
      return { ok: true }
    })
  }

  coordinator.markEdited()
  const running = coordinator.run(runner)
  latest = 'new'
  coordinator.markEdited()

  // 旧请求先返回 success；此时新草稿仍未保存，状态不能是“已保存”。
  first.resolve({ ok: true })
  await new Promise((resolve) => setTimeout(resolve, 0))
  assert.equal(serverValue, '', '旧成功返回不得把新草稿当已保存')
  assert.equal(coordinator.getState().phase, 'saving')
  assert.match(saveStateView(coordinator.getState()).label, /保存中/)

  second.resolve({ ok: true })
  assert.equal(await running, true)
  assert.deepEqual(requests, ['old', 'new'])
  assert.equal(serverValue, 'new')
  assert.equal(coordinator.getState().phase, 'saved')
  assert.equal(saveStateView(coordinator.getState()).label, '已保存')
})

test('失败后继续编辑：保留最新草稿，重试保存最新版本且提示一致', async () => {
  const coordinator = new SaveCoordinator()
  const first = deferred<ConfigSaveResult>()
  let latest = 'A'
  let serverValue = ''
  const requests: string[] = []
  const runner = async (): Promise<ConfigSaveResult> => {
    const current = latest
    requests.push(current)
    if (requests.length === 1) return first.promise
    serverValue = current
    return { ok: true }
  }

  coordinator.markEdited()
  const running = coordinator.run(runner)
  first.resolve({ ok: false, reason: '网络失败' })
  assert.equal(await running, false)
  assert.equal(coordinator.getState().phase, 'error')
  assert.match(saveStateView(coordinator.getState()).label, /保存失败/)

  latest = 'B'
  coordinator.markEdited()
  assert.equal(coordinator.getState().phase, 'dirty')
  assert.match(saveStateView(coordinator.getState()).label, /未保存/)

  assert.equal(await coordinator.run(runner), true)
  assert.deepEqual(requests, ['A', 'B'])
  assert.equal(serverValue, 'B')
  assert.equal(coordinator.getState().savedVersion, coordinator.getState().draftVersion)
  assert.equal(saveStateView(coordinator.getState()).label, '已保存')
})

test('重复点击保存不产生并发请求；无新编辑时不重复提交', async () => {
  const coordinator = new SaveCoordinator()
  const first = deferred<ConfigSaveResult>()
  let calls = 0
  const runner = async (): Promise<ConfigSaveResult> => {
    calls += 1
    return first.promise
  }

  coordinator.markEdited()
  const attempts = [
    coordinator.run(runner),
    coordinator.run(runner),
    coordinator.run(runner),
    coordinator.run(runner),
  ]
  assert.equal(calls, 1, '在途重复点击只能有一次 runner')
  const results = await Promise.all(attempts.slice(1))
  assert.deepEqual(results, [false, false, false])

  first.resolve({ ok: true })
  assert.equal(await attempts[0], true)
  assert.equal(calls, 1)
  assert.equal(coordinator.getState().phase, 'saved')

  const noChange = await coordinator.run(runner)
  assert.equal(noChange, true, '已保存且无新编辑时立即保存不应重复请求')
  assert.equal(calls, 1)
})
