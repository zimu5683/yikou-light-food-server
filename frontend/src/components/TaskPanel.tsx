/**
 * 任务面板：模式切换（下划线 tab）+ 订单处理 / 闪时送下单 表单 + 主操作条 + 更多菜单。
 * 校验结果由桥接层返回（start_order/start_sss 的 fields），前端渲染字段错误态。
 */
import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react'
import { MoreHorizontal, ScrollText } from 'lucide-react'
import { toast } from 'sonner'
import { Button } from '@/components/ui/button'
import { CloudForm } from '@/components/CloudForm'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Switch } from '@/components/ui/switch'
import { DateField, Field, GhostButton, Stepper, TextInput } from '@/components/fields'
import { FileBrowserDialog } from '@/components/FileBrowserDialog'
import { statusLabel, useApp, type FieldErrors, type TaskMode } from '@/hooks/appContext'
import {
  api,
  isApiReady,
  isWebTransport,
  type OrderFormPayload,
  type SssFormPayload,
} from '@/lib/bridge'
import type { StatusState } from '@/lib/bridge'
import type { LogSheetDrag } from '@/lib/useLogSheetDrag'
import { cn } from '@/lib/utils'
import { modeError } from '@/lib/format'

export function TaskPanel({ logToggle }: { logToggle?: LogToggleProps }) {
  const { mode, setMode, workerAlive, config } = useApp()
  const formKey = config ? 'ready' : 'loading'
  return (
    // 分栏边框由 App 外壳负责（桌面才画），这里再画一次会变成 2px
    <section className="flex min-h-0 flex-1 flex-col border-border">
      {/* 标题与模式 tab 固定不滚：手机上它们是导航，滚走就找不回来了。
          副标题是装饰性文案，窄屏收起，把高度让给表单。 */}
      <div className="shrink-0 px-3 pt-4 sm:px-5">
        <h1 className="font-serif text-lg font-semibold tracking-[1px]">任务配置</h1>
        <p className="mb-3.5 mt-0.5 hidden text-xs text-muted-foreground sm:block">
          选择任务类型，准备好资料后启动。
        </p>

        {/* 手机上三个 tab 均分整行（触控目标更大），桌面回到左对齐的紧凑排布 */}
        <div role="tablist" className="mb-3 flex gap-2 border-b sm:gap-[18px]">
          <ModeTab active={mode === 'order'} onClick={() => setMode('order')}>
            订单处理
          </ModeTab>
          <ModeTab active={mode === 'cloud'} onClick={() => setMode('cloud')}>
            云文档同步
          </ModeTab>
          <ModeTab active={mode === 'sss'} onClick={() => setMode('sss')}>
            闪时送下单
          </ModeTab>
        </div>
      </div>

      {/* 三个表单常驻渲染（仅切换可见性）：卸载会清空各字段的 useState，
          导致切页签后已输入内容丢失并被旧 config 重新填充。
          每个表单自己管「字段区滚动 + 底部操作条」，所以这里必须是能撑满的
          flex 列容器（不能是 block）。 */}
      <div className={cn('min-h-0 flex-1 flex-col', mode === 'order' ? 'flex' : 'hidden')}>
        <OrderForm key={formKey} logToggle={logToggle} />
      </div>
      <div className={cn('min-h-0 flex-1 flex-col', mode === 'cloud' ? 'flex' : 'hidden')}>
        <CloudForm key={formKey} />
      </div>
      <div className={cn('min-h-0 flex-1 flex-col', mode === 'sss' ? 'flex' : 'hidden')}>
        <SssForm key={formKey} logToggle={logToggle} />
      </div>
      {workerAlive && (
        <p className="shrink-0 border-t px-3 py-1.5 text-[11px] text-muted-foreground sm:px-5">
          任务运行中，开始与表单暂不可用。
        </p>
      )}
    </section>
  )
}

