/**
 * 对话框体系：把旧版 15 种 messagebox/filedialog 映射为 Dialog/Toast。
 * - 决策弹窗（decision 事件）：订单定位失败/下单失败 retry-skip-stop、
 *   Excel 占用 retry-cancel、关闭保护 stop_and_close-keep-cancel
 * - 更新流程：发现新版本，打开 Release 页面（网页版不做自动安装）
 */
import { useState } from 'react'
import {
  AlertDialog,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '@/components/ui/alert-dialog'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { useApp, useUpdateAvailable } from '@/hooks/appContext'
import { cn } from '@/lib/utils'

const CHOICE_STYLES: Record<string, string> = {
  primary: 'bg-primary text-primary-foreground hover:bg-primary-strong',
  neutral: 'border-border bg-card text-foreground hover:bg-secondary',
  danger: 'bg-destructive text-white hover:bg-destructive/90',
}

/** 决策弹窗：worker 阻塞等待用户选择（对应旧 askyesnocancel/askretrycancel） */
export function DecisionDialog() {
  const { decision, resolveDecision } = useApp()
  if (!decision) return null
  return (
    <AlertDialog open onOpenChange={(open) => { if (!open) resolveDecision(decision.id, 'cancel') }}>
      <AlertDialogContent className="sm:max-w-md rounded-lg">
        <AlertDialogHeader>
          <AlertDialogTitle className="font-serif">{decision.title}</AlertDialogTitle>
          <AlertDialogDescription className="whitespace-pre-wrap">
            {decision.message}
          </AlertDialogDescription>
        </AlertDialogHeader>
        <AlertDialogFooter className="flex-row justify-end gap-2">
          {decision.choices.map((choice) => (
            <Button
              key={choice.value}
              className={cn('h-8 min-w-20 rounded-[6px] text-xs', CHOICE_STYLES[choice.style])}
              onClick={() => resolveDecision(decision.id, choice.value)}
            >
              {choice.label}
            </Button>
          ))}
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  )
}

/** 闪时送图形验证码（纯接口模式）：在应用内显示验证码图片，不启动浏览器 */
export function CaptchaDialog() {
  const { captcha, resolveCaptcha } = useApp()
  const [code, setCode] = useState('')
  if (!captcha) return null
  const current = captcha

  function submit() {
    resolveCaptcha(current.id, code.trim())
    setCode('')
  }

  function cancel() {
    resolveCaptcha(current.id, '')
    setCode('')
  }

  return (
    <Dialog open onOpenChange={(open) => { if (!open) cancel() }}>
      <DialogContent className="sm:max-w-sm rounded-lg">
        <DialogHeader>
          <DialogTitle className="font-serif">闪时送登录验证</DialogTitle>
          <DialogDescription>
            请输入图片中的验证码。验证码用于纯接口登录，不会弹出浏览器。
          </DialogDescription>
        </DialogHeader>
        <div className="flex flex-col items-center gap-3 py-2">
          <img
            src={`data:image/png;base64,${current.image}`}
            alt="验证码"
            className="h-24 rounded border border-border bg-card object-contain"
          />
          <input
            autoFocus
            value={code}
            onChange={(e) => setCode(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Enter') submit() }}
            placeholder="请输入验证码"
            className="h-9 w-full rounded-[4px] border border-border bg-secondary px-3 text-center font-mono text-base tracking-[0.3em] outline-none focus:border-primary focus:ring-2 focus:ring-primary/30"
          />
        </div>
        <DialogFooter className="gap-2">
          <Button variant="ghost" className="h-8 text-xs" onClick={cancel}>
            取消
          </Button>
          <Button
            className="h-8 rounded-[6px] text-xs"
            onClick={submit}
            disabled={code.trim().length < 4}
          >
            确定
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

/** 发现新版本：网页版只提示并打开 Release 页面，不做自动安装。 */
export function UpdateAvailableDialog() {
  const { available, setAvailable } = useUpdateAvailable()
  const { openExternal } = useApp()
  if (!available) return null

  function onOpenPage() {
    if (!available) return
    const url = available.html_url
      || `https://github.com/zimu5683/yikou-light-food-server/releases/tag/${available.tag}`
    openExternal(url)
    setAvailable(null)
  }

  return (
    <Dialog open onOpenChange={(open) => { if (!open) setAvailable(null) }}>
      <DialogContent className="sm:max-w-md rounded-lg">
        <DialogHeader>
          <DialogTitle className="font-serif">发现新版本 {available.tag}</DialogTitle>
          <DialogDescription className="max-h-40 overflow-y-auto whitespace-pre-wrap">
            {available.body}
          </DialogDescription>
        </DialogHeader>
        <DialogFooter className="gap-2">
          <Button
            variant="ghost"
            className="h-8 text-xs"
            onClick={() => setAvailable(null)}
          >
            暂不更新
          </Button>
          <Button className="h-8 rounded-[6px] text-xs" onClick={onOpenPage}>
            打开 Release 页面
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
