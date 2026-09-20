import assert from 'node:assert/strict'
import test from 'node:test'
import {
  SaveCoordinator,
  saveStateView,
  type ConfigSaveResult,
} from './saveState.ts'

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason?: unknown) => void
  const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej })
  return { promise, resolve, reject }
}

test('延迟保存期间继续编辑：旧完成不标已保存，自动串行保存最新值', async () => {
  const coordinator = new SaveCoordinator()
  const requests: string[] = []
  let latest = 'A'
  const first = deferred<ConfigSaveResult>()
  const runner = async (): Promise<ConfigSaveResult> => {
    requests.push(latest)
    if (requests.length === 1) return first.promise
    return { ok: true }
  }

  coordinator.markEdited() // 用户输入 A -> version 1
  const running = coordinator.run(runner)
  assert.deepEqual(requests, ['A'])
  coordinator.markEdited() // A 未返回，用户改为 B -> version 2
  latest = 'B'
  assert.equal(await coordinator.run(runner), false) // 不允许并发
  assert.equal(coordinator.getState().pending, true)
  assert.match(saveStateView(coordinator.getState()).label, /新改动待保存/)

  first.resolve({ ok: true })
  await running
  assert.deepEqual(requests, ['A', 'B'])
  const state = coordinator.getState()
  assert.equal(state.phase, 'saved')
  assert.equal(state.draftVersion, 2)
  assert.equal(state.savedVersion, 2)
  assert.equal(state.pending, false)
})

test('旧保存在途失败：保留最新草稿，重试保存最新版本', async () => {
  const coordinator = new SaveCoordinator()
  const requests: string[] = []
  let latest = 'A'
  const first = deferred<ConfigSaveResult>()
  const runner = async (): Promise<ConfigSaveResult> => {
    requests.push(latest)
    if (requests.length === 1) return first.promise
    return { ok: true }
  }

  coordinator.markEdited()
  const running = coordinator.run(runner)
  coordinator.markEdited() // 保存中改为 B
  latest = 'B'
  first.resolve({ ok: false, reason: '网络失败' })
  assert.equal(await running, false)
  const failed = coordinator.getState()
  assert.equal(failed.phase, 'error')
  assert.equal(failed.draftVersion, 2)
  assert.equal(failed.savedVersion, 0)
  assert.equal(failed.error, '网络失败')
  assert.deepEqual(requests, ['A'])

  const retried = await coordinator.run(runner)
  assert.equal(retried, true)
  assert.deepEqual(requests, ['A', 'B'])
  assert.equal(coordinator.getState().phase, 'saved')
  assert.equal(coordinator.getState().savedVersion, 2)
})

test('连续点击立即保存不会产生并发重复请求', async () => {
  const coordinator = new SaveCoordinator()
  const first = deferred<ConfigSaveResult>()
  let calls = 0
  const runner = async (): Promise<ConfigSaveResult> => {
    calls += 1
    return first.promise
  }
  coordinator.markEdited()
  const a = coordinator.run(runner)
  const b = coordinator.run(runner)
  const c = coordinator.run(runner)
  assert.equal(await b, false)
  assert.equal(await c, false)
  assert.equal(calls, 1)
  assert.equal(coordinator.getState().pending, false, '同版本重复点击不制造 pending')
  first.resolve({ ok: true })
  assert.equal(await a, true)
  assert.equal(calls, 1, '没有新编辑时不应重复提交')
  assert.equal(coordinator.getState().phase, 'saved')
})

test('hasUnsavedChanges 覆盖 dirty/saving/error/pending', () => {
  const coordinator = new SaveCoordinator()
  assert.equal(coordinator.hasUnsavedChanges(), false)
  coordinator.markEdited()
  assert.equal(coordinator.hasUnsavedChanges(), true)
})