function ModeTab({
  active,
  onClick,
  children,
}: {
  active: boolean
  onClick: () => void
  children: ReactNode
}) {
  return (
    <button
      role="tab"
      aria-selected={active}
      onClick={onClick}
      className={cn(
        // 手机：均分整行 + ≥44px 触控高度；桌面：保持规范里的紧凑下划线 tab
        'relative flex min-h-10 flex-1 items-center justify-center pb-2 pt-1.5 text-[13.5px] font-medium transition-colors',
        'sm:min-h-0 sm:flex-none sm:justify-start',
        active ? 'font-semibold text-foreground' : 'text-muted-foreground hover:text-foreground',
        active &&
          "after:absolute after:inset-x-0 after:-bottom-px after:h-0.5 after:bg-primary after:content-['']",
      )}
    >
      {children}
    </button>
  )
}

/* ------------------------------------------------------------------ */
/* 订单处理                                                             */
/* ------------------------------------------------------------------ */

function OrderForm({ logToggle }: { logToggle?: LogToggleProps }) {
  const { config, passwords, startOrder, workerAlive, isAdmin } = useApp()
  const [url, setUrl] = useState(config?.target_url ?? '')
  const [phone, setPhone] = useState(config?.phone_number ?? '')
  const [password, setPassword] = useState(passwords.order ?? '')
  const [excel, setExcel] = useState(config?.excel_path ?? '')
  const [date, setDate] = useState(config?.order_date ?? '')
  const [count, setCount] = useState<number | null>(config?.order_count ?? null)
  const [remember, setRemember] = useState(true)
  const [fields, setFields] = useState<FieldErrors | null>(null)
  const [busy, setBusy] = useState(false)
  // 网页版的服务端文件浏览器：null 表示未打开。
  const [browser, setBrowser] = useState<'open' | 'save' | null>(null)

  // 字段停止变化后自动落盘（切页签/退出重进都从后端还原，配置不丢失）。
  const scheduleSave = useDebouncedSave(() => {
    if (!isApiReady()) return
    api()
      .save_order_config({ url, phone, excel, date, count })
      .catch(() => {})
  })
  const firstSave = useRef(true)
  useEffect(() => {
    if (firstSave.current) {
      firstSave.current = false
      return
    }
    scheduleSave()
  }, [url, phone, excel, date, count, scheduleSave])

  const excelError = modeError(fields, 'excel')
  const excelOk = !excelError && excel && !fields ? '文件已准备' : undefined

  async function onStart() {
    if (busy) return
    setBusy(true)
    setFields(null)
    try {
      // 只提交当前表单字段；桌面/浏览器时代的兼容字段已经删除。
      const payload: OrderFormPayload = { url, phone, password, excel, date, count: count === null ? '' : String(count), remember }
      const errors = await startOrder(payload)
      if (errors) setFields(errors)
    } finally {
      setBusy(false)
    }
  }

  function applyExcelResult(result: { path: string; error: string }) {
    if (result.path) setExcel(result.path)
    if (result.error) setFields((prev) => ({ ...prev, excel: { message: result.error } }))
  }

  async function chooseFile() {
    if (!isApiReady()) return
    // 网页版走服务端文件浏览器：任务在手机上跑，Excel 也在手机上，
    // 浏览器自己的文件选择器只能拿到客户端文件，用不上。
    if (isWebTransport()) {
      setBrowser('open')
      return
    }
    applyExcelResult(await api().choose_excel('order'))
  }

  async function newTemplate() {
    if (!isApiReady()) return
    if (isWebTransport()) {
      setBrowser('save')
      return
    }
    applyExcelResult(await api().new_template('order'))
  }

  /** 文件浏览器选中路径后回填，并让后端按同一套规则校验/落盘。 */
  async function onBrowserPick(path: string) {
    const picked = browser
    setBrowser(null)
    if (picked === 'save') {
      applyExcelResult(await api().new_template('order', path))
      return
    }
    applyExcelResult(await api().choose_excel('order', path))
  }

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      {/* 字段区自己滚动，操作条是它的兄弟节点（真页脚）。
          原来操作条用 sticky bottom-0 待在滚动区内部：内容不足一屏时它不会被
          撑到底部，下方就露出滚动容器的空白（「更多」下面那块空缺）；滚动时
          表单内容又会从它后面滑过。做成页脚后这两种情况都不存在。 */}
      <div className="scroll-contain min-h-0 flex-1 overflow-y-auto px-3 pb-4 pt-1 sm:px-5">
        {isAdmin && (
          <>
        <Field label="管理网址" htmlFor="order-url" error={modeError(fields, 'url')} helper="用于登录管理后台">
          <TextInput
            id="order-url"
            value={url}
            onChange={(e) => setUrl(e.target.value)}
            placeholder="https://example.com/admin"
          />
        </Field>

        <Field label="手机号 / 账号" htmlFor="order-phone" error={modeError(fields, 'phone')} helper="用于登录管理后台">
          <TextInput
            id="order-phone"
            value={phone}
            onChange={(e) => setPhone(e.target.value)}
          />
        </Field>

        <Field label="登录密码" htmlFor="order-password" error={modeError(fields, 'password')} helper="密码仅保存在系统凭据管理器中">
          <TextInput
            id="order-password"
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
        </Field>

        <Field label="Excel 文件" htmlFor="order-excel" error={excelError} okMessage={excelOk} helper="支持 .xlsx / .xlsm">
          <div className="flex gap-1.5">
            <TextInput
              id="order-excel"
              value={excel}
              onChange={(e) => setExcel(e.target.value)}
              state={excel && !excelError ? 'valid' : undefined}
              className="min-w-0 flex-1"
              placeholder="选择排单 .xlsx 文件"
            />
            <GhostButton onClick={chooseFile}>选择文件</GhostButton>
            <GhostButton onClick={newTemplate}>新建模板</GhostButton>
          </div>
        </Field>

          </>
        )}
        {/* 非管理员：凭据与文件路径一律不显示 —— 任务按管理员预设的账号运行。
            真正的拦截在后端（配置脱敏 + 方法白名单），这里只是不显示。 */}
        {!isAdmin && (
          <p className="mb-4 mt-1 rounded-[4px] border border-border bg-card px-3 py-2 text-[12px] text-muted-foreground">
            本账号按管理员预设的账号与文件运行，无需填写管理网址/手机号/密码/Excel。
          </p>
        )}
        <Field label="目标日期" error={modeError(fields, 'date')} helper="留空默认今天；只允许选择今天或过去日期">
          <DateField value={date} onChange={setDate} invalid={Boolean(modeError(fields, 'date'))} />
        </Field>

        <Field label="待处理订单数" error={modeError(fields, 'count')}>
          <Stepper value={count} onChange={setCount} invalid={Boolean(modeError(fields, 'count'))} />
          <p className="mt-1 text-[11px] text-muted-foreground">留空=全部订单</p>
        </Field>

        <div className="mb-4 mt-1 flex items-center gap-2 text-[12.5px] text-muted-foreground">
          <Switch checked={remember} onCheckedChange={setRemember} aria-label="保存到系统凭据管理器" />
          <span>保存到系统凭据管理器</span>
        </div>
        </div>

      <BottomDock>
        <ActionBar
          startLabel="开始处理"
          onStart={onStart}
          startBusy={busy}
          startDisabled={workerAlive}
          logToggle={logToggle}
        />
        <ToolsMenu mode="order" />
      </BottomDock>
      {browser && (
        <FileBrowserDialog
          open
          mode={browser}
          initialPath={excel}
          onCancel={() => setBrowser(null)}
          onPick={(path) => void onBrowserPick(path)}
        />
      )}
    </div>
  )
}

