import assert from 'node:assert/strict'
import test from 'node:test'
import {
  PENDING_INTERACTION_KEY,
  addressFromPendingItem,
  beginInteractionSubmit,
  captchaFromPendingItem,
  decisionFromPendingItem,
  dedupePendingInteractions,
  endInteractionSubmit,
  interactionRecoveryView,
  readPendingInteractionMeta,
  submissionOutcome,
  writePendingInteractionMeta,
  type StorageLike,
} from './interactionRecovery.ts'

function memoryStorage(initial: Record<string, string> = {}): StorageLike & { dump(): Record<string, string> } {
  const map = new Map(Object.entries(initial))
  return {
    getItem: (key) => map.get(key) ?? null,
    setItem: (key, value) => { map.set(key, value) },
    removeItem: (key) => { map.delete(key) },
    dump: () => Object.fromEntries(map),
  }
}

test('断线后恢复：保留本地输入并提示重新核对，绝不自动提交', () => {
  const disconnected = interactionRecoveryView({
    hasLocalRequest: true, hasPersistedMeta: false,
    connection: 'disconnected', recovery: 'idle', operationActive: true,
  })
  assert.equal(disconnected.visible, true)
  assert.equal(disconnected.autoSubmit, false)
  assert.equal(disconnected.canRetry, false)
  assert.match(disconnected.detail, /重新核对/)
  assert.match(disconnected.detail, /不会自动重复提交/)
  assert.equal(disconnected.canStop, true)

  const recovered = interactionRecoveryView({
    hasLocalRequest: true, hasPersistedMeta: false,
    connection: 'connected', recovery: 'recovered', operationActive: true,
  })
  assert.equal(recovered.visible, true)
  assert.equal(recovered.autoSubmit, false)
  assert.match(recovered.title, /重新核对/)
  assert.equal(recovered.canRetry, true)
})

test('刷新后恢复：只安全元数据不伪造弹窗，显示 operation_id 与待核对', () => {
  const storage = memoryStorage()
  writePendingInteractionMeta(storage, {
    kind: 'address_input', id: 'd12', operationId: 'op-1', updatedAt: 123,
  })
  const meta = readPendingInteractionMeta(storage)
  assert.deepEqual(meta, { kind: 'address_input', id: 'd12', operationId: 'op-1', updatedAt: 123 })
  assert.equal(JSON.stringify(storage.dump()).includes('客户'), false)
  assert.equal(JSON.stringify(storage.dump()).includes('地址内容'), false)

  const view = interactionRecoveryView({
    hasLocalRequest: false, hasPersistedMeta: true,
    connection: 'connecting', recovery: 'idle', operationActive: true,
  })
  assert.equal(view.visible, true)
  assert.equal(view.autoSubmit, false)
  assert.match(view.detail, /需要重新核对/)
  assert.match(view.detail, /不会自动提交/)

  writePendingInteractionMeta(storage, null)
  assert.equal(readPendingInteractionMeta(storage), null)
  assert.equal(PENDING_INTERACTION_KEY.length > 0, true)
})

test('刷新后损坏/非法元数据不会被伪造成已恢复', () => {
  const storage = memoryStorage({ [PENDING_INTERACTION_KEY]: '{not json' })
  assert.equal(readPendingInteractionMeta(storage), null)
  const storage2 = memoryStorage({ [PENDING_INTERACTION_KEY]: JSON.stringify({ kind: 'other', id: 'x' }) })
  assert.equal(readPendingInteractionMeta(storage2), null)
})

test('决策/验证码/地址提交失败都保留输入并可安全重试，不自动提交', () => {
  for (const kind of ['decision', 'captcha', 'address_input'] as const) {
    const outcome = submissionOutcome({ ok: false, thrown: true, message: `${kind} 网络失败` })
    assert.equal(outcome.clearRequest, false, `${kind} 不应清请求`)
    assert.equal(outcome.keepInput, true, `${kind} 应保留输入`)
    assert.equal(outcome.autoSubmit, false)
    assert.equal(outcome.tone, 'retryable')
    assert.match(outcome.message, /输入已保留/)
  }
})

