/**
 * 闪时送提交诊断日志（`sss-diagnostics/*.jsonl`）查看/导出的纯函数辅助。
 *
 * 日志写在设备私有目录（Android 上普通文件管理器打不开），只能在应用内查看、
 * 复制或保存。这里只放可单测的格式化逻辑，网络调用在 `lib/bridge.ts`。
 */

/** 后端单次读取上限（与 bridge 的 `_SSS_DIAGNOSTICS_READ_LIMIT` 同口径）。 */
export const SSS_DIAGNOSTICS_READ_LIMIT = 1024 * 1024

/** 文件大小的人类可读文本（B / KB / MB）。 */
export function formatBytes(size: number): string {
  const value = Number(size)
  if (!Number.isFinite(value) || value <= 0) return '0 B'
  if (value < 1024) return `${Math.round(value)} B`
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`
  return `${(value / (1024 * 1024)).toFixed(2)} MB`
}

/** 校验诊断文件名：必须是**裸**的 `*.jsonl` 文件名（与后端 raw==Path(raw).name 同口径）。 */
export function diagnosticsFileName(name: string): string {
  const base = String(name || '').trim()
  if (!base || base !== base.split(/[\\/]/).pop()) return ''
  return base.endsWith('.jsonl') && base !== '.jsonl' ? base : ''
}

/** 保存到本地时使用的文件名（加前缀，避免与其它文件混淆）。 */
export function diagnosticsDownloadName(name: string): string {
  const base = diagnosticsFileName(name)
  return base ? `sss-diagnostics-${base}` : ''
}

/** 截断提示：只在真的截断时给一句可执行的说明，否则空串（界面不啰嗦）。 */
export function diagnosticsTailNote(truncated: boolean,
                                    limit: number = SSS_DIAGNOSTICS_READ_LIMIT): string {
  return truncated ? `文件较大，仅显示末尾 ${formatBytes(limit)}（已对齐到行边界）。` : ''
}
