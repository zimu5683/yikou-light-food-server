/**
 * W6：**计划口径** vs **执行口径** 的渲染层分流。
 *
 * 后端从 R6 起在同一次 `wps_upload()` 返回里给出两套完全不同的行数：
 *
 * | 字段 | kind | 含义 |
 * | --- | --- | --- |
 * | `planned_summary` / `summary_stats` | `plan` | **计划**要改几行 |
 * | `execution_summary` / `summary` / `stats` | `execution` | **实际**结果（表数 + 行数） |
 *
 * 铁律（`docs/BRIDGE-WPS-CONTRACT-R6.md` §2）：
 *
 * 1. 计划行数只能读 `planned_summary.rows.*`，并必须标注「计划」；
 * 2. 成功行数只能读 `execution_summary.rows.verified`；
 * 3. `rows.verified === null` 或 `rows_unknown === true` → 显示「实际写入行数未知，
 *    请只读核对云端」，**不得**显示 0、也不得退回计划数；
 * 4. `sheets.*` 是**表数**，`rows.*` 才是行数；
 * 5. `uncertain` / 部分失败 / 未证实零写入时，不得出现任何暗示「已完成」的文案或数字；
 * 6. 只有 `proven_no_write === true` 才允许说「本次未写入任何内容」。
 *
 * 纯逻辑模块，Node 测试可直接覆盖（`resultCounts.test.ts`）。
 */
import type {
  WpsExecutionSummary,
  WpsPlannedSummary,
  WpsPreviewResult,
  WpsUploadResult,
} from './bridge.ts'

/** 无法核实的数量的统一占位符。 */
export const UNKNOWN_TEXT = '待核对/未知'

export interface PlanRowsView {
  update: number
  append: number
  unchanged: number
  skipped: number
  warned: number
}

export interface ExecutionView {
  rowsVerified: number | null
  rowsFailed: number | null
  rowsUncertain: number | null
  rowsSkipped: number | null
  rowsPlanned: number | null
  sheetsTotal: number | null
  sheetsVerified: number | null
  sheetsFailed: number | null
  sheetsUncertain: number | null
  sheetsOther: number | null
}

export interface UploadCountView {
  /**
   * 计划口径。**没有** `planned_summary` 时为 `null`，不要用旧 `summary` 冒充：
   * 旧字段名在 R6 后已经是执行口径。
   */
  plan: PlanRowsView | null
  /** 执行口径。没有 `execution_summary` 时为 `null`（未知，而不是 0）。 */
  execution: ExecutionView | null
  /** 实际写入行数无法证明。 */
  rowsUnknown: boolean
  /** 已被证明本次零云端写入。 */
  provenNoWrite: boolean
  /** `apply_plan` / `rejected_before_write` / `unknown_after_exception`；只是计数来源。 */
  countsSource: string
  /** 已核实的写入行数；`null` = 无法确认。 */
  verifiedRows: number | null
  /** 是否存在可证明的“确定成功”行数。 */
  verifiedKnown: boolean
}

export type UploadTone = 'success' | 'warning' | 'danger' | 'neutral'

export interface UploadHeadline {
  tone: UploadTone
  title: string
  /** 是否允许出现“已完成/成功/完成”这类措辞。 */
  impliesComplete: boolean
  /** 给人看的下一步。 */
  nextStep: string
}

function intOrNull(value: unknown): number | null {
  if (value === null || value === undefined) return null
  const num = Number(value)
  return Number.isFinite(num) ? num : null
}

function planRowsOf(plan: WpsPlannedSummary | null | undefined): PlanRowsView | null {
  const rows = plan?.rows
  if (!rows || typeof rows !== 'object') return null
  return {
    update: Number(rows.to_update ?? 0),
    append: Number(rows.to_append ?? 0),
    unchanged: Number(rows.unchanged ?? 0),
    skipped: Number(rows.skipped ?? 0),
    warned: Number(rows.warned ?? 0),
  }
}