/* ------------------------------------------------------------------ */
/* 闪时送下单                                                           */
/* ------------------------------------------------------------------ */

function SssForm({ logToggle }: { logToggle?: LogToggleProps }) {
  const { config, passwords, startSss, workerAlive, isAdmin } = useApp()
  const [url, setUrl] = useState(config?.sss_url ?? '')
  const [account, setAccount] = useState(config?.sss_account ?? '')
  const [password, setPassword] = useState(passwords.sss ?? '')
  const [excel, setExcel] = useState(config?.sss_excel_path ?? '')
  const [orderSource, setOrderSource] = useState<'wps' | 'excel'>(config?.sss_order_source ?? 'wps')
  const [productName, setProductName] = useState(config?.sss_product_name ?? '轻食')
  // 固定地址配置当前不在界面中编辑，直接由 config 派生，避免未使用 setter。
  const commonAddress = config?.sss_common_address ?? ''
  const useFixedAddress = config?.sss_use_fixed_address ?? true
  const fixedLnt = String(config?.sss_fixed_lnt ?? '119.728224')
  const fixedLat = String(config?.sss_fixed_lat ?? '30.256632')
  const fixedAreaCode = config?.sss_fixed_area_code ?? '330110'
  const fixedAddressDetail = config?.sss_fixed_address_detail ?? '浙江农林大学东湖校区'
  const [remember, setRemember] = useState(true)
  const [dryRun, setDryRun] = useState(config?.sss_dry_run ?? true)
  const [preflight, setPreflight] = useState(config?.sss_preflight ?? false)
  const [fields, setFields] = useState<FieldErrors | null>(null)
  const [busy, setBusy] = useState(false)
  const [dayBusy, setDayBusy] = useState(false)
  // 网页版的服务端文件浏览器：null 表示未打开。
  const [browser, setBrowser] = useState<'open' | 'save' | null>(null)

  // 字段停止变化后自动落盘（切页签/退出重进都从后端还原，配置不丢失）。
  const scheduleSave = useDebouncedSave(() => {
    if (!isApiReady()) return
    api()
      .save_sss_config({
        url,
        account,
        excel,
        order_source: orderSource,
        product_name: productName,
        common_address: commonAddress,
        use_fixed_address: useFixedAddress,
        fixed_lnt: fixedLnt,
        fixed_lat: fixedLat,
        fixed_area_code: fixedAreaCode,
        fixed_address_detail: fixedAddressDetail,
        dry_run: dryRun,
        preflight,
      })
      .catch(() => {})
  })
  const firstSave = useRef(true)
  useEffect(() => {
    if (firstSave.current) {
      firstSave.current = false
      return
    }
    scheduleSave()
  }, [
    url, account, excel, productName, commonAddress, useFixedAddress,
    fixedLnt, fixedLat, fixedAreaCode, fixedAddressDetail, dryRun, preflight,
    orderSource,
    scheduleSave,
  ])

  const excelError = modeError(fields, 'excel')
  const excelOk = !excelError && excel && !fields ? '文件已准备' : undefined

  async function onStart() {
    if (busy) return
    setBusy(true)
    setFields(null)
    try {
      const payload: SssFormPayload = {
        url,
        account,
        password,
        excel,
        order_source: orderSource,
        product_name: productName,
        common_address: commonAddress,
        use_fixed_address: useFixedAddress,
        fixed_lnt: fixedLnt,
        fixed_lat: fixedLat,
        fixed_area_code: fixedAreaCode,
        fixed_address_detail: fixedAddressDetail,
        remember,
        dry_run: dryRun,
        preflight,
      }
      const errors = await startSss(payload)
      if (errors) setFields(errors)
    } finally {
      setBusy(false)
    }
  }

  function applyExcelResult(result: { path: string; error: string }) {
    if (result.path) setExcel(result.path)
    if (result.error) setFields((prev) => ({ ...prev, excel: { message: result.error } }))
  }

  async function chooseFile() {
    if (!isApiReady()) return
    // 网页版走服务端文件浏览器（同订单处理页签：选的是手机上的路径）。
    if (isWebTransport()) {
      setBrowser('open')
      return
    }
    applyExcelResult(await api().choose_excel('sss'))
  }

  async function newTemplate() {
    if (!isApiReady()) return
    if (isWebTransport()) {
      setBrowser('save')
      return
    }
    applyExcelResult(await api().new_template('sss'))
  }

  /** 文件浏览器选中路径后回填，并让后端按同一套规则校验/落盘。 */
  async function onBrowserPick(path: string) {
    const picked = browser
    setBrowser(null)
    if (picked === 'save') {
      applyExcelResult(await api().new_template('sss', path))
      return
    }
    applyExcelResult(await api().choose_excel('sss', path))
  }

  /** 读取云端当天名单（东湖午餐/东湖晚餐）并留档，不下单。 */
  async function readDayOrders() {
    if (dayBusy) return
    setDayBusy(true)
    try {
      const result = await api().sss_day_orders()
      if (!result.ok) {
        toast.error(result.reason ?? '读取云端当天名单失败', { duration: 8000 })
        return
      }
      const parts = Object.entries(result.meals ?? {}).map(([name, info]) =>
        info.skipped
          ? `${name}不下单（${info.reason || '没有当天列'}）`
          : `${name} ${info.orders} 人（标 1 共 ${info.marked}，大西/小 ${info.skipped_address} 人不送）`,
      )
      toast.success(
        `云端当天名单 ${result.target_date ?? ''} ${result.date_text ?? ''}：${parts.join('；') || '没有数据'}`,
        { duration: 8000 },
      )
      if (result.archive_error) {
        toast.error(`留档 Excel 写入失败：${result.archive_error}`, { duration: 8000 })
      }
    } catch (error) {
      toast.error(`读取失败：${String(error)}`)
    } finally {
      setDayBusy(false)
    }
  }

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      {/* 与订单处理页签同构：字段区滚动 + 操作条做真页脚（不再 sticky） */}
      <div className="scroll-contain min-h-0 flex-1 overflow-y-auto px-3 pb-4 pt-1 sm:px-5">
        {isAdmin && (
          <>
        <Field label="闪时送网址" htmlFor="sss-url" error={modeError(fields, 'url')} helper="闪时送下单平台地址">
          <TextInput id="sss-url" value={url} onChange={(e) => setUrl(e.target.value)} />
        </Field>

        <Field label="闪时送账号" htmlFor="sss-account" error={modeError(fields, 'account')} helper="用于登录闪时送平台">
          <TextInput id="sss-account" value={account} onChange={(e) => setAccount(e.target.value)} />
        </Field>

        <Field label="登录密码" htmlFor="sss-password" error={modeError(fields, 'password')} helper="密码仅保存在系统凭据管理器中">
          <TextInput
            id="sss-password"
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
        </Field>

          </>
        )}
        {!isAdmin && (
          <p className="mb-4 mt-1 rounded-[4px] border border-border bg-card px-3 py-2 text-[12px] text-muted-foreground">
            本账号按管理员预设的闪时送账号与文件运行，无需填写网址/账号/密码。
          </p>
        )}
        <Field
          label="名单来源"
          helper={
            orderSource === 'wps'
              ? '每次下单前读取东湖午餐/东湖晚餐「当天列标 1」的人；地址是大西/小的不下单'
              : '读取《闪时送.xlsx》里的名单（人工准备），不做云端读取'
          }
        >
          <div className="flex gap-1.5" role="group" aria-label="名单来源">
            <SourceButton
              active={orderSource === 'wps'}
              onClick={() => setOrderSource('wps')}
            >
              云端当天名单
            </SourceButton>
            <SourceButton
              active={orderSource === 'excel'}
              onClick={() => setOrderSource('excel')}
            >
              本地 Excel
            </SourceButton>
          </div>
        </Field>

        <Field
          label="订单 Excel 文件"
          htmlFor="sss-excel"
          error={excelError}
          okMessage={excelOk}
          helper={
            orderSource === 'wps'
              ? '云端模式：作为当天名单的留档文件，可留空'
              : '午餐/晚餐两表，A=姓名 B=门牌号 C=电话'
          }
        >
          <div className="flex gap-1.5">
            <TextInput
              id="sss-excel"
              value={excel}
              onChange={(e) => setExcel(e.target.value)}
              state={excel && !excelError ? 'valid' : undefined}
              className="min-w-0 flex-1"
              placeholder="选择闪时送 .xlsx 文件"
            />
            <GhostButton onClick={chooseFile}>选择文件</GhostButton>
            <GhostButton onClick={newTemplate}>新建模板</GhostButton>
          </div>
        </Field>

        {orderSource === 'wps' && (
          <div className="mb-4 -mt-1 flex items-center gap-2">
            <GhostButton onClick={readDayOrders} disabled={dayBusy || workerAlive}>
              {dayBusy ? '读取中…' : '读取云端当天名单'}
            </GhostButton>
            <span className="text-[11px] text-muted-foreground">
              只读取并写留档，不下单
            </span>
          </div>
        )}

        <Field label="商品名称" htmlFor="sss-product" helper="下单时商品“名称”的默认值">
          <TextInput
            id="sss-product"
            value={productName}
            onChange={(e) => setProductName(e.target.value)}
          />
        </Field>

        <div className="mb-4 mt-1 flex items-center gap-2 text-[12.5px] text-muted-foreground">
          <Switch checked={remember} onCheckedChange={setRemember} aria-label="保存到系统凭据管理器" />
          <span>保存到系统凭据管理器</span>
        </div>

        <div className="mb-4 mt-1 flex items-center gap-2 text-[12.5px] text-muted-foreground">
          <Switch checked={dryRun} onCheckedChange={setDryRun} aria-label="干跑：只预览报文，不创建订单" />
          <span>干跑：只预览报文，不创建订单</span>
        </div>

        <div className="mb-4 mt-1 flex items-center gap-2 text-[12.5px] text-muted-foreground">
          <Switch
            checked={preflight}
            onCheckedChange={setPreflight}
            aria-label="预检：登录并检查，不创建订单"
          />
          <span>预检：登录并检查余额/订单，不创建订单</span>
        </div>
        </div>

      <BottomDock>
        <ActionBar
          startLabel="开始下单"
          onStart={onStart}
          startBusy={busy}
          startDisabled={workerAlive}
          logToggle={logToggle}
        />
        <ToolsMenu mode="sss" />
      </BottomDock>
      {browser && (
        <FileBrowserDialog
          open
          mode={browser}
          initialPath={excel}
          onCancel={() => setBrowser(null)}
          onPick={(path) => void onBrowserPick(path)}
        />
      )}
    </div>
  )
}

