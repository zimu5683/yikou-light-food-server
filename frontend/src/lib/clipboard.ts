/**
 * 复制文本：优先 Clipboard API，失败时退回 `textarea + execCommand`。
 *
 * **为什么需要兜底**：日志复制跑在 Android WebView 里。`navigator.clipboard`
 * 在非安全上下文（`file://`、http 局域网地址）或权限被拒时**不存在或直接抛错**，
 * 只写 `navigator.clipboard.writeText` 会让「复制」在真机上静默失效 ——
 * 界面什么都不显示，用户以为复制成功了。
 *
 * 失败必须给出可执行的下一步（长按选择），不能静默吞掉。
 */

export interface CopyOutcome {
  ok: boolean
  /** 失败原因（给用户看的短句，不含日志内容）。 */
  error?: string
}

/** 兜底复制：临时 textarea + execCommand，覆盖没有 Clipboard API 的 WebView。 */
function fallbackCopy(text: string): boolean {
  if (typeof document === 'undefined' || !document.body) return false
  const area = document.createElement('textarea')
  area.value = text
  area.setAttribute('readonly', '')
  area.style.cssText = 'position:fixed;top:0;left:0;width:1px;height:1px;opacity:0;pointer-events:none'
  document.body.appendChild(area)
  try {
    area.select()
    area.setSelectionRange(0, area.value.length)
    return document.execCommand('copy') === true
  } catch {
    return false
  } finally {
    area.remove()
  }
}

/**
 * 复制一段文本。
 *
 * 空文本直接判失败并说明原因 —— 空字符串写进剪贴板会**清掉用户原有的剪贴板内容**，
 * 比「什么都没发生」更糟。
 */
export async function copyText(text: string): Promise<CopyOutcome> {
  if (!text) return { ok: false, error: '没有可复制的内容' }
  const clipboard = typeof navigator === 'undefined' ? undefined : navigator.clipboard
  if (clipboard && typeof clipboard.writeText === 'function') {
    try {
      await clipboard.writeText(text)
      return { ok: true }
    } catch {
      // 非安全上下文 / 权限被拒 / 用户未授权：继续走兜底，不直接判失败。
    }
  }
  return fallbackCopy(text) ? { ok: true } : { ok: false, error: '浏览器拒绝了复制，请长按日志手动选择' }
}
