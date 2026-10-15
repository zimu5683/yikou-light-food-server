/**
 * 第 14 项：账本锚点重建（`wps_ledger_rebuild`）的纯逻辑层。
 *
 * 契约见 `docs/BRIDGE-WPS-CONTRACT-R6.md` §12。前端必须如实呈现：
 *
 * - 这个入口**只写本地账本**，永不写云端（`cloud_write=false`），也不解除任何
 *   防重复闸门（闸门在意图日志里，是另一条防线）；
 * - 方向永远是**宁漏勿重**：判成"已入账"的行下一批不会再加；判错只可能漏加，
 *   而漏加能人工补、重复加要按云端日期格往回减；
 * - 只读提案不带 `confirm`；确认必须**逐字**输入 `rebuild` 并填核对说明；
 * - `missing`（账本不存在）与全新安装**无法区分**，所以既不能说"没事"，也不能
 *   说"一定丢过"。
 *
 * 纯逻辑模块，Node 测试直接覆盖（`ledgerRepair.test.ts`）。
 */
import type {
  LedgerState,
  WpsLedgerSalvageCounts,
  WpsLedgerSalvageDroppedItem,
  WpsLedgerSalvagePayload,
  WpsLedgerSalvageResult,
  WpsLedgerRebuildCounts,
  WpsLedgerRebuildPayload,
  WpsLedgerRebuildResult,
  WpsLedgerRebuildRow,
} from './bridge.ts'

/** 与后端 `REBUILD_CONFIRM_WORD` 逐字一致；改后端必须同步改这里。 */
export const REBUILD_CONFIRM_WORD = 'rebuild'
/** 与后端 `REBUILD_MIN_NOTE` 一致：人工核对说明的最短长度。 */
export const REBUILD_MIN_NOTE = 4
/** 重建面板在任何情况下都不允许自动执行（必须人工确认）。 */
export const REBUILD_AUTO_RUN_ALLOWED = false

/** 确认弹窗里必须原样展示的风险说明。 */
export const REBUILD_RISK_TEXT = '重建只写本地账本，不写云端：判成「已入账」的行，下一批上传时不会再加。判错只会漏加（可以人工补），不会重复加 —— 这就是宁漏勿重。它也不会解除防重复闸门。'

/** 账本可用性 → 用户能看懂的一句话。 */
export const LEDGER_STATE_TEXT: Record<string, string> = {
  ok: '本地账本可用',
  missing: '本地账本不存在（可能只是第一次用；也可能被清过应用数据）',
  missing_with_history: '本地账本不存在，但本机留着以前同步过的痕迹：很可能账本丢过',
  corrupt: '本地账本损坏（原件已尽力留档，可先重建）',
  unreadable: '本地账本读不出来（先检查磁盘/权限，再重建）',
}

export function ledgerStateLabel(state: LedgerState | undefined, lost = false): string {
  if (lost) return '本地账本丢过锚点：意图日志记着已同步，账本里查不到'
  return LEDGER_STATE_TEXT[String(state || '')] || '本地账本可用性未知'
}

/**
 * 重建面板该不该出现：`blocking` 需要用户处理，`advisory` 只提示，`hidden` 不占地方。
 *
 * 普通 `missing`（没有任何历史痕迹）**不弹面板** —— 那多半就是第一次用，
 * 预览里已经有一句"可能是首次同步"的提示，这里再吓一次就成了噪音。
 */
export type RebuildVisibility = 'blocking' | 'advisory' | 'hidden'

export function rebuildVisibility(status: {
  ledger_state?: LedgerState
  ledger_lost?: boolean
} | null | undefined): RebuildVisibility {
  if (!status) return 'hidden'
  if (status.ledger_lost === true) return 'blocking'
  const state = String(status.ledger_state || '')
  if (state === 'corrupt' || state === 'unreadable') return 'blocking'
  if (state === 'missing_with_history') return 'advisory'
  return 'hidden'
}