/* ------------------------------------------------------------------ */
/* 共享：主操作条 + 工具菜单                                              */
/* ------------------------------------------------------------------ */

/** 名单来源分段按钮（云端当天名单 / 本地 Excel） */
function SourceButton({
  active,
  onClick,
  children,
}: {
  active: boolean
  onClick: () => void
  children: ReactNode
}) {
  return (
    <button
      type="button"
      aria-pressed={active}
      onClick={onClick}
      className={cn(
        'h-[38px] flex-1 rounded-[4px] border px-3 text-xs transition-colors sm:h-[34px]',
        active
          ? 'border-primary bg-secondary font-medium text-primary-strong'
          : 'border-border bg-card text-muted-foreground hover:border-primary hover:text-primary-strong',
      )}
    >
      {children}
    </button>
  )
}

/** 表单底部停靠坞：更多菜单 + 主操作条整体吸底 */
/**
 * 表单底部操作条 —— **非滚动的真页脚**。
 *
 * 它原本是滚动容器内部的 `sticky bottom-0`：内容不足一屏时不会被撑到底部，
 * 「更多」下方就露出滚动容器的空白，而滚动时表单内容又会从它后面滑过。
 * 现在它是滚动区的兄弟节点，永远贴住面板底部，也不再需要负边距对齐
 * （横向内边距由自己给）。底部安全区不在这里加：手机最底部是 tab 栏。
 */