function executionOf(summary: WpsExecutionSummary | null | undefined): ExecutionView | null {
  if (!summary || typeof summary !== 'object') return null
  const sheets = summary.sheets ?? ({} as WpsExecutionSummary['sheets'])
  const rows = summary.rows ?? ({} as WpsExecutionSummary['rows'])
  return {
    rowsVerified: intOrNull(rows.verified),
    rowsFailed: intOrNull(rows.failed),
    rowsUncertain: intOrNull(rows.uncertain),
    rowsSkipped: intOrNull(rows.skipped),
    rowsPlanned: intOrNull(rows.planned),
    sheetsTotal: intOrNull(sheets.total),
    sheetsVerified: intOrNull(sheets.verified),
    sheetsFailed: intOrNull(sheets.failed),
    sheetsUncertain: intOrNull(sheets.uncertain),
    sheetsOther: intOrNull(sheets.other),
  }
}

/**
 * 把 `wps_upload()` 结果解析成渲染用的两套口径。
 *
 * 兼容旧后端（没有 `planned_summary/execution_summary`）时：旧 `summary` 在 R6 后
 * 语义是执行口径，因此只在它确实带 `kind:"execution"` 或含 `rows` 结构时采信；
 * 否则一律按「未知」，绝不把旧字段当计划数展示成功行数。
 */
export function uploadCountView(result: WpsUploadResult | null | undefined): UploadCountView {
  if (!result) {
    return {
      plan: null, execution: null, rowsUnknown: true, provenNoWrite: false,
      countsSource: '', verifiedRows: null, verifiedKnown: false,
    }
  }
  const plan = planRowsOf(result.planned_summary ?? result.summary_stats ?? null)
  const executionRaw = result.execution_summary
    ?? (result.summary && (result.summary as { kind?: string }).kind === 'execution'
      ? (result.summary as unknown as WpsExecutionSummary)
      : null)
  const execution = executionOf(executionRaw)
  const rowsUnknown = result.rows_unknown === true
    || executionRaw?.rows_unknown === true
    || (execution !== null && execution.rowsVerified === null)
    || execution === null
  const provenNoWrite = executionRaw?.proven_no_write === true
  const verifiedRows = execution?.rowsVerified ?? null
  return {
    plan,
    execution,
    rowsUnknown,
    provenNoWrite,
    countsSource: String(executionRaw?.counts_source || ''),
    verifiedRows,
    verifiedKnown: !rowsUnknown && verifiedRows !== null,
  }
}

function isUncertain(result: WpsUploadResult): boolean {
  const status = String(result.status || '').toLowerCase()
  return result.uncertain === true
    || result.verification_missing === true
    || result.contradictory === true
    || status === 'uncertain'
}

/**
 * 结果标题/语气。**只有** `ok===true && status==='success'` 才允许「已完成」。
 *
 * `noop` 也是 `ok=true`，但它没有写入任何行，不能说成“成功更新 N 行”。
 */
