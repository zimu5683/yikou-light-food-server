/**
 * 更新检查节流：App 每次打开都会检查；短节流只用于挡住页面反复刷新造成的重复请求，
 * 避免撞 GitHub 匿名限流（60 次/小时）。手动“检查更新”不受这里限制。
 */

//: App 打开后最多每 5 分钟自动查一次；正常使用频率下每次打开都会检查。
export const AUTO_CHECK_INTERVAL_MS = 5 * 60 * 1000
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
