/**
 * 页头：印章 Logo + 宋体产品名 + 版本徽章 + 主题切换。
 *
 * 手机端右上角固定着日志按钮（`LogFab`），所以这里在窄屏给日志按钮预留
 * 56px 宽度；「主题切换」正好落在日志按钮左侧。日志全屏时整条页头会被盖住，
 * 但日志按钮由 LogFab 自己维持 z-index，始终可见。
 */
import { useEffect, useState } from 'react'
import { Moon, Sun } from 'lucide-react'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { useApp } from '@/hooks/appContext'
import { applyTheme, initialTheme, type Theme } from '@/lib/theme'

export function TitleBar() {
  const { version, mocked } = useApp()
  const [theme, setTheme] = useState<Theme>(initialTheme)

  // 启动时把持久化的主题真正应用上去（仅保存状态不会切换 .dark 类）。
  useEffect(() => {
    applyTheme(theme)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  function toggleTheme() {
    const next = theme === 'light' ? 'dark' : 'light'
    setTheme(next)
    applyTheme(next)
  }

  return (
    <header
      className="flex h-10 shrink-0 select-none items-center gap-2.5 border-b bg-background pl-3 pr-[calc(4rem+var(--safe-right))] sm:px-4"
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
        {mocked && (
          <span className="truncate text-xs text-warning">浏览器 mock 模式</span>
        )}
      </div>

      <div className="flex shrink-0 items-center">
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
      </div>
    </header>
  )
}