/** 判定理由 → 中文（与后端 `REASON_*` 机器码一一对应）。 */
export const REBUILD_REASON_TEXT: Record<string, string> = {
  cloud_person_absent: '云端还没有这个人 → 留空（下一批会正常新增）',
  cloud_cell_marked: '云端这一格已经有值 → 记「已入账」（本批不会再加）',
  cloud_cell_empty: '云端这一格是空的 → 留空（下一批会正常加）',
}

export function rebuildReasonText(reason: string): string {
  return REBUILD_REASON_TEXT[String(reason || '')] || `未知判定（${String(reason || '')}）`
}

export function rebuildDecisionLabel(row: Pick<WpsLedgerRebuildRow, 'decision'>): string {
  return row.decision === 'mark_synced' ? '记「已入账」' : '留空'
}

/** 一行人的可读说明（弹窗逐行核对用）。 */
export function rebuildRowText(row: WpsLedgerRebuildRow): string {
  const slot = Number(row.slot || 1) > 1 ? `第 ${row.slot} 行 ` : ''
  const where = row.decision === 'mark_synced'
    ? `已入账 ${row.anchor} 餐（本批不会再加）`
    : '下一批会正常加'
  return `${row.name}（${row.phone}）${slot}：${rebuildReasonText(row.reason)}，${where}`
}

/** 计数 → 「已入账 N 行 · 留空 M 行」。 */
export function rebuildCountsSummary(counts: WpsLedgerRebuildCounts | undefined): string {
  if (!counts) return '统计不可用'
  const marked = Number(counts.mark_synced || 0)
  const empty = Number(counts.leave_empty || 0)
  const skipped = Number(counts.skipped_sheets || 0)
  const parts = [`已入账 ${marked} 行`, `留空 ${empty} 行`]
  if (skipped > 0) parts.push(`另有 ${skipped} 张表本次不重建`)
  return parts.join(' · ')
}

export interface RebuildPayloadBuild {
  ok: boolean
  payload?: WpsLedgerRebuildPayload
  message: string
}

/** 组装确认请求；任何一条不满足都不发请求（宁可不做，也不做错）。 */
export function buildRebuildPayload(input: {
  confirm: string
  note: string
  fingerprint: string
}): RebuildPayloadBuild {
  const confirm = String(input.confirm || '').trim()
  const note = String(input.note || '').trim()
  const fingerprint = String(input.fingerprint || '').trim()
  if (confirm !== REBUILD_CONFIRM_WORD) {
    return { ok: false, message: `必须逐字输入 ${REBUILD_CONFIRM_WORD} 才能写入账本` }
  }
  if (note.length < REBUILD_MIN_NOTE) {
    return { ok: false, message: `请填写至少 ${REBUILD_MIN_NOTE} 个字符的人工核对说明（会写进本地账本审计）` }
  }
  if (!fingerprint) {
    return { ok: false, message: '缺少提案指纹：请先「查看重建提案（只读）」，再确认写入' }
  }
  return {
    ok: true,
    payload: { confirm, note, expected_fingerprint: fingerprint },
    message: '',
  }
}

export interface RebuildOutcomeView {
  ok: boolean
  tone: 'success' | 'warning' | 'danger' | 'info'
  title: string
  detail: string
  nextStep: string
  /** 阻断是否仍然保留（重建**不**解除防重复闸门）。 */
  blockingRetained: boolean
  /** 本次是否真的改了本地账本。 */
  ledgerChanged: boolean
}

