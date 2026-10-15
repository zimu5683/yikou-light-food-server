import assert from 'node:assert/strict'
import test from 'node:test'
import {
  REBUILD_AUTO_RUN_ALLOWED,
  REBUILD_CONFIRM_WORD,
  REBUILD_MIN_NOTE,
  SALVAGE_CONFIRM_WORD,
  SALVAGE_MIN_NOTE,
  buildRebuildPayload,
  buildSalvagePayload,
  salvageCountsSummary,
  salvageDroppedText,
  salvageOutcomeView,
  salvageScopeLabel,
  salvageVisibility,
  ledgerStateLabel,
  rebuildCountsSummary,
  rebuildOutcomeView,
  rebuildReasonText,
  rebuildRowText,
  rebuildVisibility,
} from './ledgerRepair.ts'
import type { WpsLedgerRebuildRow } from './bridge.ts'

function row(overrides: Partial<WpsLedgerRebuildRow> = {}): WpsLedgerRebuildRow {
  return {
    name: '张',
    phone: '111',
    slot: 1,
    local_meals: 6,
    cloud_present: true,
    cell_marked: true,
    cell_value: '1',
    decision: 'mark_synced',
    reason: 'cloud_cell_marked',
    anchor: 6,
    order_no: 'W1',
    row: 3,
    ...overrides,
  }
}

test('确认必须逐字输入 rebuild、说明至少 4 字、且必须带指纹', () => {
  const good = buildRebuildPayload({
    confirm: 'rebuild', note: '账本丢失后按云端日期格重建', fingerprint: 'abc123',
  })
  assert.equal(good.ok, true)
  assert.deepEqual(good.payload, {
    confirm: 'rebuild',
    note: '账本丢失后按云端日期格重建',
    expected_fingerprint: 'abc123',
  })

  assert.equal(REBUILD_CONFIRM_WORD, 'rebuild')
  assert.equal(REBUILD_MIN_NOTE, 4)
  assert.equal(buildRebuildPayload({ confirm: 'REBUILD', note: '足够长的说明', fingerprint: 'a' }).ok, false)
  assert.equal(buildRebuildPayload({ confirm: 'yes', note: '足够长的说明', fingerprint: 'a' }).ok, false)
  assert.equal(buildRebuildPayload({ confirm: 'rebuild', note: '短', fingerprint: 'a' }).ok, false)
  assert.equal(buildRebuildPayload({ confirm: 'rebuild', note: '足够长的说明', fingerprint: '' }).ok, false)
})

test('重建永远不允许自动执行', () => {
  assert.equal(REBUILD_AUTO_RUN_ALLOWED, false)
})

test('面板只在该出现的时候出现（普通 missing 不占地方）', () => {
  assert.equal(rebuildVisibility({ ledger_state: 'ok' }), 'hidden')
  assert.equal(rebuildVisibility({ ledger_state: 'missing' }), 'hidden')
  assert.equal(rebuildVisibility({ ledger_state: 'missing_with_history' }), 'advisory')
  assert.equal(rebuildVisibility({ ledger_state: 'corrupt' }), 'blocking')
  assert.equal(rebuildVisibility({ ledger_state: 'unreadable' }), 'blocking')
  assert.equal(rebuildVisibility({ ledger_state: 'ok', ledger_lost: true }), 'blocking')
  assert.equal(rebuildVisibility(null), 'hidden')
})

test('账本状态与判定理由都有中文说法', () => {
  assert.match(ledgerStateLabel('missing'), /不存在/)
  assert.match(ledgerStateLabel('missing_with_history'), /痕迹/)
  assert.match(ledgerStateLabel('ok', true), /丢过锚点/)
  assert.match(ledgerStateLabel(undefined), /未知/)
  assert.match(rebuildReasonText('cloud_person_absent'), /还没/)
  assert.match(rebuildReasonText('cloud_cell_empty'), /是空的/)
  assert.match(rebuildReasonText('cloud_cell_marked'), /已经有值/)
  assert.match(rebuildReasonText('未来新增的理由'), /未知判定/)
})

test('逐行说明写清"会不会再加"', () => {
  assert.match(rebuildRowText(row()), /已入账 6 餐（本批不会再加）/)
  assert.match(
    rebuildRowText(row({ decision: 'leave_empty', reason: 'cloud_cell_empty', anchor: 0 })),
    /下一批会正常加/,
  )
  assert.match(rebuildRowText(row({ slot: 2 })), /第 2 行/)
})

