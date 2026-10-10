/**
 * `lib/sssDiagnostics.ts` 的回归锁。
 *
 * 诊断日志查看/导出跑在 Android WebView 里，涉及：
 * 1. 文件名只接受 `*.jsonl`（目录穿越/其它后缀一律拒绝，与后端校验同口径）；
 * 2. 保存文件名带前缀且不携带路径；
 * 3. 大小与截断提示不编造数字。
 */
import assert from 'node:assert/strict'
import test from 'node:test'

import {
  SSS_DIAGNOSTICS_READ_LIMIT,
  diagnosticsDownloadName,
  diagnosticsFileName,
  diagnosticsTailNote,
  formatBytes,
  resolveExportBridge,
} from './sssDiagnostics.ts'

test('formatBytes 覆盖 B/KB/MB 与非法值', () => {
  assert.equal(formatBytes(0), '0 B')
  assert.equal(formatBytes(-1), '0 B')
  assert.equal(formatBytes(Number.NaN), '0 B')
  assert.equal(formatBytes(512), '512 B')
  assert.equal(formatBytes(2048), '2.0 KB')
  assert.equal(formatBytes(1024 * 1024 * 3 / 2), '1.50 MB')
})

test('diagnosticsFileName 只接受裸的 .jsonl 文件名', () => {
  assert.equal(diagnosticsFileName('2026-10-09.jsonl'), '2026-10-09.jsonl')
  assert.equal(diagnosticsFileName(' 2026-10-09.jsonl '), '2026-10-09.jsonl')
  assert.equal(diagnosticsFileName('a/b.jsonl'), '')      // 带路径一律拒绝（与后端同口径）
  assert.equal(diagnosticsFileName('..\\x.jsonl'), '')
  assert.equal(diagnosticsFileName('../config.json'), '')
  assert.equal(diagnosticsFileName('x.txt'), '')
  assert.equal(diagnosticsFileName(''), '')
  assert.equal(diagnosticsFileName('.jsonl'), '')
})

test('diagnosticsDownloadName 加前缀且不含路径', () => {
  assert.equal(diagnosticsDownloadName('2026-10-09.jsonl'), 'sss-diagnostics-2026-10-09.jsonl')
  assert.equal(diagnosticsDownloadName('../x.jsonl'), '')
  assert.equal(diagnosticsDownloadName('bad.txt'), '')
})

test('diagnosticsTailNote 只在截断时给提示', () => {
  assert.equal(diagnosticsTailNote(false), '')
  const note = diagnosticsTailNote(true)
  assert.match(note, /末尾/)
  assert.match(note, new RegExp(formatBytes(SSS_DIAGNOSTICS_READ_LIMIT).replace('.', '\\.')))
})

test('resolveExportBridge 只认完整的原生桥，残缺/缺失都回退网页下载', () => {
  const full = { saveDiagnostics: () => {}, shareDiagnostics: () => {} }
  assert.equal(resolveExportBridge({ YikouExport: full }), full)
  // 缺方法（旧版本 APK 只注入一半）→ 视为没有原生桥，走网页下载兜底。
  assert.equal(resolveExportBridge({ YikouExport: { saveDiagnostics: () => {} } }), null)
  assert.equal(resolveExportBridge({ YikouExport: { shareDiagnostics: () => {} } }), null)
  assert.equal(resolveExportBridge({ YikouExport: {} }), null)
  assert.equal(resolveExportBridge({ YikouExport: 'yes' }), null)
  assert.equal(resolveExportBridge({}), null)
  assert.equal(resolveExportBridge(null), null)
  assert.equal(resolveExportBridge(undefined), null)
})
