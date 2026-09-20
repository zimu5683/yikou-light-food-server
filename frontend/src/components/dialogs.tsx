/**
 * 对话框体系：决策/验证码/更新。
 * 决策与验证码只在服务端返回 ok 后才关闭；网络未知失败保留输入可重试，
 * 服务端权威返回 ok=false 视为交互已结束/过期并明确提示。
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
import { Callout } from '@/components/WorkspaceUI'
import { interactionRecoveryView } from '@/lib/interactionRecovery'
import { cn } from '@/lib/utils'
import { formatBytes, updateProgressText } from '@/lib/updateUi'

const CHOICE_STYLES: Record<string, string> = {
  primary: 'bg-primary text-primary-foreground hover:bg-primary-strong',
  neutral: 'border-border bg-card text-foreground hover:bg-secondary',
  danger: 'bg-destructive text-white hover:bg-destructive/90',
}

/** 决策弹窗：worker 阻塞等待用户选择；成功确认后才关闭。 */
export function DecisionDialog() {
  const { decision, resolveDecision, connection, recovery, operationActive } = useApp()
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  if (!decision) return null
  const current = decision
  const recoveryView = interactionRecoveryView({
    hasLocalRequest: true, hasPersistedMeta: false, connection, recovery, operationActive, hasServerReadApi: true,
  })

  async function choose(value: string) {
    if (busy) return
    setBusy(value)
    setError('')
    const result = await resolveDecision(current.id, value)
    if (!result.ok && result.retryable) setError(result.message || '提交失败，输入已保留，请重试')
    setBusy('')
  }

  return (
    <AlertDialog open onOpenChange={(open) => {
      if (!open && !busy) void choose('cancel')
    }}>
      <AlertDialogContent className="sm:max-w-md rounded-lg">
        <AlertDialogHeader>
          <AlertDialogTitle className="font-serif">{current.title}</AlertDialogTitle>
          <AlertDialogDescription className="whitespace-pre-wrap">
            {current.message}
          </AlertDialogDescription>
        </AlertDialogHeader>
        {recoveryView.visible && (
          <Callout tone="warning" title={recoveryView.title}>{recoveryView.detail}</Callout>
        )}
        {error && <p role="alert" className="rounded-md border border-destructive/40 bg-destructive/5 px-3 py-2 text-xs text-destructive">{error}</p>}
        <AlertDialogFooter className="flex-row justify-end gap-2">
          {current.choices.map((choice) => (
            <Button
              key={choice.value}
              className={cn('h-9 min-w-20 rounded-[6px] text-xs', CHOICE_STYLES[choice.style])}
              disabled={Boolean(busy)}
              onClick={() => void choose(choice.value)}
            >
              {busy === choice.value ? '提交中…' : choice.label}
            </Button>
          ))}
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  )
}