function BottomDock({ children }: { children: ReactNode }) {
  return (
    <div className="shrink-0 border-t bg-background px-3 pb-3 pt-2.5 sm:px-5">
      {children}
    </div>
  )
}

function ActionBar({
  startLabel,
  onStart,
  startBusy,
  startDisabled,
  logToggle,
}: {
  startLabel: string
  onStart: () => void
  startBusy: boolean
  startDisabled: boolean
  /** 手机端把「日志」按钮并进操作栏（原来在最底部的 tab 栏里，已移除）。 */
  logToggle?: LogToggleProps
}) {
  const { stopTask, workerAlive } = useApp()
  const [confirming, setConfirming] = useState(false)

  return (
    <>
      {/* 顶边与上间距由 BottomDock 提供，这里不再重复画 border-t */}
      <div className="flex gap-2">
        {logToggle && <LogToggleButton {...logToggle} />}
        <Button
          className="btn-serif-primary h-[38px] flex-1 rounded-[6px] text-sm"
          disabled={startDisabled || startBusy}
          onClick={onStart}
        >
          {startBusy ? '校验中…' : startLabel}
        </Button>
        <Button
          variant="outline"
          className="h-[38px] w-24 rounded-[6px] border-destructive/45 bg-card text-[13px] text-destructive hover:bg-destructive/5 hover:text-destructive"
          disabled={!workerAlive}
          onClick={() => setConfirming(true)}
        >
          停止
        </Button>
      </div>
      <ConfirmStopDialog open={confirming} onOpenChange={setConfirming} onConfirm={stopTask} />
    </>
  )
}

