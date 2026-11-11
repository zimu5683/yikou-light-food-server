/**
 * 「核对并补传」前端纯逻辑单测：订单/表状态文案、默认勾选、令牌有效期、
 * 提交闸门与结果标题口径。全部只读结构化字段，不依赖后端/浏览器。
 */
import assert from 'node:assert/strict'
import test from 'node:test'
import type {
  WpsReuploadCheckResult,
  WpsReuploadOrder,
  WpsReuploadTable,
  WpsReuploadUploadResult,
} from './bridge.ts'
import {
  reuploadCanSubmit,
  reuploadHeadline,
  reuploadInitialSelection,
  reuploadOrderMissingText,
  reuploadOrderStatusText,
  reuploadSelectedCount,
  reuploadSelectable,
  reuploadSheetStatusText,
  reuploadSheetTone,
  reuploadSummaryText,
  reuploadTokenFresh,
  reuploadWriteStatusText,
} from './reupload.ts'

function order(overrides: Partial<WpsReuploadOrder> = {}): WpsReuploadOrder {
  return {
    order_no: 'W1', name: '张三', phone: '111', slot: 1, kind: 'existing',
    local_meals: 2, status: 'present', missing: [], missing_meals: 0,
    expected_total: 4, before_total: 2, actual_total: 4,
    expected_mark: '1', actual_mark: '1', actual_row: 3, reason: '',
    ...overrides,
  }
}

function table(overrides: Partial<WpsReuploadTable> = {}): WpsReuploadTable {
  return {
    sheet: '东湖中餐', file_id: 'F1', target_date: '2026-11-11',
    status: 'ready', reason: '', pending: false,
    counts: { total: 1, to_resend: 1, present: 0, manual: 0 },
    orders: [order({ status: 'missing_meals', missing: ['meals', 'mark'], missing_meals: 2 })],
    warnings: [], target_col: 4, target_header: '11.11 周三',
    last_upload: { operation_id: 'wps-1' }, plan_fingerprint: 'fp',
    ...overrides,
  }
}

function check(overrides: Partial<WpsReuploadCheckResult> = {}): WpsReuploadCheckResult {
  return {
    ok: true, status: 'check_ready', reason: '', code: '', next_action: '',
    check_id: 'rw-1', created_at: '2026-11-10T21:00:00+08:00',
    expires_at: '2026-11-10T21:10:00+08:00', expires_in: 600,
    ttl_seconds: 600, state: 'valid',
    summary: { sheets: 1, writeable_sheets: 1, to_resend: 1, present: 0, manual: 0, complete_sheets: 0, blocked_sheets: 0 },
    tables: [table()], warnings: [], target_date: '2026-11-11', test_mode: false,
    ...overrides,
  }
}

const NOW = Date.parse('2026-11-10T21:05:00+08:00')

test('订单状态/缺项文案只读结构化字段', () => {
  assert.equal(reuploadOrderStatusText('missing_row'), '缺整行')
  assert.equal(reuploadOrderStatusText('unknown_status'), 'unknown_status')
  assert.equal(
    reuploadOrderMissingText(order({ missing: ['meals', 'mark'], missing_meals: 2 })),
    '少 2 餐、缺日期格',
  )
  assert.equal(reuploadOrderMissingText(order({ missing: ['row'] })), '缺整行')
  assert.equal(reuploadOrderMissingText(order({ missing: [] })), '不缺')
})

test('表状态语气与文案', () => {
  assert.equal(reuploadSheetTone('blocked'), 'danger')
  assert.equal(reuploadSheetTone('partial'), 'warning')
  assert.equal(reuploadSheetTone('complete'), 'success')
  assert.equal(reuploadSheetStatusText('ready'), '可补传')
})