/** 闪时送图形验证码：服务端确认成功才关闭，失败保留已输入内容。 */
export function CaptchaDialog() {
  const { captcha, resolveCaptcha, connection, recovery, operationActive } = useApp()
  const [code, setCode] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  if (!captcha) return null
  const current = captcha
  const recoveryView = interactionRecoveryView({
    hasLocalRequest: true, hasPersistedMeta: false, connection, recovery, operationActive, hasServerReadApi: true,
  })

  async function submit(value: string) {
    if (busy) return
    setBusy(true)
    setError('')
    const result = await resolveCaptcha(current.id, value)
    if (!result.ok && result.retryable) {
      setError(result.message || '提交失败，验证码已保留，请重试')
      setBusy(false)
      return
    }
    setBusy(false)
  }

  return (
    <Dialog open onOpenChange={(open) => {
      if (!open && !busy) void submit('')
    }}>
      <DialogContent className="sm:max-w-sm rounded-lg">
        <DialogHeader>
          <DialogTitle className="font-serif">闪时送登录验证</DialogTitle>
          <DialogDescription>请输入图片中的验证码。验证码用于接口登录，不会弹出浏览器。</DialogDescription>
        </DialogHeader>
        <div className="flex flex-col items-center gap-3 py-2">
          <img src={`data:image/png;base64,${current.image}`} alt="验证码" className="h-24 rounded border border-border bg-card object-contain" />
          <input
            autoFocus
            value={code}
            disabled={busy}
            onChange={(e) => setCode(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Enter') void submit(code.trim()) }}
            placeholder="请输入验证码"
            className="h-11 w-full rounded-[6px] border border-border bg-secondary px-3 text-center font-mono text-base tracking-[0.3em] outline-none focus:border-primary focus:ring-2 focus:ring-primary/30"
          />
        </div>
        {recoveryView.visible && (
          <Callout tone="warning" title={recoveryView.title}>{recoveryView.detail}</Callout>
        )}
        {error && <p role="alert" className="rounded-md border border-destructive/40 bg-destructive/5 px-3 py-2 text-xs text-destructive">{error}</p>}
        <DialogFooter className="gap-2">
          <Button variant="ghost" className="h-9 text-xs" disabled={busy} onClick={() => void submit('')}>
            取消
          </Button>
          <Button className="h-9 rounded-[6px] text-xs" disabled={busy || code.trim().length < 4} onClick={() => void submit(code.trim())}>
            {busy ? '验证中…' : '确定'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

/** 发现新版本：APK 模式下自动下载、校验并拉起系统安装器。 */
export function UpdateAvailableDialog() {
  const { available, setAvailable } = useUpdateAvailable()
  const {
    installUpdate,
    cancelUpdate,
    openInstallSettings,
    updateProgress,
    updatePermissionRequired,
    updateError,
    canSelfUpdate,
  } = useApp()
  if (!available) return null
  const current = available
  const canInstall = current.can_install ?? canSelfUpdate
  const progress = updateProgress
  const downloading = progress?.phase === 'downloading'
  const installing = progress?.phase === 'installing'
  const percent = progress ? Math.max(0, Math.min(100, Math.round(progress.percent))) : 0

  function close() {
    if (downloading) cancelUpdate()
    setAvailable(null)
  }

  return (
    <Dialog open onOpenChange={(open) => { if (!open) close() }}>
      <DialogContent className="sm:max-w-md rounded-lg">
        <DialogHeader>
          <DialogTitle className="font-serif">发现新版本 {current.tag}</DialogTitle>
          <DialogDescription className="max-h-40 overflow-y-auto whitespace-pre-wrap">
            {current.body}
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-3">
          <div className="text-xs text-muted-foreground">
            当前版本 {current.current}
            {current.asset_name ? ` · 安装包 ${current.asset_name}` : ''}
            {current.size ? `（${formatBytes(current.size)}）` : ''}
          </div>

          {progress && (
            <div className="space-y-2">
              <div className="h-2 w-full overflow-hidden rounded-full bg-secondary">
                <div className="h-full rounded-full bg-primary transition-all" style={{ width: `${percent}%` }} />
              </div>
              <div className="text-xs text-muted-foreground">
                {updateProgressText(progress.phase, percent, progress.downloaded, progress.total, progress.message)}
              </div>
            </div>
          )}

          {!progress && !canInstall && (
            <div className="rounded-[6px] border border-border bg-secondary/60 p-3 text-xs text-muted-foreground">
              当前通过浏览器访问，无法自动安装 APK。请在手机上直接打开「一口轻食」App 更新。
            </div>
          )}

          {!progress && updatePermissionRequired && (
            <div className="rounded-[6px] border border-warning/50 bg-warning/10 p-3 text-xs text-foreground">
              需要先允许本应用安装「未知来源应用」。授权后回到 App，再点一次「下载并安装」即可。
            </div>
          )}

          {!progress && updateError && (
            <div className="rounded-[6px] border border-destructive/40 bg-destructive/5 p-3 text-xs text-destructive">
              {updateError}
            </div>
          )}
        </div>

        <DialogFooter className="gap-2">
          {progress ? (
            downloading ? (
              <Button variant="ghost" className="h-9 text-xs" onClick={close}>取消下载</Button>
            ) : (
              <Button variant="ghost" className="h-9 text-xs" onClick={() => setAvailable(null)}>
                {installing ? '关闭' : '知道了'}
              </Button>
            )
          ) : updatePermissionRequired ? (
            <>
              <Button variant="ghost" className="h-9 text-xs" onClick={close}>暂不更新</Button>
              <Button className="h-9 rounded-[6px] text-xs" onClick={openInstallSettings}>去允许安装</Button>
            </>
          ) : updateError ? (
            <>
              <Button variant="ghost" className="h-9 text-xs" onClick={close}>暂不更新</Button>
              <Button className="h-9 rounded-[6px] text-xs" onClick={installUpdate}>重试</Button>
            </>
          ) : canInstall ? (
            <>
              <Button variant="ghost" className="h-9 text-xs" onClick={close}>暂不更新</Button>
              <Button className="h-9 rounded-[6px] text-xs" onClick={installUpdate}>下载并安装</Button>
            </>
          ) : (
            <Button variant="ghost" className="h-9 text-xs" onClick={() => setAvailable(null)}>知道了</Button>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