/**
 * 手机端操作栏里的「日志」按钮。
 *
 * 双重身份：
 * - **点** → 开合日志抽屉；
 * - **拖** → 直接调抽屉高度（不用先打开再去找把手）。
 *
 * 两个关键细节：
 * 1. 必须 `touch-none`。否则手机上竖向拖动会被浏览器当成页面滚动，
 *    随即发出 `pointercancel` 把拖动打断 —— 表现就是「拖不动」。
 * 2. 拖动/点击用位移阈值区分（见 isClick）：拖过就不当成点击，避免松手误切换。
 */
function LogToggleButton({ open, status, running, drag }: LogToggleProps) {
  const pressed = useRef(false)

  return (
    <button
      type="button"
      aria-label={open ? '收起日志' : '展开日志'}
      aria-pressed={open}
      onPointerDown={(e) => {
        pressed.current = true
        // 指针捕获在 hook 内部完成（手指移出按钮范围也要继续收到 move/up）
        drag.onPointerDown(e)
      }}
      onPointerMove={(e) => {
        if (pressed.current) drag.onPointerMove(e)
      }}
      onPointerUp={() => {
        if (!pressed.current) return
        pressed.current = false
        // 开合与「是否算点击」都由 hook 统一判定，避免两处各判一次而不一致
        drag.onPointerUp()
      }}
      onPointerCancel={() => {
        if (!pressed.current) return
        pressed.current = false
        drag.onPointerUp()
      }}
      className={cn(
        'flex h-[38px] w-[58px] shrink-0 touch-none select-none flex-col items-center justify-center',
        'rounded-[6px] border bg-card text-[10px] transition-colors',
        open ? 'border-primary/60 text-primary' : 'border-border text-muted-foreground',
      )}
    >
      <span className="flex items-center gap-1">
        {running && <span className="led-breathe size-[5px] rounded-[1px] bg-primary" />}
        <ScrollText className="size-3.5" />
      </span>
      <span className="leading-tight">{running ? statusLabel(status) : '日志'}</span>
    </button>
  )
}

