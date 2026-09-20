import assert from 'node:assert/strict'
import test from 'node:test'
import { RequestError, classifyRequestError, toRequestError } from './requestError.ts'

test('HTTP 401/403/5xx 分类为 auth/permission/server', () => {
  assert.equal(classifyRequestError(new RequestError('auth', 'token')).kind, 'auth')
  assert.equal(classifyRequestError(new RequestError('permission', 'denied')).kind, 'permission')
  assert.equal(classifyRequestError(new RequestError('server', 'boom')).kind, 'server')
})

test('超时与断网分类为可重试，且不把整批任务失败当成网络错误', () => {
  const timeout = new Error('The operation was aborted'); timeout.name = 'AbortError'
  assert.equal(classifyRequestError(timeout).kind, 'timeout')
  assert.equal(classifyRequestError(timeout).retryable, true)
  assert.match(classifyRequestError(timeout).nextStep, /不要立即重跑/)
  assert.equal(classifyRequestError(new TypeError('Failed to fetch')).kind, 'offline')
  assert.match(classifyRequestError(new TypeError('Failed to fetch')).title, /网络已断开/)
})

test('toRequestError 保留状态码与不可重试权限错误', () => {
  const e = new RequestError('permission', '仅管理员')
  assert.equal(toRequestError(e).retryable, false)
  assert.equal(toRequestError(new RequestError('client', 'bad'), 400).status, 400)
  assert.match(classifyRequestError({ status: 404 }).nextStep, /字段提示/)
})