export function uploadHeadline(result: WpsUploadResult | null | undefined): UploadHeadline {
  if (!result) {
    return { tone: 'neutral', title: '尚无上传结果', impliesComplete: false, nextStep: '' }
  }
  const status = String(result.status || '').toLowerCase()
  const counts = uploadCountView(result)
  const unknownHint = counts.rowsUnknown
    ? '实际写入行数未知，请只读核对云端。'
    : ''

  if (isUncertain(result)) {
    return {
      tone: 'warning',
      title: '上传结果不确定 · 待核对',
      impliesComplete: false,
      nextStep: result.next_action || '只读核对云端与日志，不要重新上传。',
    }
  }
  switch (status) {
    case 'noop':
      return {
        tone: 'neutral',
        title: '本次无需写入（服务端确认无变化）',
        impliesComplete: false,
        nextStep: result.next_action || '无需写入；如需变更请修改本地表后重新预览。',
      }
    case 'partial':
      return {
        tone: 'warning',
        title: '部分表未确认 · 待核对',
        impliesComplete: false,
        nextStep: result.next_action || '核对失败表与日志，不要整批重传。',
      }
    case 'blocked':
      return {
        tone: 'danger',
        title: '上传被阻断 · 待核对',
        impliesComplete: false,
        nextStep: result.next_action || '先修复本地日志/账本或走管理员恢复入口，不要重传。',
      }
    case 'failed':
    case 'error':
      return {
        tone: 'danger',
        title: '上传失败',
        impliesComplete: false,
        nextStep: result.next_action || '查看日志定位原因后重新预览。',
      }
    case 'recovered':
    case 'not_started':
      return {
        tone: 'warning',
        title: '本轮计划未执行 · 旧令牌已不可用',
        impliesComplete: false,
        nextStep: result.next_action || '重新预览；不要复用旧令牌。',
      }
    case 'rejected':
      return {
        tone: 'warning',
        title: '上传被服务端拒绝（未写入）',
        impliesComplete: false,
        nextStep: result.next_action || '按提示处理后重新预览。',
      }
    case 'success':
      if (result.ok === true) {
        // 关键：服务端 status=success 时，如果**实际写入行数无法核实**（rows.verified=null /
        // rows_unknown=true / 缺 execution_summary），就不能声称"上传完成"——那样的卡片会同时
        // 说"上传完成"和"实际写入行数未知"，等于把无法证明的行数当成功展示。
        // 与 mayClaimCompletion() 保持一致：只有已核实或已证明零写入才算完成。
        if (counts.rowsUnknown && !counts.provenNoWrite) {
          return {
            tone: 'warning',
            title: '服务端报告成功，但实际写入行数无法核实 · 待核对',
            impliesComplete: false,
            nextStep: result.next_action
              || '只读核对云端确认实际写入范围后再决定下一步；不要据此重传。',
          }
        }
        return {
          tone: 'success',
          title: '上传完成',
          impliesComplete: true,
          nextStep: result.next_action || (counts.rowsUnknown ? unknownHint : ''),
        }
      }
      return {
        tone: 'warning',
        title: '上传结果未确认 · 待核对',
        impliesComplete: false,
        nextStep: result.next_action || '未收到明确成功标记，请只读核对云端与日志。',
      }
    default:
      return {
        tone: 'warning',
        title: '上传结果未确认 · 待核对',
        impliesComplete: false,
        nextStep: result.next_action || '未收到明确成功标记，请只读核对云端与日志；不要重复提交。',
      }
  }
}

export interface CountLine {
  label: string
  value: string
  tone: 'neutral' | 'success' | 'warning' | 'danger'
}

/**
 * 结果卡片的两套口径文本。
 *
 * 返回值保证：
 * - 计划行数一律带「计划」前缀；
 * - 无法核实时用 {@link UNKNOWN_TEXT}，不出现 0 或计划数冒充实际；
 * - `impliesComplete=false` 时不会出现「已更新 N 行」这类措辞。
 */
export function uploadCountLines(result: WpsUploadResult | null | undefined): CountLine[] {
  const counts = uploadCountView(result)
  const lines: CountLine[] = []
  if (counts.plan) {
    lines.push({
      label: '计划变更行（未执行前只是计划）',
      value: `计划更新 ${counts.plan.update} · 计划新增 ${counts.plan.append} · 计划跳过 ${counts.plan.skipped} · 计划不变 ${counts.plan.unchanged}`,
      tone: 'neutral',
    })
  } else {
    lines.push({ label: '计划变更行', value: UNKNOWN_TEXT, tone: 'neutral' })
  }

  if (counts.provenNoWrite) {
    lines.push({ label: '实际写入', value: '本次未写入任何内容（已证明零云端写入）', tone: 'neutral' })
  } else if (counts.verifiedKnown) {
    lines.push({ label: '实际写入', value: `已核实写入 ${counts.verifiedRows} 行`, tone: 'success' })
  } else {
    lines.push({ label: '实际写入', value: `实际写入行数未知，请只读核对云端（${UNKNOWN_TEXT}）`, tone: 'warning' })
  }

  if (counts.execution) {
    const { sheetsVerified, sheetsFailed, sheetsUncertain, sheetsTotal, sheetsOther } = counts.execution
    const sheetParts = [
      sheetsVerified === null ? `已核实表 ${UNKNOWN_TEXT}` : `已核实表 ${sheetsVerified}`,
      sheetsFailed === null ? `失败表 ${UNKNOWN_TEXT}` : `失败表 ${sheetsFailed}`,
      sheetsUncertain === null ? `不确定表 ${UNKNOWN_TEXT}` : `不确定表 ${sheetsUncertain}`,
      sheetsTotal === null ? `总表 ${UNKNOWN_TEXT}` : `总表 ${sheetsTotal}`,
    ]
    if (sheetsOther !== null && sheetsOther > 0) sheetParts.push(`未知状态表 ${sheetsOther}`)
    lines.push({
      label: '表数（不是行数）',
      value: sheetParts.join(' · '),
      tone: (sheetsFailed ?? 0) > 0 || (sheetsUncertain ?? 0) > 0 ? 'warning' : 'neutral',
    })
  }

  const rowsFailed = counts.execution?.rowsFailed
  const rowsUncertain = counts.execution?.rowsUncertain
  if ((rowsFailed ?? 0) > 0 || (rowsUncertain ?? 0) > 0) {
    lines.push({
      label: '失败/不确定行',
      value: `失败 ${rowsFailed ?? UNKNOWN_TEXT} · 不确定 ${rowsUncertain ?? UNKNOWN_TEXT}`,
      tone: 'warning',
    })
  }
  return lines
}