/** 操作栏日志按钮需要的 props（由 App 透传下来）。 */
export interface LogToggleProps {
  open: boolean
  status: StatusState
  running: boolean
  /** 抽屉的拖拽状态机（由 App 的 useLogSheetDrag 提供）。 */
  drag: LogSheetDrag
}

function ToolsMenu({ mode }: { mode: TaskMode }) {
  const { clearPassword, checkUpdates, isAdmin } = useApp()
  const [confirmClear, setConfirmClear] = useState(false)

  // 清密码与检查更新都需要管理员权限（后端也会拦），普通用户直接不显示入口。
  if (!isAdmin) return null

  return (
    <>
      <nav className="mt-3 flex items-center text-xs text-muted-foreground">
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <button
              className="flex items-center gap-1 rounded px-2 py-1 hover:bg-secondary hover:text-foreground"
              aria-label="更多工具"
            >
              <MoreHorizontal className="size-4" />
              更多
            </button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="start" className="rounded-md text-xs">
            <DropdownMenuItem onClick={() => setConfirmClear(true)}>清除密码</DropdownMenuItem>
            <DropdownMenuItem onClick={() => checkUpdates(true)}>检查更新</DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </nav>
      <ConfirmClearPassword
        open={confirmClear}
        onOpenChange={setConfirmClear}
        onConfirm={() => clearPassword(mode === 'sss' ? 'sss' : 'order')}
      />
    </>
  )
}

