/**
 * 密码草稿与“清除密码”同步规则。
 * 普通配置刷新不应覆盖正在输入的密码；只有清除成功后的 passwordReset 信号才清空对应模式。
 */

export interface PasswordResetLike {
  mode: 'order' | 'sss' | null
  nonce: number
}

export function passwordResetVersion(reset: PasswordResetLike, mode: 'order' | 'sss'): number {
  return reset.mode === mode ? reset.nonce : 0
}

export function shouldResetPasswordDraft(
  reset: PasswordResetLike,
  mode: 'order' | 'sss',
): boolean {
  return reset.mode === mode && reset.nonce > 0
}

export function nextPasswordDraft(
  current: string,
  reset: PasswordResetLike,
  mode: 'order' | 'sss',
): string {
  return shouldResetPasswordDraft(reset, mode) ? '' : current
}