/** 一次重建的结果 → 展示视图。绝不把失败说成成功，也不暗示可以跳过预览。 */
export function rebuildOutcomeView(
  result: WpsLedgerRebuildResult | null | undefined,
): RebuildOutcomeView {
  if (!result) {
    return {
      ok: false, tone: 'warning', title: '重建结果未知',
      detail: '没有拿到服务端结果，本地账本是否已改未知。',
      nextStep: '先重新只读刷新状态，确认后再决定；不要重复提交。',
      blockingRetained: true, ledgerChanged: false,
    }
  }
  const code = String(result.code || '')
  const status = String(result.status || '')
  if (status === 'rebuilt' && result.ok === true) {
    return {
      ok: true, tone: 'success', title: '账本锚点已重建',
      detail: `已写入 ${Number(result.written_people || 0)} 人 / ${Number(result.written_slots || 0)} 个槽位，云端一个格子都没动。`,
      nextStep: '重新点「预览」，逐行核对确认无误后再上传。',
      blockingRetained: true, ledgerChanged: result.changed === true,
    }
  }
  if (status === 'rebuild_noop') {
    return {
      ok: true, tone: 'info', title: '没有需要重建的行',
      detail: '本批没有任何一行判定为「已入账」，因此没有写账本。',
      nextStep: '重新预览；如果这些行本来就该新加，直接上传即可。',
      blockingRetained: true, ledgerChanged: false,
    }
  }
  switch (code) {
    case 'confirmation_required':
    case 'note_required':
      return {
        ok: false, tone: 'warning', title: '确认不完整，未写账本',
        detail: '必须逐字输入确认词，并填写至少 4 个字符的人工核对说明。',
        nextStep: '补全后重新提交；本次没有改任何文件。',
        blockingRetained: true, ledgerChanged: false,
      }
    case 'rebuild_changed':
      return {
        ok: false, tone: 'danger', title: '提案已过期，未写账本',
        detail: '云端或本地排单表在查看提案之后发生了变化，重建出来的锚点会不一样。',
        nextStep: '重新「查看重建提案（只读）」，核对新的判定后再确认。',
        blockingRetained: true, ledgerChanged: false,
      }
    case 'ledger_write_failed':
      return {
        ok: false, tone: 'danger', title: '账本写入失败',
        detail: '本地账本没能写成（原件已尽力留档）；锚点仍是旧状态。',
        nextStep: '先修复磁盘/权限，再重新提交；确认前不要上传。',
        blockingRetained: true, ledgerChanged: false,
      }
    case 'rebuild_unverified':
      return {
        ok: false, tone: 'danger', title: '账本写入后回读对不上',
        detail: '服务端写完后重新读盘，没能证明锚点就位。',
        nextStep: '人工只读核对本地账本与云端后再决定，不要直接上传。',
        blockingRetained: true, ledgerChanged: result.changed === true,
      }
    case 'ledger_unavailable_requires_repair':
      return {
        ok: false, tone: 'danger', title: '本地账本不可用',
        detail: String(result.reason || '本地账本/意图日志不可用。'),
        nextStep: '先按提示修复本地账本（可走「账本锚点重建」），不要直接重传。',
        blockingRetained: true, ledgerChanged: false,
      }
    case 'wps_disabled':
      return {
        ok: false, tone: 'warning', title: '云文档同步已关闭',
        detail: '开关关闭时服务端拒绝重建，未读云端、未写任何内容。',
        nextStep: '在「云文档同步」中开启后再试。',
        blockingRetained: false, ledgerChanged: false,
      }
    default:
      break
  }
  if (result.ok === true) {
    return {
      ok: true, tone: 'info', title: '重建未发生写入',
      detail: String(result.reason || '服务端没有改动本地账本。'),
      nextStep: '重新预览后再决定是否上传。',
      blockingRetained: true, ledgerChanged: result.changed === true,
    }
  }
  return {
    ok: false, tone: 'danger', title: '重建未完成',
    detail: String(result.reason || '服务端拒绝了本次重建，未写云端。'),
    nextStep: '按提示处理后重试；处理前不要直接上传。',
    blockingRetained: true, ledgerChanged: result.changed === true,
  }
}

// ----------------------------------------------------------------------
// 账本抢救（第 14 项续）：把坏的那几条丢掉，好的锚点原样留下
// ----------------------------------------------------------------------

