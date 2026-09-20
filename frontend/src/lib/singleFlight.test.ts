import assert from 'node:assert/strict'
import test from 'node:test'
import { SingleFlightGate } from './singleFlight.ts'

test('单飞闸门：在途期间的重复 begin 一律返回 null', () => {
  const gate = new SingleFlightGate()
  const first = gate.begin()
  assert.notEqual(first, null)
  assert.equal(gate.begin(), null)
  assert.equal(gate.begin(), null)
  assert.equal(gate.pending, true)
  gate.finish(first as number)
  assert.equal(gate.pending, false)
  const second = gate.begin()
  assert.notEqual(second, null)
  assert.notEqual(second, first)
})

test('单飞闸门：旧的 token 在更晚请求发出后不再 current', () => {
  const gate = new SingleFlightGate()
  const first = gate.begin() as number
  gate.finish(first)
  const second = gate.begin() as number
  assert.equal(gate.isCurrent(first), false)
  assert.equal(gate.isCurrent(second), true)
})

test('单飞闸门：finish 过期 token 不会误关掉在途状态', () => {
  const gate = new SingleFlightGate()
  const first = gate.begin() as number
  gate.finish(first)
  const second = gate.begin() as number
  // 迟到的旧 finish 不应影响当前在途请求。
  gate.finish(first - 1)
  assert.equal(gate.pending, true)
  gate.finish(second)
  assert.equal(gate.pending, false)
})
