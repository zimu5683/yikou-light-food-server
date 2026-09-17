import assert from 'node:assert/strict'
import test from 'node:test'

import {
  AUTO_CHECK_INTERVAL_MS,
  AUTO_CHECK_STORAGE_KEY,
  shouldAutoCheckUpdates,
} from './updateCheck.ts'

function fakeStorage(initial: string | null = null) {
  const data = new Map<string, string>()
  if (initial !== null) data.set(AUTO_CHECK_STORAGE_KEY, initial)
  return {
    data,
    getItem: (key: string) => data.get(key) ?? null,
    setItem: (key: string, value: string) => { data.set(key, value) },
  }
}

test('first auto check writes timestamp and allows check', () => {
  const storage = fakeStorage()
  assert.equal(shouldAutoCheckUpdates(storage, 1_000_000), true)
  assert.equal(storage.getItem(AUTO_CHECK_STORAGE_KEY), '1000000')
})

test('auto check is throttled inside 6h window', () => {
  const storage = fakeStorage('1000000')
  assert.equal(
    shouldAutoCheckUpdates(storage, 1_000_000 + AUTO_CHECK_INTERVAL_MS - 1),
    false,
  )
})

test('auto check allowed after 6h', () => {
  const storage = fakeStorage('1000000')
  assert.equal(
    shouldAutoCheckUpdates(storage, 1_000_000 + AUTO_CHECK_INTERVAL_MS),
    true,
  )
})

test('malformed stored value is treated as no previous check', () => {
  const storage = fakeStorage('not-a-number')
  assert.equal(shouldAutoCheckUpdates(storage, 123), true)
})

test('throwing storage does not block the check', () => {
  const storage = {
    getItem: () => { throw new Error('denied') },
    setItem: () => { throw new Error('denied') },
  }
  assert.equal(shouldAutoCheckUpdates(storage, 123), true)
})