/** 与后端 `SALVAGE_CONFIRM_WORD` 逐字一致；改后端必须同步改这里。 */
export const SALVAGE_CONFIRM_WORD = 'salvage'
/** 与后端 `SALVAGE_MIN_NOTE` 一致。 */
export const SALVAGE_MIN_NOTE = 4

/** 抢救弹窗里必须原样展示的风险说明。 */
export const SALVAGE_RISK_TEXT = '抢救只丢掉坏掉的那几条，好的锚点原样保留；丢掉的那些人就没有锚点了，接下来只能靠「账本锚点重建」按云端现状补。它只写本地账本，不写云端，也不碰意图日志。'

/**
 * 该不该给「先试着抢救账本」这个按钮：只有账本**损坏**时才给。
 *
 * `missing` / `missing_with_history`（文件不在）和 `ledger_lost`（文件能读、
 * 只是锚点没了）都不给 —— 那两种情况抢救无从下手，按钮只会误导。
 */
export function salvageVisibility(status: {
  ledger_state?: LedgerState
} | null | undefined): 'available' | 'hidden' {
  return String(status?.ledger_state || '') === 'corrupt' ? 'available' : 'hidden'
}

export function salvageCountsSummary(counts: WpsLedgerSalvageCounts | undefined): string {
  if (!counts) return '统计不可用'
  const parts = [`留下 ${Number(counts.kept_people || 0)} 人`,
                 `丢掉 ${Number(counts.dropped_total || 0)} 条`]
  if (Number(counts.kept_batches || 0) > 0) parts.push(`涉及 ${Number(counts.kept_batches || 0)} 张表`)
  return parts.join(' · ')
}

export function salvageScopeLabel(scope: string): string {
  return ({ person: '某个人', batch: '整张表', day: '整天' } as Record<string, string>)[String(scope || '')]
    || '未知层级'
}

/** 清单里的一条 → 人话。坏在整层时没有姓名，就写清是哪一天、哪张表。 */
export function salvageDroppedText(item: WpsLedgerSalvageDroppedItem): string {
  const where = [item.day, item.file].filter(Boolean).join(' / ')
  const who = item.name ? `${item.name}${item.phone ? `（${item.phone}）` : ''}` : salvageScopeLabel(item.scope)
  return `${who}${where ? ` · ${where}` : ''}：${item.reason}`
}

export interface SalvagePayloadBuild {
  ok: boolean
  payload?: WpsLedgerSalvagePayload
  message: string
}

export function buildSalvagePayload(input: { confirm: string; note: string }): SalvagePayloadBuild {
  const confirm = String(input.confirm || '').trim()
  const note = String(input.note || '').trim()
  if (confirm !== SALVAGE_CONFIRM_WORD) {
    return { ok: false, message: `必须逐字输入 ${SALVAGE_CONFIRM_WORD} 才能抢救账本` }
  }
  if (note.length < SALVAGE_MIN_NOTE) {
    return { ok: false, message: `请填写至少 ${SALVAGE_MIN_NOTE} 个字符的人工核对说明（会写进本地账本审计）` }
  }
  return { ok: true, payload: { confirm, note }, message: '' }
}