test('统计同时给出两个方向', () => {
  const text = rebuildCountsSummary({
    mark_synced: 3, leave_empty: 12, cloud_person_absent: 4, cloud_cell_marked: 3,
    cloud_cell_empty: 8, rows: 15, people: 3, sheets: 2, skipped_sheets: 1,
  })
  assert.match(text, /已入账 3 行/)
  assert.match(text, /留空 12 行/)
  assert.match(text, /1 张表本次不重建/)
  assert.equal(rebuildCountsSummary(undefined), '统计不可用')
})

test('成功结果：说清没写云端，且下一步是重新预览', () => {
  const view = rebuildOutcomeView({
    ok: true, status: 'rebuilt', code: 'rebuilt', reason: '', next_action: 'repreview',
    changed: true, cloud_write: false, ledger_write: true,
    written_people: 3, written_slots: 3,
    counts: { mark_synced: 3, leave_empty: 0, cloud_person_absent: 0, cloud_cell_marked: 3, cloud_cell_empty: 0, rows: 3, people: 3, sheets: 1, skipped_sheets: 0 },
  })
  assert.equal(view.ok, true)
  assert.equal(view.tone, 'success')
  assert.match(view.detail, /云端一个格子都没动/)
  assert.match(view.nextStep, /预览/)
  assert.equal(view.blockingRetained, true)
  assert.equal(view.ledgerChanged, true)
})

test('无写入也要如实说：noop 与未确认都不得暗示成功', () => {
  const noop = rebuildOutcomeView({
    ok: true, status: 'rebuild_noop', code: 'rebuild_noop', reason: '', next_action: 'repreview', changed: false,
  })
  assert.equal(noop.tone, 'info')
  assert.equal(noop.ledgerChanged, false)

  const unknown = rebuildOutcomeView(null)
  assert.equal(unknown.ok, false)
  assert.match(unknown.detail, /未知/)
  assert.equal(unknown.blockingRetained, true)
})

test('失败与拒绝：各自的下一步不同，绝不提示跳过预览', () => {
  const cases: Array<[string, string, RegExp]> = [
    ['confirmation_required', 'warning', /没有改任何文件/],
    ['note_required', 'warning', /没有改任何文件/],
    ['rebuild_changed', 'danger', /重新「查看重建提案/],
    ['ledger_write_failed', 'danger', /磁盘/],
    ['rebuild_unverified', 'danger', /人工只读核对/],
    ['ledger_unavailable_requires_repair', 'danger', /不要直接重传/],
    ['wps_disabled', 'warning', /开启/],
  ]
  for (const [code, tone, pattern] of cases) {
    const view = rebuildOutcomeView({
      ok: false, status: 'rejected', code, reason: '服务端说明', next_action: 'manual_reconcile',
      changed: false,
    })
    assert.equal(view.ok, false, code)
    assert.equal(view.tone, tone, code)
    assert.match(view.nextStep, pattern, code)
  }
  const unverified = rebuildOutcomeView({
    ok: false, status: 'rebuild_unverified', code: 'rebuild_unverified', reason: '',
    next_action: 'manual_reconcile', changed: true,
  })
  assert.equal(unverified.ledgerChanged, true)
  assert.match(unverified.title, /回读对不上/)
})

test('抢救按钮只在账本损坏时出现', () => {
  assert.equal(salvageVisibility({ ledger_state: 'corrupt' }), 'available')
  assert.equal(salvageVisibility({ ledger_state: 'missing' }), 'hidden')
  assert.equal(salvageVisibility({ ledger_state: 'missing_with_history' }), 'hidden')
  assert.equal(salvageVisibility({ ledger_state: 'ok', ledger_lost: true }), 'hidden')
  assert.equal(salvageVisibility(null), 'hidden')
})

test('抢救清单说清留下多少、丢掉多少', () => {
  const text = salvageCountsSummary({
    kept_people: 12, kept_batches: 3, dropped_people: 2, dropped_batches: 1, dropped_total: 3,
  })
  assert.match(text, /留下 12 人/)
  assert.match(text, /丢掉 3 条/)
  assert.match(text, /涉及 3 张表/)
  assert.equal(salvageCountsSummary(undefined), '统计不可用')
})