/* 停止确认（对齐旧版 askyesnocancel 语义：是=停止 / 否=继续 / 取消=返回） */
function ConfirmStopDialog({
  open,
  onOpenChange,
  onConfirm,
}: {
  open: boolean
  onOpenChange: (v: boolean) => void
  onConfirm: () => void
}) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-sm rounded-lg">
        <DialogHeader>
          <DialogTitle className="font-serif">暂停处理</DialogTitle>
          <DialogDescription>是否停止当前任务？停止后需等待浏览器操作结束。</DialogDescription>
        </DialogHeader>
        <DialogFooter className="gap-2">
          <Button variant="ghost" className="h-8 text-xs" onClick={() => onOpenChange(false)}>
            取消
          </Button>
          <Button
            variant="outline"
            className="h-8 rounded-[6px] border-border bg-card text-xs text-foreground hover:bg-secondary"
            onClick={() => {
              onOpenChange(false)
            }}
          >
            继续处理
          </Button>
          <Button
            className="h-8 rounded-[6px] bg-destructive text-xs text-white hover:bg-destructive/90"
            onClick={() => {
              onOpenChange(false)
              onConfirm()
            }}
          >
            停止任务
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

function ConfirmClearPassword({
  open,
  onOpenChange,
  onConfirm,
}: {
  open: boolean
  onOpenChange: (v: boolean) => void
  onConfirm: () => void
}) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-sm rounded-lg">
        <DialogHeader>
          <DialogTitle className="font-serif">清除密码</DialogTitle>
          <DialogDescription>
            将从系统凭据管理器删除本机保存的密码，输入框也会清空。继续吗？
          </DialogDescription>
        </DialogHeader>
        <DialogFooter className="gap-2">
          <Button variant="ghost" className="h-8 text-xs" onClick={() => onOpenChange(false)}>
            取消
          </Button>
          <Button
            className="h-8 rounded-[6px] text-xs"
            onClick={() => {
              onOpenChange(false)
              onConfirm()
            }}
          >
            清除
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

/* ------------------------------------------------------------------ */
/* 防抖自动保存：字段停止变化 delay ms 后把表单值持久化到后端配置。          */
/* 只在 isApiReady（真实桌面端）且已从 config 完成首次填充后触发，避免     */
/* 初始化瞬间把默认值写回配置，也避免浏览器 mock 态空跑。                  */
/* ------------------------------------------------------------------ */
function useDebouncedSave(save: () => void, delay = 500): () => void {
  const timer = useRef<number | undefined>(undefined)
  const saveRef = useRef(save)
  useEffect(() => {
    saveRef.current = save
  }, [save])
  const cancel = useCallback(() => {
    if (timer.current !== undefined) {
      clearTimeout(timer.current)
      timer.current = undefined
    }
  }, [])
  const trigger = useCallback(() => {
    cancel()
    timer.current = window.setTimeout(() => {
      timer.current = undefined
      saveRef.current()
    }, delay)
  }, [cancel, delay])
  useEffect(() => cancel, [cancel])
  return trigger
}