/** 一次抢救的结果 → 展示视图（与重建共用同一套视图字段）。 */
export function salvageOutcomeView(
  result: WpsLedgerSalvageResult | null | undefined,
): RebuildOutcomeView {
  if (!result) {
    return {
      ok: false, tone: 'warning', title: '抢救结果未知',
      detail: '没有拿到服务端结果，本地账本是否已改未知。',
      nextStep: '先重新只读刷新状态，确认后再决定；不要重复提交。',
      blockingRetained: true, ledgerChanged: false,
    }
  }
  const status = String(result.status || '')
  const counts = salvageCountsSummary(result.counts)
  if (status === 'salvaged' && result.ok === true) {
    if (result.verified_on_disk !== true) {
      return {
        ok: false, tone: 'danger', title: '抢救写了但回读对不上',
        detail: '服务端写完后重新读盘，没能证明账本已经能正常读回。',
        nextStep: '人工只读核对本地账本后再决定，不要直接上传。',
        blockingRetained: true, ledgerChanged: result.changed === true,
      }
    }
    return {
      ok: true, tone: 'success', title: '账本已抢救',
      detail: `${counts}。原件已留档，云端一个格子都没动。`,
      nextStep: '重新点「预览」核对；丢掉的那些人如果这一批该加，走「账本锚点重建」。',
      blockingRetained: true, ledgerChanged: true,
    }
  }
  switch (status) {
    case 'salvage_ready':
      return {
        ok: true, tone: 'info', title: '抢救清单（还没写任何东西）',
        detail: `${counts}。`,
        nextStep: '逐条核对清单，确认后输入确认词写入。',
        blockingRetained: true, ledgerChanged: false,
      }
    case 'nothing_to_salvage':
      return {
        ok: true, tone: 'info', title: '账本没有需要抢救的内容',
        detail: '账本读出来是好的，一个字节都没写。',
        nextStep: '重新预览；如果预览仍被拦，说明问题不在账本内容上。',
        blockingRetained: true, ledgerChanged: false,
      }
    case 'parse_failed':
      return {
        ok: false, tone: 'danger', title: '账本文件读不成 JSON，抢救不了',
        detail: '整个文件已经解析不了，只能按云端现状重建锚点；原件已留档。',
        nextStep: '改走「账本锚点重建」。',
        blockingRetained: true, ledgerChanged: false,
      }
    case 'unsalvageable':
    case 'missing':
    case 'unreadable':
      return {
        ok: false, tone: 'danger', title: '没有可抢救的内容',
        detail: String(result.reason || '账本文件不在或读不出来。'),
        nextStep: '检查数据目录后重试；不行就改走「账本锚点重建」。',
        blockingRetained: true, ledgerChanged: false,
      }
    case 'self_check_failed':
      return {
        ok: false, tone: 'danger', title: '抢救结果自查未通过，已放弃写入',
        detail: '服务端发现抢救后的账本自己都读不回来，因此一个字节都没写。',
        nextStep: '把这条报给维护者；先不要上传。',
        blockingRetained: true, ledgerChanged: false,
      }
    case 'confirmation_required':
    case 'note_required':
      return {
        ok: false, tone: 'warning', title: '确认不完整，未写账本',
        detail: '必须逐字输入确认词，并填写至少 4 个字符的核对说明。',
        nextStep: '补全后重新提交；本次没有改任何文件。',
        blockingRetained: true, ledgerChanged: false,
      }
    case 'operation_conflict':
      return {
        ok: false, tone: 'warning', title: '另一个操作正在跑',
        detail: '有上传或恢复正在写账本，这时候抢救会覆盖它刚写下的锚点，所以先不救。',
        nextStep: '等它结束后重新生成清单；不要重复提交。',
        blockingRetained: true, ledgerChanged: false,
      }
    case 'lock_unavailable':
      return {
        ok: false, tone: 'danger', title: '本地存储不可用',
        detail: String(result.reason || '拿不到操作锁。'),
        nextStep: '检查磁盘/权限后重试。',
        blockingRetained: true, ledgerChanged: false,
      }
    default:
      break
  }
  if (result.ok === true) {
    return {
      ok: true, tone: 'info', title: '抢救未发生写入',
      detail: String(result.reason || '服务端没有改动本地账本。'),
      nextStep: '重新预览后再决定。',
      blockingRetained: true, ledgerChanged: result.changed === true,
    }
  }
  return {
    ok: false, tone: 'danger', title: '抢救未完成',
    detail: String(result.reason || '服务端拒绝了本次抢救，未写云端。'),
    nextStep: '按提示处理后重试；处理前不要直接上传。',
    blockingRetained: true, ledgerChanged: result.changed === true,
  }
}