/**
 * 是否存在「可能被误读成已完成」的展示风险。
 *
 * 用于浏览器断言与自检：只要结果是 uncertain/partial/blocked/failed，或实际行数未知，
 * 就**必须**返回 `false`。
 */
export function mayClaimCompletion(result: WpsUploadResult | null | undefined): boolean {
  if (!result) return false
  const headline = uploadHeadline(result)
  const counts = uploadCountView(result)
  if (!headline.impliesComplete) return false
  // 即便 status=success，只要实际行数无法核实，也不能声称“已更新 N 行”。
  return counts.verifiedKnown || counts.provenNoWrite
}

/**
 * FlowStrip「结果」步骤的状态。
 *
 * 只有**确定成功**（`ok && status==='success'` 且没有不确定标记）或服务端确认
 * **无需写入**（`ok && status==='noop'`）才算 `done`；其余一律 `warning`，
 * 避免用 `result.ok` 直接把 uncertain/partial 标成绿色完成。
 */
export function uploadResultStep(
  result: WpsUploadResult | null | undefined,
): 'todo' | 'done' | 'warning' {
  if (!result) return 'todo'
  const headline = uploadHeadline(result)
  if (headline.impliesComplete && headline.tone === 'success') return 'done'
  if (result.ok === true && String(result.status || '').toLowerCase() === 'noop') return 'done'
  return 'warning'
}

/** 结果里是否出现了暗示“已写入 N 行”的数字（计划数不算）。 */
export function claimsWrittenRows(result: WpsUploadResult | null | undefined): number | null {
  const counts = uploadCountView(result)
  const headline = uploadHeadline(result)
  if (!headline.impliesComplete) return null
  return counts.verifiedRows
}

// ---------- 预览侧：预览只有计划口径 ----------

export interface PreviewPlanView {
  /** 预览阶段永远是计划：字段名与文案都必须标注「计划」。 */
  plan: PlanRowsView | null
  /** 预览阶段执行口径必然 `proven_no_write=true`（还没写任何东西）。 */
  provenNoWrite: boolean
  blockedSheets: number
}

/** 预览统计：只作为**计划**展示，绝不写成“已更新 N 行”。 */
export function previewPlanView(preview: WpsPreviewResult | null | undefined): PreviewPlanView {
  if (!preview || !preview.ok) return { plan: null, provenNoWrite: false, blockedSheets: 0 }
  const plan = planRowsOf(preview.planned_summary ?? null)
    ?? (preview.summary
      ? {
        update: Number(preview.summary.to_update ?? 0),
        append: Number(preview.summary.to_append ?? 0),
        unchanged: Number(preview.summary.unchanged ?? 0),
        skipped: Number(preview.summary.skipped ?? 0),
        warned: Number(preview.summary.warned ?? 0),
      }
      : null)
  return {
    plan,
    provenNoWrite: preview.execution_summary?.proven_no_write === true,
    blockedSheets: Array.isArray(preview.blocked) ? preview.blocked.length : 0,
  }
}

/** 预览统计的唯一合法文案：必须带「计划」。 */
export function previewPlanText(preview: WpsPreviewResult | null | undefined): string {
  const view = previewPlanView(preview)
  if (!view.plan) return `计划统计 ${UNKNOWN_TEXT}（预览未返回结构化计划）`
  const { update, append, skipped, unchanged, warned } = view.plan
  return `计划更新 ${update} · 计划新增 ${append} · 计划跳过 ${skipped} · 计划不变 ${unchanged} · 计划警告 ${warned}`
}
