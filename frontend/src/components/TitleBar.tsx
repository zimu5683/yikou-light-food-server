/**
 * 网页版页头：印章 Logo + 宋体产品名 + 版本徽章 + 主题切换 + 停止服务。
 *
 * 原实现是 pywebview 无边框窗口的自绘标题栏，带最小化/最大化/关闭按钮和
 * 标题栏拖拽区（历史遗留的桌面端 GTK 方案已移除）。
 * 目标，浏览器里也没有可操作的原生窗口，这些全部去掉。
 *
 * 「停止服务」保留——它会在服务端真的关掉后端服务，是有意义的能力——但网页版
 * 加了二次确认：`Bridge.request_close` 在空闲时是**直接销毁、没有任何确认**的，
 * 手机上误触一下就把服务停了。
 */
import { useEffect, useState } from 'react'
import { Moon, Power, Sun } from 'lucide-react'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { useApp } from '@/hooks/appContext'
import { applyTheme, initialTheme, type Theme } from '@/lib/theme'

export function TitleBar() {
  const { version, mocked, transport, requestClose } = useApp()
  const web = transport === 'http'
  const [theme, setTheme] = useState<Theme>(initialTheme)
  const [confirmStop, setConfirmStop] = useState(false)

  // 启动时把持久化的主题真正应用上去（仅保存状态不会切换 .dark 类）。
  useEffect(() => {
    applyTheme(theme)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  function toggleTheme() {
    const next: Theme = theme === 'light' ? 'dark' : 'light'
    setTheme(next)
    applyTheme(next)
  }

  return (
    <header
      className="flex h-10 shrink-0 select-none items-center gap-2.5 border-b bg-background px-3 sm:px-4"
      // 刘海屏 / 圆角屏：状态栏高度不能压在内容上
      style={{ paddingTop: 'var(--safe-top)', height: 'calc(2.5rem + var(--safe-top))' }}
    >
      <div className="flex min-w-0 flex-1 items-center gap-2.5 overflow-hidden">
        <div className="flex h-6 w-6 shrink-0 items-center justify-center rounded-[3px] bg-primary font-serif text-[13px] font-bold text-primary-foreground">
          轻
        </div>
        <span className="shrink-0 whitespace-nowrap font-serif text-base font-semibold tracking-[1px]">
          一口轻食
        </span>
        {/* 窄屏先收起副标题：它在手机上最先被挤变形 */}
        <span className="hidden truncate text-xs text-muted-foreground sm:inline">订单自动处理台</span>
        <Badge
          variant="outline"
          className="tabular ml-1 shrink-0 rounded-[3px] font-mono text-[11px] font-normal"
        >
          v{version || '…'}
        </Badge>
        {web && <span className="shrink-0 whitespace-nowrap text-xs text-primary">网页版</span>}
        {mocked && !web && (
          <span className="truncate text-xs text-warning">浏览器 mock 模式</span>
        )}
      </div>

      <div className="flex shrink-0 items-center gap-1.5">
        <Button
          variant="ghost"
          size="sm"
          className="h-8 rounded-[4px] px-2 text-xs text-muted-foreground hover:text-foreground touch-target"
          onClick={toggleTheme}
          aria-label={theme === 'light' ? '切换到深色' : '切换到浅色'}
        >
          {theme === 'light' ? <Moon className="size-4" /> : <Sun className="size-4" />}
          <span className="hidden sm:inline">{theme === 'light' ? '深色' : '浅色'}</span>
        </Button>
        <Button
          variant="ghost"
          size="sm"
          className="h-8 rounded-[4px] px-2 text-xs text-muted-foreground hover:bg-destructive/10 hover:text-destructive touch-target"
          onClick={() => (web ? setConfirmStop(true) : requestClose())}
          aria-label="停止服务"
        >
          <Power className="size-4" />
          <span className="hidden sm:inline">停止服务</span>
        </Button>
      </div>

      <Dialog open={confirmStop} onOpenChange={setConfirmStop}>
        <DialogContent className="sm:max-w-sm rounded-lg">
          <DialogHeader>
            <DialogTitle className="font-serif">停止服务</DialogTitle>
            <DialogDescription className="whitespace-pre-wrap">
              会关掉这台机器上的服务进程，所有设备都会断开，需要重新启动才能再用。
              {'\n'}任务不会自动完成，正在运行的任务会一并中止。
            </DialogDescription>
          </DialogHeader>
          <DialogFooter className="flex-row justify-end gap-2">
            <Button
              variant="outline"
              className="h-9 rounded-[6px] text-xs touch-target"
              onClick={() => setConfirmStop(false)}
            >
              取消
            </Button>
            <Button
              className="h-9 rounded-[6px] bg-destructive text-xs text-white hover:bg-destructive/90 touch-target"
              onClick={() => {
                setConfirmStop(false)
                requestClose()
              }}
            >
              停止服务
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </header>
  )
}
