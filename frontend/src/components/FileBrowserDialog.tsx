/**
 * 服务器端文件浏览器（网页版专用）。
 *
 * 任务在服务器（手机）上跑，Excel 也在手机上，所以这里选的是**服务端**路径；
 * 浏览器自带的 <input type="file"> 只能拿到客户端自己的文件，对远程操作没有用。
 * 列表来自 `GET /api/fs/list`（见 app/web/server.py），只返回目录与 Excel 文件。
 */
import { useEffect, useState } from 'react'
import { ArrowUp, FileSpreadsheet, Folder, Loader2 } from 'lucide-react'
import { TextInput } from '@/components/fields'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { ScrollArea } from '@/components/ui/scroll-area'
import { listServerDir, type FsEntry, type FsShortcut } from '@/lib/bridge'
import { cn } from '@/lib/utils'

const EXCEL_RE = /\.(xlsx|xlsm|xls)$/i

/** 从已有路径推出要打开的目录：是 Excel 文件就取其所在目录。 */
function dirOf(path: string): string {
  const trimmed = path.trim()
  if (!trimmed) return ''
  if (EXCEL_RE.test(trimmed)) {
    const cut = trimmed.lastIndexOf('/')
    return cut > 0 ? trimmed.slice(0, cut) : '/'
  }
  return trimmed
}

function baseName(path: string): string {
  const cut = path.lastIndexOf('/')
  return cut >= 0 ? path.slice(cut + 1) : path
}

function humanSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`
}

export function FileBrowserDialog({
  open,
  mode,
  initialPath = '',
  onCancel,
  onPick,
}: {
  open: boolean
  mode: 'open' | 'save'
  initialPath?: string
  onCancel: () => void
  onPick: (path: string) => void
}) {
  const [current, setCurrent] = useState('')
  const [parent, setParent] = useState('')
  const [entries, setEntries] = useState<FsEntry[]>([])
  const [shortcuts, setShortcuts] = useState<FsShortcut[]>([])
  const [fileName, setFileName] = useState('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')

  async function load(path: string) {
    setLoading(true)
    setError('')
    try {
      const result = await listServerDir(path)
      setCurrent(result.path)
      setParent(result.parent ?? '')
      setEntries(result.entries ?? [])
      setShortcuts(result.shortcuts ?? [])
      setError(result.error ?? '')
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
      setEntries([])
    } finally {
      setLoading(false)
    }
  }

  // 每次打开都重新定位到初始目录（可能是上次选的路径）。
  useEffect(() => {
    if (!open) return
    setFileName(mode === 'save' ? (EXCEL_RE.test(initialPath) ? baseName(initialPath) : '排单.xlsx') : '')
    void load(dirOf(initialPath))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, initialPath, mode])

  const savePath = current && fileName.trim()
    ? `${current.replace(/\/$/, '')}/${fileName.trim()}`
    : ''

  return (
    <Dialog open={open} onOpenChange={(next) => { if (!next) onCancel() }}>
      <DialogContent className="sm:max-w-lg rounded-lg">
        <DialogHeader>
          <DialogTitle className="font-serif">
            {mode === 'save' ? '选择保存位置（手机上的目录）' : '选择 Excel 文件（手机上的文件）'}
          </DialogTitle>
          <DialogDescription className="break-all font-mono text-[11px]">
            {current || '正在读取…'}
          </DialogDescription>
        </DialogHeader>

        {shortcuts.length > 0 && (
          <div className="flex flex-wrap gap-1.5">
            {shortcuts.map((item) => (
              <Button
                key={item.path}
                variant="outline"
                size="sm"
                className="h-7 rounded-[4px] px-2 text-[11px]"
                onClick={() => void load(item.path)}
              >
                {item.name}
              </Button>
            ))}
          </div>
        )}

        <ScrollArea className="h-64 rounded-[4px] border border-border">
          <div className="p-1">
            {parent && (
              <button
                type="button"
                className="flex w-full items-center gap-2 rounded-[4px] px-2 py-1.5 text-left text-[13px] hover:bg-secondary"
                onClick={() => void load(parent)}
              >
                <ArrowUp className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
                <span className="text-muted-foreground">上一级</span>
              </button>
            )}
            {loading && (
              <div className="flex items-center gap-2 px-2 py-3 text-xs text-muted-foreground">
                <Loader2 className="h-3.5 w-3.5 animate-spin" />
                正在读取…
              </div>
            )}
            {!loading && error && (
              <p className="px-2 py-3 text-xs text-destructive">{error}</p>
            )}
            {!loading && !error && entries.length === 0 && (
              <p className="px-2 py-3 text-xs text-muted-foreground">
                这个目录里没有子目录或 Excel 文件。
              </p>
            )}
            {!loading &&
              entries.map((entry) => (
                <button
                  key={entry.path}
                  type="button"
                  className={cn(
                    'flex w-full items-center gap-2 rounded-[4px] px-2 py-1.5 text-left text-[13px] hover:bg-secondary',
                    !entry.is_dir && mode === 'save' && 'opacity-40',
                  )}
                  onClick={() => {
                    if (entry.is_dir) {
                      void load(entry.path)
                    } else if (mode === 'open') {
                      onPick(entry.path)
                    } else {
                      // 保存模式点已有文件＝沿用它的文件名（换目录另存）。
                      setFileName(entry.name)
                    }
                  }}
                >
                  {entry.is_dir ? (
                    <Folder className="h-3.5 w-3.5 shrink-0 text-primary" />
                  ) : (
                    <FileSpreadsheet className="h-3.5 w-3.5 shrink-0 text-success" />
                  )}
                  <span className="min-w-0 flex-1 truncate">{entry.name}</span>
                  {!entry.is_dir && (
                    <span className="shrink-0 text-[11px] text-muted-foreground">
                      {humanSize(entry.size)}
                    </span>
                  )}
                </button>
              ))}
          </div>
        </ScrollArea>

        {mode === 'save' && (
          <TextInput
            value={fileName}
            onChange={(event) => setFileName(event.target.value)}
            placeholder="文件名，例如 排单.xlsx"
          />
        )}

        <DialogFooter className="flex-row items-center justify-end gap-2">
          {mode === 'open' && (
            <span className="mr-auto text-[11px] text-muted-foreground">
              点文件名即可选用
            </span>
          )}
          <Button
            variant="outline"
            className="h-8 rounded-[6px] text-xs"
            onClick={onCancel}
          >
            取消
          </Button>
          {mode === 'save' && (
            <Button
              className="h-8 rounded-[6px] text-xs"
              disabled={!savePath}
              onClick={() => savePath && onPick(savePath)}
            >
              保存到这里
            </Button>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
