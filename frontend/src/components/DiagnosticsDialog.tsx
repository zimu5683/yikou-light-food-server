/**
 * 闪时送提交诊断日志查看 / 复制 / 导出弹窗。
 *
 * 背景：诊断日志（`sss-diagnostics/*.jsonl`）写在设备私有目录里，手机上的普通
 * 文件管理器与 Termux 都打不开，用户只能看到日志里的路径、看不到内容。这里提供
 * 应用内查看、一键复制与保存为文件（保存依赖浏览器下载能力；内嵌页里若不可用，
 * 界面会明确提示改用「复制全部」）。
 *
 * 日志记录本身已脱敏：不含姓名、电话、地址、请求正文与凭据。
 */
import { useCallback, useEffect, useState } from 'react'

import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import {
  api,
  isApiReady,
  type SssDiagnosticsFile,
  type SssDiagnosticsReadResult,
} from '@/lib/bridge'
import { copyText } from '@/lib/clipboard'
import { classifyRequestError } from '@/lib/requestError'
import {
  diagnosticsDownloadName,
  diagnosticsTailNote,
  formatBytes,
} from '@/lib/sssDiagnostics'

function formatTime(mtime: number): string {
  const value = Number(mtime)
  if (!Number.isFinite(value) || value <= 0) return ''
  try {
    return new Date(value * 1000).toLocaleString()
  } catch {
    return ''
  }
}

export function DiagnosticsDialog({ open, onOpenChange }: {
  open: boolean
  onOpenChange: (open: boolean) => void
}) {
  const [files, setFiles] = useState<SssDiagnosticsFile[]>([])
  const [directory, setDirectory] = useState('')
  const [selected, setSelected] = useState('')
  const [read, setRead] = useState<SssDiagnosticsReadResult | null>(null)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [busy, setBusy] = useState(false)

  const load = useCallback(async (name?: string) => {
    if (!isApiReady()) {
      setError('后端 API 尚未就绪，请稍后重试')
      return
    }
    setBusy(true)
    setError('')
    setNotice('')
    try {
      const list = await api().sss_diagnostics_files()
      if (!list.ok) {
        throw new Error(list.next_action || '读取诊断日志列表失败')
      }
      const items = list.files || []
      setFiles(items)
      setDirectory(list.directory || '')
      const wanted = name && items.some((item) => item.name === name)
        ? name
        : (items[0]?.name || '')
      if (!wanted) {
        setSelected('')
        setRead(null)
        return
      }
      const result = await api().sss_diagnostics_read(wanted)
      if (!result.ok) {
        throw new Error(result.reason || '读取诊断日志失败')
      }
      setSelected(wanted)
      setRead(result)
    } catch (err) {
      setError(classifyRequestError(err).detail || '读取诊断日志失败')
      setFiles([])
      setSelected('')
      setRead(null)
    } finally {
      setBusy(false)
    }
  }, [])

  useEffect(() => {
    if (!open) return
    // 延到下一拍再加载：不在 effect 体内同步 setState（React Compiler 门禁），
    // 也让弹窗先渲染出来再进入读取态；关闭时取消未开始的加载。
    const timer = window.setTimeout(() => { void load() }, 0)
    return () => window.clearTimeout(timer)
  }, [open, load])

  const content = read?.content || ''

  async function copyAll() {
    const outcome = await copyText(content)
    setNotice(outcome.ok ? '已复制全部内容' : (outcome.error || '复制失败：请长按文本框选择后复制'))
  }

  function saveFile() {
    const name = diagnosticsDownloadName(selected)
    if (!name || !content) return
    try {
      const url = URL.createObjectURL(new Blob([content], { type: 'application/x-ndjson' }))
      const link = document.createElement('a')
      link.href = url
      link.download = name
      document.body.appendChild(link)
      link.click()
      link.remove()
      URL.revokeObjectURL(url)
      setNotice(`已触发保存 ${name}；若当前环境没有出现保存提示，请改用「复制全部」。`)
    } catch {
      setNotice('当前环境不支持保存文件，请改用「复制全部」。')
    }
  }

  return (
    <Dialog open={open} onOpenChange={(next) => { if (!busy) onOpenChange(next) }}>
      <DialogContent className="sm:max-w-2xl rounded-lg">
        <DialogHeader>
          <DialogTitle className="font-serif">闪时送诊断日志</DialogTitle>
          <DialogDescription>
            每次真实提交写一条脱敏记录（不含姓名、电话与请求正文）。可按日期查看最近记录，
            复制或保存为 .jsonl 文件发给技术支持。
          </DialogDescription>
        </DialogHeader>
        {directory && (
          <p className="truncate text-[11px] text-muted-foreground" title={directory}>目录：{directory}</p>
        )}
        {error && (
          <p role="alert" className="rounded-md border border-destructive/40 bg-destructive/5 px-3 py-2 text-xs text-destructive">{error}</p>
        )}
        {!error && files.length === 0 && !busy && (
          <p className="text-xs text-muted-foreground">暂无诊断日志：发生过闪时送提交后才会生成。</p>
        )}
        {files.length > 0 && (
          <div className="flex max-h-32 flex-wrap gap-1 overflow-y-auto" role="listbox" aria-label="诊断日志文件">
            {files.map((file) => (
              <button
                key={file.name}
                type="button"
                role="option"
                aria-selected={file.name === selected}
                disabled={busy}
                onClick={() => void load(file.name)}
                className={`rounded border px-2 py-1 text-left text-[11px] leading-4 ${file.name === selected
                  ? 'border-foreground/40 bg-secondary font-medium'
                  : 'border-border text-muted-foreground hover:bg-secondary'}`}
                title={formatTime(file.mtime)}
              >
                {file.name}
                <span className="ml-1 opacity-70">{formatBytes(file.size)}</span>
              </button>
            ))}
          </div>
        )}
        {content && (
          <>
            {read?.truncated && (
              <p className="text-[11px] text-warning">{diagnosticsTailNote(true)}</p>
            )}
            <pre className="max-h-[45vh] min-h-24 overflow-auto rounded-md border bg-card px-3 py-2 font-mono text-[11px] leading-4 whitespace-pre-wrap select-text">
              {content}
            </pre>
          </>
        )}
        {notice && <p className="text-[11px] text-muted-foreground">{notice}</p>}
        <DialogFooter className="flex-wrap gap-2">
          <Button variant="ghost" className="h-9 text-xs" disabled={busy} onClick={() => void load(selected || undefined)}>
            {busy ? '读取中…' : '刷新'}
          </Button>
          <Button variant="ghost" className="h-9 text-xs" disabled={!content} onClick={() => void copyAll()}>
            复制全部
          </Button>
          <Button className="h-9 rounded-[6px] text-xs" disabled={!content} onClick={saveFile}>
            保存 .jsonl
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
