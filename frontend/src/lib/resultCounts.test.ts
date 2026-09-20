import assert from 'node:assert/strict'
import test from 'node:test'
import {
  UNKNOWN_TEXT,
  claimsWrittenRows,
  mayClaimCompletion,
  previewPlanText,
  uploadCountLines,
  uploadCountView,
  uploadHeadline,
  uploadResultStep,
} from './resultCounts.ts'
import type { WpsExecutionSummary, WpsUploadResult } from './bridge.ts'

function execution(overrides: Partial<WpsExecutionSummary> = {}): WpsExecutionSummary {
  return {
    kind: 'execution',
    contract_version: 1,
    status: 'success',
    executed: true,
    counts_source: 'apply_plan',
    sheets: { total: 1, verified: 1, noop: 0, failed: 0, uncertain: 0, skipped: 0, blocked: 0, other: 0 },
    rows: { verified: 1, failed: 0, uncertain: 0, skipped: 0, planned: 1 },
    rows_unknown: false,
    proven_no_write: false,
    written_sheets: 1,
    failed_sheets: 0,
    ...overrides,
  }
}

function result(overrides: Partial<WpsUploadResult> = {}): WpsUploadResult {
  return {
    ok: true,
    status: 'success',
    code: '',
    reason: '',
    next_action: '',
    operation_id: 'op-1',
    planned_summary: { kind: 'plan', rows: { to_update: 1, to_append: 0, unchanged: 0, skipped: 0, warned: 0 } },
    execution_summary: execution(),
    ...overrides,
  }
}

test('W6：计划 1 行、执行已核实 1 行时两套口径分别展示', () => {
  const view = uploadCountView(result())
  assert.deepEqual(view.plan, { update: 1, append: 0, unchanged: 0, skipped: 0, warned: 0 })
  assert.equal(view.verifiedRows, 1)
  assert.equal(view.verifiedKnown, true)
  assert.equal(view.rowsUnknown, false)
  const text = uploadCountLines(result()).map((line) => line.value).join(' | ')
  assert.match(text, /计划更新 1/)
  assert.match(text, /已核实写入 1 行/)
  assert.equal(uploadHeadline(result()).impliesComplete, true)
})

test('F1：ok+success 但实际行数无法核实时，不得声称“上传完成”', () => {
  // 计划 1 行、执行行数未知：服务端 status=success，但 rows.verified=null。
  const unverified = result({
    execution_summary: execution({
      rows: { verified: null, failed: null, uncertain: null, skipped: null, planned: 1 },
      rows_unknown: true,
    }),
  })
  const headline = uploadHeadline(unverified)
  assert.equal(headline.impliesComplete, false)
  assert.notEqual(headline.tone, 'success')
  assert.doesNotMatch(headline.title, /上传完成|已完成/)
  assert.match(headline.title, /无法核实|待核对/)
  assert.equal(mayClaimCompletion(unverified), false)
  assert.equal(uploadResultStep(unverified), 'warning')
  // 与 mayClaimCompletion 自洽：impliesComplete 必须等价于"可声称完成"。
  assert.equal(headline.impliesComplete, mayClaimCompletion(unverified))

  // 缺失 execution_summary 同样不能算完成。
  const missing = result({ execution_summary: null, rows_unknown: true })
  assert.equal(uploadHeadline(missing).impliesComplete, false)
  assert.equal(mayClaimCompletion(missing), false)

  // 对照：行数已核实 → 允许成功。
  assert.equal(uploadHeadline(result()).impliesComplete, true)
  // 对照：服务端证明零写入 → 也是可核实的，允许（由 rejected 分支给出"未写入"文案）。
  const noWrite = result({
    ok: false, status: 'rejected',
    execution_summary: execution({ proven_no_write: true, rows_unknown: false }),
  })
  assert.equal(uploadCountView(noWrite).provenNoWrite, true)
})

