/**
 * 密码清除的三态语义判断（A 的 clear_password additive 契约）。
 *
 * 只有后端明确删除成功或确认原本不存在，才允许清空草稿并提示成功；
 * 失败、断线、未知结果一律保留草稿，不显示“已清除”。
 * 不记录、不返回密码内容。
 */
import type { ClearPasswordResult } from './bridge.ts'

export type CredentialClearToast = 'success' | 'error'

export interface CredentialClearDecision {
  clearDraft: boolean
  toast: CredentialClearToast
  status: string
  state: string
  mode: string
  deleted: boolean
  reason: string
  nextAction: string
}

const ACCEPTED_STATES = new Set(['deleted', 'account_empty', 'already_cleared'])

export function evaluatePasswordClearResult(
  result: ClearPasswordResult | null | undefined,
  thrownMessage = '',
): CredentialClearDecision {
  if (!result) {
    return {
      clearDraft: false, toast: 'error', status: 'error', state: 'unconfirmed',
      mode: '', deleted: false,
      reason: thrownMessage || '清除密码请求未确认',
      nextAction: '检查网络或系统凭据状态后重试；当前草稿保持不变，不要把失败当作已清除',
    }
  }
  const state = String(result.state || '')
  if (result.ok === true && ACCEPTED_STATES.has(state)) {
    return {
      clearDraft: true, toast: 'success', status: result.status || 'success',
      state, mode: String(result.mode || ''), deleted: Boolean(result.deleted),
      reason: '', nextAction: '',
    }
  }
  return {
    clearDraft: false, toast: 'error',
    status: result.status || 'error',
    state: state || 'unknown',
    mode: String(result.mode || ''),
    deleted: false,
    reason: result.reason || thrownMessage || '服务端未能确认密码已清除',
    nextAction: result.next_action || '请在系统凭据管理器中确认后重试；不要把失败当作已清除',
  }
}
