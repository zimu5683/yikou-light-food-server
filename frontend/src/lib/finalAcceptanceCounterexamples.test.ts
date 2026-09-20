/**
 * 独立反证：pending interaction 恢复、重复解决、权限/过期与密码清除决策。
 *
 * 只测纯逻辑；真实浏览器/软键盘布局没有依赖可自动化，保持“未验证”结论，
 * 不伪造渲染测试。
 */
import assert from 'node:assert/strict'
import test from 'node:test'
import {
  beginInteractionSubmit,
  captchaFromPendingItem,
  decisionFromPendingItem,
  dedupePendingInteractions,
  endInteractionSubmit,
  interactionRecoveryView,
  submissionOutcome,
  type PendingInteractionItem,
} from './interactionRecovery.ts'
import { evaluatePasswordClearResult } from './credentialClear.ts'
import { taskOutcomeView } from './taskOutcome.ts'

function item(overrides: Partial<PendingInteractionItem> = {}): PendingInteractionItem {
  return {
    interaction_id: 'd1',
    operation_id: 'op1',
    kind: 'sss_retry',
    created_at: 1,
    expires_at: 999,
    status: 'pending',
    request: {
      title: '确认',
      message: 'm',
      choices: [{ value: 'retry', label: '重试', style: 'primary' }],
    },
    ...overrides,
  }
}

test('pending interaction 去重按 id，重复/空 id 不会产生双弹窗', () => {
  const items = [
    item({ interaction_id: 'd1', created_at: 2 }),
    item({ interaction_id: 'd1', created_at: 1 }),
    item({ interaction_id: '' }),
    item({ interaction_id: 'c1', kind: 'captcha', created_at: 3,
           request: { image: 'png' } }),
  ]
  const out = dedupePendingInteractions(items)
  assert.deepEqual(out.map((entry) => entry.interaction_id), ['d1', 'c1'])
  assert.equal(decisionFromPendingItem(out[0])?.id, 'd1')
  assert.equal(captchaFromPendingItem(out[1])?.id, 'c1')
})

test('刷新恢复只重建弹窗内容；缺少关键字段时不伪造恢复', () => {
  const missingChoices = item({ request: { title: 't', message: 'm', choices: [] } })
  assert.equal(decisionFromPendingItem(missingChoices), null)
  const missingImage = item({ kind: 'captcha', request: {} })
  assert.equal(captchaFromPendingItem(missingImage), null)
})

test('重复 resolve 的本地闸门：同 key 只能有一个在途', () => {
  const pending = new Set<string>()
  const key = 'decision:d1'
  assert.equal(beginInteractionSubmit(pending, key), true)
  assert.equal(beginInteractionSubmit(pending, key), false)
  endInteractionSubmit(pending, key)
  assert.equal(beginInteractionSubmit(pending, key), true)
})

test('提交结果语义：ok=false 才是结束并清请求，异常/断线保留输入', () => {
  assert.deepEqual(submissionOutcome({ ok: true }).tone, 'resolved')
  assert.equal(submissionOutcome({ ok: false }).tone, 'ended')
  assert.equal(submissionOutcome({ ok: false }).clearRequest, true)
  const thrown = submissionOutcome({ ok: false, thrown: true, message: '断网' })
  assert.equal(thrown.tone, 'retryable')
  assert.equal(thrown.clearRequest, false)
  assert.equal(thrown.keepInput, true)
  assert.equal(thrown.autoSubmit, false)
})

test('断线时 pending interaction 只给安全降级，不自动提交', () => {
  const noApi = interactionRecoveryView({
    hasLocalRequest: false, hasPersistedMeta: true,
    connection: 'disconnected', recovery: 'idle', operationActive: true,
  })
  assert.equal(noApi.visible, true)
  assert.equal(noApi.autoSubmit, false)
  assert.equal(noApi.canStop, true)
  assert.match(noApi.detail, /不会自动重复提交/)

  const withApi = interactionRecoveryView({
    hasLocalRequest: true, hasPersistedMeta: false,
    connection: 'connected', recovery: 'idle', operationActive: true,
    hasServerReadApi: true,
  })
  assert.equal(withApi.visible, false, '本地弹窗仍在时不额外遮挡')
  assert.equal(withApi.autoSubmit, false)
})

test('密码清除失败/未知/网络中断不能清空草稿或提示成功', () => {
  for (const state of ['delete_failed', 'delete_error', 'delete_unconfirmed',
                       'already_absent_or_unavailable', 'unknown']) {
    const decision = evaluatePasswordClearResult({
      ok: true, status: 'error', state, mode: 'order', deleted: false,
      reason: 'r', next_action: 'n', summary: {},
    })
    assert.equal(decision.clearDraft, false, state)
    assert.equal(decision.toast, 'error', state)
    assert.notEqual(decision.nextAction, '')
  }
  const thrown = evaluatePasswordClearResult(null, '网络错误')
  assert.equal(thrown.clearDraft, false)
  assert.match(thrown.reason, /网络错误/)
})

test('taskOutcome 不把 uncertain/partial/blocked 说成功', () => {
  for (const status of ['uncertain', 'partial', 'blocked_uncertain', 'failed']) {
    const view = taskOutcomeView({
      status, message: 'm', ok: false, success: false,
      uncertain: status === 'uncertain',
      blocked: status === 'blocked_uncertain',
      partial: status === 'partial',
    })
    assert.equal(view.isSuccess, false, status)
    assert.ok(view.toast === 'warning' || view.toast === 'error', status)
    assert.notEqual(view.toast, 'success', status)
  }
})
