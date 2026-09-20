/**
 * 机器码 → 用户可见的「发生了什么 + 下一步」。
 *
 * 覆盖 `docs/BRIDGE-WPS-CONTRACT-R6.md` 里前端必须正确展示的状态：
 *
 * - 并发：`blocked_concurrent` / `operation_conflict` —— 本次**没有发送任何请求**，
 *   级别是 warning（不是 error），也不需要站内对账；
 * - 开关：`wps_disabled` —— `wps_enabled=false` 时预览/上传直接被拒，且已发出的
 *   一次性令牌会立即作废；
 * - 本地状态：`local_state_blocked`（journal/账本损坏、**版本不受支持**）、
 *   `journal_write_failed` / `journal_unreadable`（**持久化失败**）、
 *   `unsupported_status_requires_manual`；
 * - 云端证明不足：`cloud_verify_failed` / `cloud_not_untouched` —— 只能人工核对，
 *   **不得**当作可重传。
 *
 * 铁律：修复失败时「保持阻断」，绝不提示“删除账本”或“直接重传”。
 */
import type { WpsUploadResult } from './bridge.ts'

/** 全局统一并发文案（R6 §11 第 7 条）。 */
export const CONCURRENT_TASK_TEXT = '另一个任务正在运行，请等待后刷新'

export type FailureTone = 'danger' | 'warning' | 'info'

export interface FailureView {
  code: string
  tone: FailureTone
  title: string
  detail: string
  nextStep: string
  /** 是否可证明本次零云端写入。 */
  provenNoWrite: boolean
  /** 是否允许提示“重新上传/重试写入”。 */
  retryAllowed: boolean
  /** 失败后是否仍然阻断（禁止走捷径）。 */
  blockingRetained: boolean
  /** 是否需要人工只读核对。 */
  needsManualReconcile: boolean
}

function base(code: string, tone: FailureTone, title: string, detail: string, nextStep: string): FailureView {
  return {
    code, tone, title, detail, nextStep,
    provenNoWrite: false, retryAllowed: false, blockingRetained: false, needsManualReconcile: false,
  }
}

/**
 * 本地日志问题细分：损坏、**版本不受支持**、不可读、**持久化（写入）失败**。
 *
 * 后端把 journal 异常统一收敛成 `local_state_blocked` / `wps_recovery_journal_unreadable`，
 * 版本不支持与 JSON 损坏只在 `reason` 文本里区分，所以这里必须同时看文本。
 */
export type JournalIssue = 'corrupt' | 'version_unsupported' | 'unreadable' | 'write_failed' | ''

export function journalIssueOf(code: string, reason: string): JournalIssue {
  const codeText = String(code || '')
  const text = `${codeText} ${reason}`
  if (/journal_write_failed|journal_save_failed|写入失败|落盘失败|persist/i.test(text)) return 'write_failed'
  if (/版本|version/i.test(text) && /(不支持|unsupported)/i.test(text)) return 'version_unsupported'
  // 纯「读不到」与「结构损坏」要分开：前者是权限/磁盘问题，后者要修内容或迁移版本。
  if (/^(wps_recovery_)?(journal|ledger)_unreadable$/i.test(codeText)) return 'unreadable'
  if (/journal_unreadable|ledger_unreadable|local_state_blocked|损坏|不可读|corrupt|unavailable|结构非法/i.test(text)) {
    return /不可读|unreadable/i.test(text) && !/损坏|corrupt|结构非法|local_state_blocked/i.test(text)
      ? 'unreadable'
      : 'corrupt'
  }
  return ''
}

const JOURNAL_COPY: Record<Exclude<JournalIssue, ''>, { title: string; detail: string; nextStep: string }> = {
  corrupt: {
    title: '本地账本/意图日志损坏或不可读',
    detail: '本地恢复日志无法解析，服务端为保证不重复写入已拒绝本次操作；本次没有写云端。',
    nextStep: '请联系管理员走「旧任务恢复/退场」入口修复本地日志；修复前不要重新上传或补发。',
  },
  version_unsupported: {
    title: '本地日志版本不受支持',
    detail: '意图日志的版本高于当前程序支持的版本，服务端失败关闭，本次没有写云端。',
    nextStep: '请升级到匹配版本或联系管理员迁移本地日志；不要删除日志，也不要直接重传。',
  },
  unreadable: {
    title: '本地恢复日志不可读',
    detail: '服务端无法读取本地恢复记录，因此无法证明可以安全写入。',
    nextStep: '联系管理员修复文件权限/磁盘后重新只读核对；不要删除账本，也不要直接重传。',
  },
  write_failed: {
    title: '本地日志持久化失败',
    detail: '本次操作无法把意图/结果写入本地日志，服务端因此不认为操作已生效；不能当作成功。',
    nextStep: '先修复本地磁盘/权限问题，再重新只读核对状态；确认前不要重复提交。',
  },
}