test('W6：计划 1 行、实际未核实时不得显示“已更新 1 行”，必须显示未知', () => {
  const payload = result({
    execution_summary: execution({
      rows: { verified: null, failed: null, uncertain: null, skipped: null, planned: 1 },
      rows_unknown: true,
    }),
  })
  const view = uploadCountView(payload)
  assert.equal(view.rowsUnknown, true)
  assert.equal(view.verifiedRows, null)
  const text = uploadCountLines(payload).map((line) => line.value).join(' | ')
  assert.match(text, /实际写入行数未知/)
  assert.match(text, new RegExp(UNKNOWN_TEXT))
  assert.doesNotMatch(text, /已核实写入 1 行/)
  assert.doesNotMatch(text, /已更新 1 行/)
  assert.equal(claimsWrittenRows(payload), null)
})

test('W6：execution_summary 缺失时按未知处理，不用计划数顶替', () => {
  const payload = result({ execution_summary: null, rows_unknown: true })
  const view = uploadCountView(payload)
  assert.equal(view.execution, null)
  assert.equal(view.rowsUnknown, true)
  assert.equal(view.verifiedRows, null)
  const text = uploadCountLines(payload).map((line) => line.value).join(' | ')
  assert.match(text, /实际写入行数未知/)
})

test('W6：execution_summary.rows.verified === null 即使 rows_unknown 缺失也算未知', () => {
  const payload = result({
    execution_summary: execution({
      rows: { verified: null, failed: 0, uncertain: 0, skipped: 0, planned: 1 },
      rows_unknown: false,
    }),
  })
  const view = uploadCountView(payload)
  assert.equal(view.rowsUnknown, true)
  assert.equal(view.verifiedKnown, false)
})

test('W6：uncertain 不显示完成，impliesComplete=false 且禁止“已写入 N 行”', () => {
  const payload = result({
    ok: false,
    status: 'uncertain',
    uncertain: true,
    execution_summary: execution({ status: 'uncertain' }),
  })
  const headline = uploadHeadline(payload)
  assert.equal(headline.impliesComplete, false)
  assert.equal(headline.tone, 'warning')
  assert.match(headline.title, /不确定/)
  assert.equal(mayClaimCompletion(payload), false)
  assert.equal(claimsWrittenRows(payload), null)
})

test('W6：verification_missing / contradictory 也按不确定处理', () => {
  for (const flag of ['verification_missing', 'contradictory'] as const) {
    const payload = result({ ok: true, status: 'success', [flag]: true })
    assert.equal(uploadHeadline(payload).impliesComplete, false, flag)
    assert.equal(mayClaimCompletion(payload), false, flag)
  }
})

test('W6：0 次写入（proven_no_write）显示“未写入任何内容”，不显示已更新', () => {
  const payload = result({
    ok: false,
    status: 'rejected',
    code: 'wps_disabled',
    execution_summary: execution({
      status: 'rejected',
      executed: false,
      counts_source: 'rejected_before_write',
      sheets: { total: 1, verified: 0, noop: 0, failed: 0, uncertain: 0, skipped: 0, blocked: 0, other: 0 },
      rows: { verified: 0, failed: 0, uncertain: 0, skipped: 0, planned: 1 },
      proven_no_write: true,
      written_sheets: 0,
    }),
  })
  const text = uploadCountLines(payload).map((line) => line.value).join(' | ')
  assert.match(text, /本次未写入任何内容/)
  assert.doesNotMatch(text, /已核实写入/)
  assert.equal(mayClaimCompletion(payload), false)
})

test('W6：noop 明确“无需写入”，不得写成成功更新 N 行', () => {
  const payload = result({
    status: 'noop',
    execution_summary: execution({
      status: 'noop',
      sheets: { total: 1, verified: 0, noop: 1, failed: 0, uncertain: 0, skipped: 0, blocked: 0, other: 0 },
      rows: { verified: 0, failed: 0, uncertain: 0, skipped: 0, planned: 0 },
    }),
  })
  const headline = uploadHeadline(payload)
  assert.equal(headline.impliesComplete, false)
  assert.match(headline.title, /无需写入/)
})