test('只有 ready/partial 且有可补订单的表可勾选；默认全部勾选', () => {
  const ready = table()
  const complete = table({ sheet: '衣锦中餐', status: 'complete', counts: { total: 1, to_resend: 0, present: 1, manual: 0 } })
  const blocked = table({ sheet: '医学院中餐', status: 'blocked' })
  assert.equal(reuploadSelectable(ready), true)
  assert.equal(reuploadSelectable(complete), false)
  assert.equal(reuploadSelectable(blocked), false)
  assert.deepEqual(reuploadInitialSelection(check({ tables: [ready, complete, blocked] })), ['东湖中餐'])
})

test('选中计数与提交闸门：需有可补订单、令牌有效', () => {
  const data = check()
  assert.equal(reuploadSelectedCount(data, ['东湖中餐']), 1)
  assert.equal(reuploadSelectedCount(data, []), 0)
  assert.equal(reuploadCanSubmit(data, ['东湖中餐'], NOW), true)
  // 没勾选 → 不能提交
  assert.equal(reuploadCanSubmit(data, [], NOW), false)
  // 令牌过期 → 不能提交
  assert.equal(reuploadCanSubmit(data, ['东湖中餐'], Date.parse('2026-11-10T21:20:00+08:00')), false)
  // 没有 check_id → 不能提交
  assert.equal(reuploadCanSubmit(check({ check_id: undefined }), ['东湖中餐'], NOW), false)
  // 核对失败 → 不能提交
  assert.equal(reuploadCanSubmit(check({ ok: false }), ['东湖中餐'], NOW), false)
})

test('令牌有效期只来自服务端时间字段', () => {
  assert.equal(reuploadTokenFresh(check(), NOW).fresh, true)
  const expired = reuploadTokenFresh(check(), Date.parse('2026-11-10T21:10:00+08:00'))
  assert.equal(expired.fresh, false)
  assert.match(expired.reason, /过期/)
  assert.equal(reuploadTokenFresh(check({ expires_at: undefined }), NOW).fresh, false)
})

test('汇总行使用 summary 字段', () => {
  assert.match(reuploadSummaryText(check()), /需补传 1 单/)
  assert.match(
    reuploadSummaryText(check({
      summary: { sheets: 2, writeable_sheets: 1, to_resend: 3, present: 5, manual: 2, complete_sheets: 1, blocked_sheets: 0 },
    })),
    /无法判定 2 单/,
  )
})

test('补传结果标题：只有 ok+ok 才叫完成，未知不冒充成功', () => {
  const okResult: WpsReuploadUploadResult = {
    ok: true, status: 'ok', reason: '', next_action: '', operation_id: 'op-1',
    check_id: 'rw-1', target_date: '2026-11-11', test_mode: false,
    tables: [table({ write: { status: 'ok', reason: '', uncertain: false, next_action: '', people: 1, cells: 2, problems: [] } })],
    warnings: [],
  }
  assert.equal(reuploadHeadline(okResult).impliesComplete, true)
  assert.equal(reuploadHeadline(okResult).tone, 'success')

  const noopResult = { ...okResult, status: 'noop' }
  assert.equal(reuploadHeadline(noopResult).impliesComplete, true)

  const uncertain = { ...okResult, status: 'uncertain', ok: false }
  const view = reuploadHeadline(uncertain)
  assert.equal(view.impliesComplete, false)
  assert.equal(view.tone, 'danger')

  const failed = { ...okResult, status: 'failed', ok: false }
  assert.equal(reuploadHeadline(failed).impliesComplete, false)
})

test('逐表写入状态文案', () => {
  assert.equal(
    reuploadWriteStatusText(table({ write: { status: 'ok', reason: '', uncertain: false, next_action: '', people: 1, cells: 3, problems: [] } })),
    '已写入并回读通过',
  )
  assert.equal(
    reuploadWriteStatusText(table({ write: { status: 'uncertain', reason: '回读失败', uncertain: true, next_action: 'manual_reconcile', people: null, cells: null, problems: [] } })),
    '回读失败',
  )
  assert.equal(reuploadWriteStatusText(table()), '未执行')
})