function journalFailure(code: string, reason: string, issue: JournalIssue): FailureView | null {
  if (!issue) return null
  const copy = JOURNAL_COPY[issue]
  return {
    code: code || `journal_${issue}`,
    tone: 'danger',
    title: copy.title,
    detail: `${copy.detail}${reason ? `（服务端说明：${reason}）` : ''}`,
    nextStep: copy.nextStep,
    provenNoWrite: true,
    retryAllowed: false,
    blockingRetained: true,
    needsManualReconcile: true,
  }
}

/** 上传结果（成功/失败/拒绝）→ 展示视图。非成功结果一律不允许暗示成功。 */
export function uploadFailureView(result: WpsUploadResult | null | undefined): FailureView | null {
  if (!result || result.ok === true) return null
  const code = String(result.code || '')
  const status = String(result.status || '').toLowerCase()
  const reason = String(result.reason || '')
  const next = String(result.next_action || '')
  const provenNoWrite = result.execution_summary?.proven_no_write === true
    || (result.execution_summary as { counts_source?: string } | null | undefined)?.counts_source === 'rejected_before_write'

  const journal = journalFailure(code, reason, journalIssueOf(code, reason))
  if (journal) return { ...journal, provenNoWrite: journal.provenNoWrite || provenNoWrite }

  const withGuards = (view: FailureView): FailureView => ({
    ...view,
    provenNoWrite: view.provenNoWrite || provenNoWrite,
  })

  switch (code) {
    case 'wps_disabled':
      return withGuards({
        ...base(code, 'warning', '云文档同步已关闭',
          '`wps_enabled=false` 时预览与上传都会被服务端拒绝；本次没有读云端、没有写任何内容。',
          next),
        nextStep: next || '在「云文档同步」中开启开关后重新预览（旧的 preview_id 已立即作废）。',
        provenNoWrite: true,
        retryAllowed: false,
        blockingRetained: false,
      })
    case 'operation_conflict':
    case 'blocked_concurrent':
      return withGuards({
        ...base(code || 'blocked_concurrent', 'warning', '另一个任务正在运行',
          '已有互斥操作占用中（跨进程锁不可用或另一进程正在处理同一批次），本次没有发送任何请求。',
          next),
        nextStep: next || `${CONCURRENT_TASK_TEXT}；可用「刷新状态（只读）」查看当前是谁在跑。`,
        provenNoWrite: true,
        retryAllowed: false,
      })
    case 'missing_preview':
    case 'preview_not_found':
    case 'preview_expired':
    case 'preview_consumed':
    case 'preview_changed':
    case 'preview_invalidated':
      return withGuards({
        ...base(code, 'warning', '预览已失效或被消费（未写入）',
          '一次性上传令牌不再有效；服务端在写入前就拒绝，本次没有写云端。',
          next),
        nextStep: next || '重新生成只读预览后再决定是否上传；不要复用旧令牌。',
        provenNoWrite: true,
      })
    case 'local_file_changed':
    case 'local_file_unreadable':
      return withGuards({
        ...base(code, 'warning', '本地排单表已变化或不可读（未写入）',
          '服务端复核时发现本地表与预览不一致或读不到，已在写入前拒绝。',
          next),
        nextStep: next || '等文件稳定/修复路径后重新预览。',
        provenNoWrite: true,
      })
    case 'unauthenticated':
      return withGuards({
        ...base(code, 'warning', '尚未授权云文档（未写入）',
          '服务端未拿到有效云文档授权，已在写入前拒绝。',
          next),
        nextStep: next || '先完成云文档授权，再重新预览。',
        provenNoWrite: true,
      })
    case 'cloud_error':
    case 'unexpected':
      return withGuards({
        ...base(code, 'danger', '服务端异常：写入结果未知',
          '结果组装期间发生异常，`written/failed` 可能为 null，不能按 0 或计划数展示。',
          next),
        nextStep: next || '按「未知」处理：只读核对云端与日志后再决定，不要直接重复上传。',
        needsManualReconcile: true,
      })
    case 'cloud_verify_failed':
    case 'cloud_not_untouched':
      return withGuards({
        ...base(code, 'danger', '云端证明不了「已完成/完全未执行」',
          '只读核对没有拿到可以支撑结论的证据，因此不解除阻断。',
          next),
        nextStep: next || '下一步是人工只读核对（manual_reconcile）；不要当作可以重传。',
        needsManualReconcile: true,
        blockingRetained: true,
      })
    default:
      break
  }

  if (status === 'blocked_concurrent') {
    return withGuards({
      ...base(code || 'blocked_concurrent', 'warning', '另一个任务正在运行',
        reason || '跨进程锁不可用或另一进程正在处理同一批次，本次没有发送任何请求。',
        next),
      nextStep: next || `${CONCURRENT_TASK_TEXT}；不需要对账，等待锁释放后重试。`,
      provenNoWrite: true,
      retryAllowed: false,
    })
  }
  if (status === 'uncertain') {
    return withGuards({
      ...base(code || 'uncertain', 'warning', '上传结果不确定 · 待核对',
        reason || '可能已写入但无法确认；不能当作成功，也不能当作零写入。',
        next),
      nextStep: next || '只读核对云端与日志，不要重新上传。',
      needsManualReconcile: true,
    })
  }
  if (status === 'blocked') {
    return withGuards({
      ...base(code || 'blocked', 'danger', '上传被阻断 · 待核对',
        reason || '本地账本/意图日志不可用或仍有防重复闸门，服务端拒绝写入。',
        next),
      nextStep: next || '先修复本地日志或走管理员恢复入口，不要重传。',
      blockingRetained: true,
      needsManualReconcile: true,
    })
  }
  if (status === 'partial') {
    return withGuards({
      ...base(code || 'partial', 'warning', '部分表未确认 · 待核对',
        reason || '有表没有拿到确定结果，不能整体当作成功。',
        next),
      nextStep: next || '逐表只读核对失败/不确定项，不要整批重传。',
      needsManualReconcile: true,
    })
  }
  if (status === 'rejected') {
    return withGuards({
      ...base(code || 'rejected', 'warning', '上传被服务端拒绝（未写入）',
        reason || '前置闸门拒绝，本次没有写云端。',
        next),
      nextStep: next || '按服务端提示处理后重新预览。',
      provenNoWrite: true,
    })
  }
  if (status === 'recovered' || status === 'not_started') {
    return withGuards({
      ...base(code || status, 'warning', '本轮计划没有执行',
        reason || '服务端处理了历史未完成操作，本次计划没有执行；旧令牌不可复用。',
        next),
      nextStep: next || '重新预览后再决定是否上传。',
      provenNoWrite: true,
    })
  }
  if (status === 'failed' || status === 'error') {
    return withGuards({
      ...base(code || status, 'danger', '上传失败',
        reason || '服务端明确失败，未成功写入。',
        next),
      nextStep: next || '查看日志定位原因后重新预览。',
    })
  }
  return withGuards({
    ...base(code || status || 'unknown', 'warning', '上传未完成 · 待核对',
      reason || '没有拿到明确成功标记。',
      next),
    nextStep: next || '只读核对云端与日志后再决定；不要直接重复上传。',
    needsManualReconcile: true,
  })
}

