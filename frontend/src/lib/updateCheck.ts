/**
 * 更新检查节流：浏览器每次刷新都打 GitHub API 会撞匿名限流（60 次/小时）。
 * 自动检查每 6 小时最多一次；手动“检查更新”不受这里限制。
 */

export const AUTO_CHECK_INTERVAL_MS = 6 * 60 * 60 * 1000
export const AUTO_CHECK_STORAGE_KEY = 'yikou.update.autoCheckAt.v1'

/** storage 只需实现 getItem/setItem，便于 Node 测试注入假对象。 */
export interface UpdateCheckStorage {
  getItem(key: string): string | null
  setItem(key: string, value: string): void
}

export function shouldAutoCheckUpdates(
  storage: UpdateCheckStorage,
  now: number = Date.now(),
): boolean {
  try {
    const raw = storage.getItem(AUTO_CHECK_STORAGE_KEY)
    const last = raw ? Number(raw) : 0
    if (Number.isFinite(last) && last > 0 && now - last < AUTO_CHECK_INTERVAL_MS) {
      return false
    }
    storage.setItem(AUTO_CHECK_STORAGE_KEY, String(now))
    return true
  } catch {
    // 隐私模式等 storage 不可用时：不因为节流失败而阻止检查。
    return true
  }
}