test('清单里的一条写清是谁、哪一批、为什么', () => {
  assert.match(
    salvageDroppedText({ scope: 'person', day: '2026-09-11', file: 'F1', name: '王五', phone: '333', reason: '账本 slots 值非法' }),
    /王五（333） · 2026-09-11 \/ F1：账本 slots 值非法/,
  )
  // 整层坏掉时没有姓名，就写清是哪一层
  assert.match(
    salvageDroppedText({ scope: 'batch', day: '2026-09-11', file: 'F2', name: '', phone: '', reason: '账本批次层结构非法' }),
    /整张表 · 2026-09-11 \/ F2：账本批次层结构非法/,
  )
  assert.equal(salvageScopeLabel('day'), '整天')
  assert.equal(salvageScopeLabel('未来层级'), '未知层级')
})

test('抢救必须逐字 salvage 且说明够长', () => {
  const good = buildSalvagePayload({ confirm: 'salvage', note: '账本损坏，按清单抢救' })
  assert.equal(good.ok, true)
  assert.deepEqual(good.payload, { confirm: 'salvage', note: '账本损坏，按清单抢救' })
  assert.equal(SALVAGE_CONFIRM_WORD, 'salvage')
  assert.equal(SALVAGE_MIN_NOTE, 4)
  assert.equal(buildSalvagePayload({ confirm: 'SALVAGE', note: '足够长的说明' }).ok, false)
  assert.equal(buildSalvagePayload({ confirm: 'salvage', note: '短' }).ok, false)
})

test('抢救成功：说清原件留档、没写云端、下一步重新预览', () => {
  const view = salvageOutcomeView({
    ok: true, status: 'salvaged', changed: true, verified_on_disk: true,
    cloud_write: false, ledger_write: true,
    counts: { kept_people: 12, kept_batches: 3, dropped_people: 2, dropped_batches: 1, dropped_total: 3 },
  })
  assert.equal(view.ok, true)
  assert.equal(view.tone, 'success')
  assert.match(view.detail, /原件已留档/)
  assert.match(view.detail, /云端一个格子都没动/)
  assert.match(view.nextStep, /预览/)
  assert.equal(view.blockingRetained, true)
  assert.equal(view.ledgerChanged, true)
})

test('抢救写了但回读对不上：不得当成功', () => {
  const view = salvageOutcomeView({
    ok: true, status: 'salvaged', changed: true, verified_on_disk: false,
  })
  assert.equal(view.ok, false)
  assert.equal(view.tone, 'danger')
  assert.match(view.title, /回读对不上/)
})

test('救不了的情况各自给出下一步', () => {
  const parseFailed = salvageOutcomeView({ ok: false, status: 'parse_failed', reason: '' })
  assert.equal(parseFailed.tone, 'danger')
  assert.match(parseFailed.nextStep, /账本锚点重建/)

  for (const status of ['missing', 'unreadable', 'unsalvageable']) {
    const view = salvageOutcomeView({ ok: false, status, reason: '账本文件不存在，没有可抢救的内容' })
    assert.equal(view.ok, false, status)
    assert.match(view.detail, /账本文件不存在|读不出来/, status)
  }

  const selfCheck = salvageOutcomeView({ ok: false, status: 'self_check_failed', reason: '' })
  assert.match(selfCheck.detail, /一个字节都没写/)

  const conflict = salvageOutcomeView({ ok: false, status: 'operation_conflict', reason: '' })
  assert.equal(conflict.tone, 'warning')
  assert.match(conflict.detail, /覆盖/)
})

test('抢救清单阶段不暗示已经写过东西', () => {
  const ready = salvageOutcomeView({
    ok: true, status: 'salvage_ready', read_only: true, changed: false,
    counts: { kept_people: 12, kept_batches: 3, dropped_people: 2, dropped_batches: 1, dropped_total: 3 },
  })
  assert.equal(ready.ledgerChanged, false)
  assert.match(ready.title, /还没写任何东西/)

  const nothing = salvageOutcomeView({ ok: true, status: 'nothing_to_salvage', changed: false })
  assert.equal(nothing.ledgerChanged, false)
  assert.match(nothing.detail, /一个字节都没写/)

  const unknown = salvageOutcomeView(null)
  assert.equal(unknown.ok, false)
  assert.equal(unknown.blockingRetained, true)
})
