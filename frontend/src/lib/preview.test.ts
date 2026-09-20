import assert from 'node:assert/strict'
import test from 'node:test'
import {
  blockedItems,
  previewCounts,
  previewFreshness,
  previewLocalKey,
  uploadGate,
  uploadNextAction,
} from './preview.ts'
import type { WpsPreviewResult } from './bridge.ts'

function preview(overrides: Partial<WpsPreviewResult> = {}): WpsPreviewResult {
  return {
    ok: true,
    status: 'preview_ready',
    reason: '',
    next_action: 'wps_upload(preview_id)',
    summary: { to_update: 99, to_append: 99, unchanged: 99, warned: 99, skipped: 99 },
    stats: { to_update: 99, to_append: 99, unchanged: 99, warned: 99, skipped: 99 },
    operation_id: '',
    preview_id: 'pv-test-1',
    created_at: '2026-09-19T10:00:00+08:00',
    expires_at: '2026-09-19T10:10:00+08:00',
    expires_in: 600,
    ttl_seconds: 600,
    local_sha256: 'a',
    context_fingerprint: 'b',
    plan_fingerprint: 'c',
    fingerprint: { local_sha256: 'a', context: 'b', plan: 'c' },
    target_tables: { 东湖中餐: { file_id: 'F1' } },
    target_date: '2026-09-19',
    test_mode: true,
    tables: [
      {
        sheet: '东湖中餐', file_id: 'F1', target_date: '2026-09-19', target_col: 7,
        target_header: '9/19', weekday_number: 6, blocked_reason: '',
        counts: { to_update: 2, to_append: 1, unchanged: 3, skipped: 1, warned: 1, blocked: 0 },
        changes: [], insert_blocks: [], warnings: [], unknown_addresses: [],
      },
      {
        sheet: '东湖晚餐', file_id: 'F2', target_date: '2026-09-19', target_col: 9,
        target_header: '9/19', weekday_number: 6, blocked_reason: '本地表批次日期不符',
        counts: { to_update: 0, to_append: 0, unchanged: 0, skipped: 0, warned: 0, blocked: 1 },
        changes: [], insert_blocks: [], warnings: ['测试警告'], unknown_addresses: [],
      },
    ],
    blocked: [{ sheet: '东湖晚餐', reason: '本地表批次日期不符' }],
    warnings: ['东湖晚餐：测试警告'],
    text: '⛔ 本次未写入这张表 不应再被前端解析',
    ...overrides,
  }
}

test('previewCounts 只汇总结构化 tables.counts 与 blocked，不解析 text', () => {
  const counts = previewCounts(preview())
  assert.deepEqual(counts, { update: 2, append: 1, unchanged: 3, skipped: 1, warned: 1, blocked: 1 })
  const noTableCounts = previewCounts(preview({ tables: [], blocked: [], text: '⛔ 本次未写入这张表' }))
  assert.deepEqual(noTableCounts, { update: 99, append: 99, unchanged: 99, skipped: 99, warned: 99, blocked: 0 })
})

test('previewFreshness 使用后端 created_at/expires_at，并在本地 key 变化时立即失效', () => {
  const handle = { preview: preview(), localKey: 'key-a' }
  const now = Date.parse('2026-09-19T10:05:00+08:00')
  assert.equal(previewFreshness(handle, 'key-a', now).fresh, true)
  const changed = previewFreshness(handle, 'key-b', now)
  assert.equal(changed.fresh, false)
  assert.match(changed.reason, /重新预览/)
  const expired = previewFreshness(handle, 'key-a', Date.parse('2026-09-19T10:10:01+08:00'))
  assert.equal(expired.fresh, false)
  assert.match(expired.reason, /过期/)
  const noTime = previewFreshness({ preview: preview({ created_at: undefined as unknown as string }), localKey: 'key-a' }, 'key-a', now)
  assert.equal(noTime.fresh, false)
  assert.match(noTime.reason, /时间字段/)
})

test('uploadGate 在缺 token、缺 preview_id、过期、变化时全部拒绝', () => {
  const handle = { preview: preview(), localKey: 'key-a' }
  const now = Date.parse('2026-09-19T10:05:00+08:00')
  assert.equal(uploadGate({ hasValidToken: false, wpsEnabled: true, cliFound: true, authenticated: true, busy: false, handle, currentLocalKey: 'key-a', now }).ok, false)
  assert.match(uploadGate({ hasValidToken: false, wpsEnabled: true, cliFound: true, authenticated: true, busy: false, handle, currentLocalKey: 'key-a', now }).reason, /令牌/)
  assert.match(uploadGate({ hasValidToken: true, wpsEnabled: true, cliFound: true, authenticated: true, busy: false, handle: null, currentLocalKey: 'key-a', now }).reason, /preview_id/)
  const expired = uploadGate({ hasValidToken: true, wpsEnabled: true, cliFound: true, authenticated: true, busy: false, handle, currentLocalKey: 'key-a', now: Date.parse('2026-09-19T10:20:00+08:00') })
  assert.equal(expired.ok, false)
  assert.match(expired.reason, /过期/)
  const changed = uploadGate({ hasValidToken: true, wpsEnabled: true, cliFound: true, authenticated: true, busy: false, handle, currentLocalKey: 'key-b', now })
  assert.equal(changed.ok, false)
  assert.match(changed.reason, /变化/)
  assert.equal(uploadGate({ hasValidToken: true, wpsEnabled: true, cliFound: true, authenticated: true, busy: false, handle, currentLocalKey: 'key-a', now }).ok, true)
})

test('preview_changed / operation_conflict / uncertain 给出不同的下一步动作', () => {
  assert.equal(uploadNextAction({ code: 'preview_changed' }).label, '重新预览')
  assert.equal(uploadNextAction({ code: 'preview_expired' }).label, '重新预览')
  assert.equal(uploadNextAction({ code: 'operation_conflict', status: 'rejected' }).label, '查看操作状态')
  assert.equal(uploadNextAction({ status: 'uncertain' }).label, '重新核对')
  assert.equal(uploadNextAction({ status: 'partial' }).label, '重新核对')
})

test('previewLocalKey 对对象键顺序不敏感，对值变化敏感', () => {
  assert.equal(previewLocalKey({ b: 1, a: 2 }), previewLocalKey({ a: 2, b: 1 }))
  assert.notEqual(previewLocalKey({ a: 2, b: 1 }), previewLocalKey({ a: 3, b: 1 }))
  assert.equal(blockedItems(preview()).length, 1)
})

test('recovered/not_started 重新预览，blocked 先重新核对', () => {
  assert.equal(uploadNextAction({ status: 'recovered' }).label, '重新预览')
  assert.equal(uploadNextAction({ status: 'not_started' }).label, '重新预览')
  assert.equal(uploadNextAction({ status: 'blocked' }).label, '重新核对')
  assert.equal(uploadNextAction({ status: 'blocked' }).tone, 'danger')
})
