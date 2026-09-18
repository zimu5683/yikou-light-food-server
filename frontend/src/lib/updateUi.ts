/**
 * 更新弹窗的纯展示工具：只负责把字节数/进度变成可读文本，便于 Node 单测。
 */

export function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes <= 0) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB']
  let value = bytes
  let index = 0
  while (value >= 1024 && index < units.length - 1) {
    value /= 1024
    index += 1
  }
  const digits = value >= 100 ? 0 : 1
  return `${value.toFixed(digits)} ${units[index]}`
}

export function updateProgressText(
  phase: 'downloading' | 'verifying' | 'installing',
  percent: number,
  downloaded?: number,
  total?: number,
  message?: string,
): string {
  if (phase === 'downloading') {
    const suffix = downloaded !== undefined && total
      ? `（${formatBytes(downloaded)} / ${formatBytes(total)}）`
      : ''
    return `正在下载 ${Math.max(0, Math.min(100, Math.round(percent)))}%${suffix}`
  }
  if (phase === 'verifying') return '正在校验安装包…'
  return message || '已打开系统安装器，请按手机提示完成安装'
}
