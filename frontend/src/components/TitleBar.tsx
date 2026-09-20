/**
 * 紧凑顶栏：品牌/版本 + 当前任务 + 权威运行状态 + 安全模式 + 深浅主题。
 * 手机 APK 优先；状态不靠颜色单独表达，均有文字。
 */
import { useEffect, useState } from 'react'
import { Moon, Sun } from 'lucide-react'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { useApp } from '@/hooks/appContext'
import { applyTheme, initialTheme, type Theme } from '@/lib/theme'
import { cn } from '@/lib/utils'

const MODE_LABELS = {
  order: '订单处理',
  cloud: '云文档同步',
  sss: '闪时送下单',
} as const

export function TitleBar() {
  const { version, mocked, mode, operationView, config } = useApp()
  const [theme, setTheme] = useState<Theme>(initialTheme)

  useEffect(() => {
    applyTheme(theme)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  function toggleTheme() {
    const next = theme === 'light' ? 'dark' : 'light'
    setTheme(next)
    applyTheme(next)
  }

  const safety = (() => {
    if (mode === 'sss') {
      if (config?.sss_dry_run) return { label: '模拟执行', tone: 'text-success' }
      if (config?.sss_preflight) return { label: '预检，不创建订单', tone: 'text-warning' }
      return { label: '正式下单', tone: 'text-destructive' }
    }
    if (mode === 'cloud') {
      return config?.wps_test_mode
        ? { label: '写入测试副本', tone: 'text-success' }
        : { label: '写入正式目标', tone: 'text-destructive' }
    }
    return { label: '读写排单表', tone: 'text-warning' }
  })()

  return (
    <header
      className="shrink-0 border-b bg-card/95 backdrop-blur"
      style={{ paddingTop: 'var(--safe-top)' }}
    >
      <div className="flex h-11 items-center gap-2 px-3 sm:px-4">
        <div className="flex h-6 w-6 shrink-0 items-center justify-center rounded-[4px] bg-primary font-serif text-[13px] font-bold text-primary-foreground">
          轻
        </div>
        <span className="shrink-0 whitespace-nowrap font-serif text-sm font-semibold tracking-[1px]">一口轻食</span>
        <Badge variant="outline" className="tabular shrink-0 rounded-[4px] font-mono text-[10px] font-normal">
          v{version || '…'}
        </Badge>
        {mocked && <span className="truncate text-[10px] text-warning">静态预览</span>}
        <div className="ml-auto shrink-0">
          <Button
            variant="ghost"
            size="sm"
            className="h-9 rounded-[6px] px-2 text-xs text-muted-foreground hover:text-foreground"
            onClick={toggleTheme}
            aria-label={theme === 'light' ? '切换到深色主题' : '切换到浅色主题'}
          >
            {theme === 'light' ? <Moon className="size-4" /> : <Sun className="size-4" />}
            <span className="hidden sm:inline">{theme === 'light' ? '深色' : '浅色'}</span>
          </Button>
        </div>
      </div>

      <div className="flex min-w-0 items-center gap-2 border-t/60 px-3 pb-1.5 text-[11px] text-muted-foreground sm:px-4">
        <span className="shrink-0 rounded-full border bg-secondary/60 px-2 py-0.5 text-foreground">
          {MODE_LABELS[mode]}
        </span>
        <span className="min-w-0 truncate">
          状态：
          <b className={cn('font-medium', operationView.tone === 'danger' ? 'text-destructive' : operationView.tone === 'warning' ? 'text-warning' : operationView.tone === 'success' ? 'text-success' : 'text-foreground')}>
            {operationView.label}
          </b>
          {operationView.detail ? ` · ${operationView.detail}` : ''}
        </span>
        <span className={cn('ml-auto shrink-0 whitespace-nowrap font-medium', safety.tone)}>
          安全模式：{safety.label}
        </span>
      </div>
    </header>
  )
}