test('W6：partial / failed / blocked / recovered 都不允许完成措辞', () => {
  for (const status of ['partial', 'failed', 'error', 'blocked', 'recovered', 'not_started']) {
    const payload = result({ ok: false, status })
    const headline = uploadHeadline(payload)
    assert.equal(headline.impliesComplete, false, status)
    assert.equal(mayClaimCompletion(payload), false, status)
  }
})

test('W6：status=success 但 ok 不为 true 仍按未确认处理', () => {
  const payload = result({ ok: false, status: 'success' })
  const headline = uploadHeadline(payload)
  assert.equal(headline.impliesComplete, false)
  assert.match(headline.title, /未确认/)
})

test('W6：表数是表数，不得当行数用', () => {
  const payload = result({
    execution_summary: execution({
      sheets: { total: 3, verified: 2, noop: 1, failed: 0, uncertain: 0, skipped: 0, blocked: 0, other: 0 },
      rows: { verified: 7, failed: 0, uncertain: 0, skipped: 0, planned: 7 },
    }),
  })
  const text = uploadCountLines(payload).map((line) => line.value).join(' | ')
  assert.match(text, /已核实表 2/)
  assert.match(text, /已核实写入 7 行/)
  assert.doesNotMatch(text, /已核实写入 2 行/)
})

test('W6：表数里的 unknown 维度显示 待核对/未知，不显示 0', () => {
  const payload = result({
    execution_summary: execution({
      sheets: { total: 1, verified: 1, noop: 0, failed: 0, uncertain: 0, skipped: 0, blocked: 0, other: 0 },
      rows: { verified: null, failed: null, uncertain: null, skipped: null, planned: 1 },
      rows_unknown: true,
    }),
  })
  const view = uploadCountView(payload)
  assert.equal(view.execution?.rowsFailed, null)
  assert.equal(view.execution?.rowsUncertain, null)
})

test('W6：预览统计一律标注计划', () => {
  const preview = {
    ok: true,
    summary: { to_update: 2, to_append: 1, unchanged: 3, warned: 0, skipped: 1 },
    planned_summary: { kind: 'plan' as const, rows: { to_update: 2, to_append: 1, unchanged: 3, skipped: 1, warned: 0 } },
    execution_summary: execution({ proven_no_write: true, executed: false }),
    blocked: [],
  }
  const text = previewPlanText(preview as never)
  assert.match(text, /计划更新 2/)
  assert.match(text, /计划新增 1/)
  assert.doesNotMatch(text, /已更新/)
})

test('W6：计划口径缺失时预览显示 待核对/未知，不默认为 0', () => {
  const text = previewPlanText({ ok: true, summary: undefined, planned_summary: undefined, blocked: [] } as never)
  assert.match(text, new RegExp(UNKNOWN_TEXT))
})

test('W6 流程步骤：只有确定成功/无需写入才是 done，其余是 warning', () => {
  assert.equal(uploadResultStep(null), 'todo')
  assert.equal(uploadResultStep(result()), 'done')
  assert.equal(uploadResultStep(result({ status: 'noop' })), 'done')
  // ok=true 但状态不确定：绝不能是 done。
  assert.equal(uploadResultStep(result({ ok: true, status: 'uncertain', uncertain: true })), 'warning')
  assert.equal(uploadResultStep(result({ ok: true, status: 'success', verification_missing: true })), 'warning')
  assert.equal(uploadResultStep(result({ ok: false, status: 'uncertain', uncertain: true })), 'warning')
  assert.equal(uploadResultStep(result({ ok: false, status: 'partial' })), 'warning')
  assert.equal(uploadResultStep(result({ ok: true, status: 'rejected' })), 'warning')
  // ok 不是 true 时，即使 status=success 也不是 done。
  assert.equal(uploadResultStep(result({ ok: false, status: 'success' })), 'warning')
})