test('提交成功后清请求不保留；服务端权威 ok=false 关闭并提示已结束', () => {
  const success = submissionOutcome({ ok: true })
  assert.equal(success.clearRequest, true)
  assert.equal(success.keepInput, false)
  assert.equal(success.tone, 'resolved')
  assert.equal(success.autoSubmit, false)

  const ended = submissionOutcome({ ok: false })
  assert.equal(ended.clearRequest, true)
  assert.equal(ended.keepInput, false)
  assert.equal(ended.tone, 'ended')
  assert.match(ended.message, /已不再等待/)
})

test('重复点击不会重复提交同一个 interaction', () => {
  const pending = new Set<string>()
  assert.equal(beginInteractionSubmit(pending, 'decision:d1'), true)
  assert.equal(beginInteractionSubmit(pending, 'decision:d1'), false)
  assert.equal(pending.size, 1)
  endInteractionSubmit(pending, 'decision:d1')
  assert.equal(beginInteractionSubmit(pending, 'decision:d1'), true)
})

test('pending_interactions 映射：按 interaction_id 去重且恢复弹窗内容', () => {
  const items = dedupePendingInteractions([
    {
      interaction_id: 'd1', operation_id: 'op-1', kind: 'order_retry', status: 'pending',
      created_at: 1, expires_at: 100,
      request: { title: '订单失败', message: '请选择', choices: [{ value: 'retry', label: '重试', style: 'primary' }] },
    },
    {
      interaction_id: 'd1', operation_id: 'op-1', kind: 'order_retry', status: 'pending',
      created_at: 2, expires_at: 100,
      request: { title: '重复项', message: '应被去重', choices: [{ value: 'x', label: 'X', style: 'neutral' }] },
    },
    {
      interaction_id: 'c2', operation_id: 'op-1', kind: 'captcha', status: 'pending',
      created_at: 3, expires_at: 100, request: { image: 'img-base64' },
    },
    {
      interaction_id: 'a3', operation_id: 'op-1', kind: 'address_input', status: 'pending',
      created_at: 4, expires_at: 100,
      request: { title: '地址确认', message: '填入', items: [{ raw_address: 'D2', order_numbers: ['W1'], campus: '', confidence: '', reason: '', suggested_point: '' }] },
    },
  ])
  assert.equal(items.length, 3)
  assert.equal(items.filter((item) => item.interaction_id === 'd1').length, 1)
  const decision = decisionFromPendingItem(items[0])
  assert.equal(decision?.title, '订单失败')
  assert.deepEqual(decision?.choices[0], { value: 'retry', label: '重试', style: 'primary' })
  const captcha = captchaFromPendingItem(items[1])
  assert.equal(captcha?.image, 'img-base64')
  const address = addressFromPendingItem(items[2])
  assert.equal(address?.items[0].raw_address, 'D2')
})

test('pending_interactions 不可用/无权限/过期结果是安全降级，不伪造恢复', () => {
  const view = interactionRecoveryView({
    hasLocalRequest: false, hasPersistedMeta: true,
    connection: 'connected', recovery: 'idle', operationActive: true,
    hasServerReadApi: true,
  })
  assert.equal(view.visible, true)
  assert.equal(view.autoSubmit, false)
  assert.match(view.title, /没有可恢复|需要重新核对/)
  assert.match(view.detail, /已解决|过期|取消|无权限|不会自动提交/)
})

test('request_redacted 的空 request 不会被拿来伪造恢复输入', () => {
  const item = {
    interaction_id: 'x1', operation_id: 'op-x', kind: 'address_input', status: 'pending' as const,
    created_at: 1, expires_at: 2, request: {}, request_redacted: true,
  }
  assert.equal(decisionFromPendingItem(item), null)
  assert.equal(captchaFromPendingItem(item), null)
  assert.equal(addressFromPendingItem(item), null)
})
