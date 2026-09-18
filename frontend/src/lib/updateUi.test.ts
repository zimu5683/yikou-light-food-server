import assert from 'node:assert/strict'
import test from 'node:test'

import { formatBytes, updateProgressText } from './updateUi.ts'

test('formatBytes renders human readable sizes', () => {
  assert.equal(formatBytes(0), '0 B')
  assert.equal(formatBytes(512), '512 B')
  assert.equal(formatBytes(1024), '1.0 KB')
  assert.equal(formatBytes(19 * 1024 * 1024), '19.0 MB')
})

test('updateProgressText renders download size when total known', () => {
  assert.equal(
    updateProgressText('downloading', 50, 10 * 1024 * 1024, 20 * 1024 * 1024),
    '正在下载 50%（10.0 MB / 20.0 MB）',
  )
})

test('updateProgressText renders verify and install phases', () => {
  assert.equal(updateProgressText('verifying', 100), '正在校验安装包…')
  assert.equal(updateProgressText('installing', 100, undefined, undefined, '请完成安装'),
    '请完成安装')
  assert.equal(updateProgressText('installing', 100), '已打开系统安装器，请按手机提示完成安装')
})