/** 预览拒绝 → 展示视图（全部可证明零云端写入）。 */
export function previewFailureView(result: { code?: string; status?: string; reason?: string; next_action?: string } | null | undefined): FailureView {
  const code = String(result?.code || '')
  const reason = String(result?.reason || '')
  const next = String(result?.next_action || '')
  const journal = journalFailure(code, reason, journalIssueOf(code, reason))
  if (journal) return journal
  if (code === 'wps_disabled') {
    return {
      ...base(code, 'warning', '云文档同步已关闭',
        '开关关闭时预览会被直接拒绝：不读云端、不建计划、不发一次性令牌。',
        next),
      nextStep: next || '在「云文档同步」中开启开关后再试。',
      provenNoWrite: true,
    }
  }
  if (code === 'operation_conflict') {
    return {
      ...base(code, 'warning', '另一个任务正在运行',
        '只读入口也占用互斥槽位；本次预览被拒绝，没有读云端。',
        next),
      nextStep: next || `${CONCURRENT_TASK_TEXT}；可刷新只读状态查看当前是谁在跑。`,
      provenNoWrite: true,
    }
  }
  if (code === 'unauthenticated') {
    return {
      ...base(code, 'warning', '尚未授权云文档',
        '预览需要只读访问云端排单表。',
        next),
      nextStep: next || '先完成云文档授权再重新预览。',
      provenNoWrite: true,
    }
  }
  if (code === 'local_file_unreadable' || code === 'local_file_changed') {
    return {
      ...base(code, 'warning', '本地排单表读不到或读取期间被修改',
        '预览在读取本地表时失败，没有读云端、没有写任何内容。',
        next),
      nextStep: next || '等文件稳定后重新预览。',
      provenNoWrite: true,
    }
  }
  if (code === 'missing_excel' || code === 'missing_tables' || code === 'effective_tables_error') {
    return {
      ...base(code, 'warning', '写入目标/排单表未配置完整',
        '预览需要有效的本地排单表与云端目标表配置。',
        next),
      nextStep: next || '在「高级设置与写入目标」补全配置后重新预览。',
      provenNoWrite: true,
    }
  }
  return {
    ...base(code || 'preview_failed', 'danger', '预览失败',
      reason || '预览未成功；本次没有写云端。',
      next),
    nextStep: next || '查看日志后重试；不要跳过预览直接上传（上传必须带 preview_id）。',
    provenNoWrite: true,
  }
}

/** 并发/互斥操作的统一提示（operation_status 冲突、任务事件等共用）。 */
export function concurrentView(nextAction = '', modeLabel = ''): FailureView {
  return {
    code: 'operation_conflict',
    tone: 'warning',
    title: '另一个任务正在运行',
    detail: modeLabel ? `当前占用者：${modeLabel}。本次没有发送任何请求。` : '已有互斥操作占用中，本次没有发送任何请求。',
    nextStep: nextAction || `${CONCURRENT_TASK_TEXT}；可用「刷新状态（只读）」查看当前是谁在跑。`,
    provenNoWrite: true,
    retryAllowed: false,
    blockingRetained: false,
    needsManualReconcile: false,
  }
}
