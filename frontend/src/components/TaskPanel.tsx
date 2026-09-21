/**
 * 任务工作台：三个语义页签 + 订单处理 / 云文档同步 / 闪时送表单。
 *
 * 本版重设计重点：
 * - tab 有 aria-controls/aria-labelledby 与方向键；
 * - 默认只展开“本次必要字段”，网址/凭据/路径等高级配置折叠；
 * - 主流程准备→校验/预览→确认→执行→结果，不伪造后台不存在的步骤/百分比；
 * - 固定底栏主动作 + 权威状态；真实下单与模拟执行都用明确文字二次确认；
 * - 密码清除只在服务端成功后同步清空表单草稿。
 */
import { useCallback, useEffect, useRef, useState, type KeyboardEvent, type ReactNode } from 'react'
import { MoreHorizontal } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { CloudForm } from '@/components/CloudForm'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { Switch } from '@/components/ui/switch'
import { DateField, Field, GhostButton, Stepper, TextInput } from '@/components/fields'
import { FileBrowserDialog } from '@/components/FileBrowserDialog'
import {
  AdvancedSection,
  Callout,
  ConfirmActionDialog,
  FlowStrip,
  SegmentedControl,
  StatusPill,
} from '@/components/WorkspaceUI'
import { useApp, type FieldErrors, type TaskMode } from '@/hooks/appContext'
import { useConfigSave } from '@/hooks/useConfigSave'
import { api, isApiReady, isWebTransport, type OrderFormPayload, type SssDayOrders, type SssFormPayload } from '@/lib/bridge'
import { formatISO, modeError } from '@/lib/format'
import { toFieldErrors, validateOrderDraft, validateSssDraft } from '@/lib/formValidation'
import { passwordResetVersion } from '@/lib/passwordDraft'
import { classifyRequestError } from '@/lib/requestError'
import { previewLocalKey } from '@/lib/preview'
import { saveStateView } from '@/lib/saveState'
import { cn } from '@/lib/utils'

const TAB_ORDER: TaskMode[] = ['order', 'cloud', 'sss']

export function TaskPanel() {
  const { mode, setMode, config, operationView } = useApp()
  const formKey = config ? 'ready' : 'loading'

  const focusTab = useCallback((next: TaskMode) => {
    setMode(next)
    window.requestAnimationFrame(() => {
      document.getElementById(`task-tab-${next}`)?.focus()
    })
  }, [setMode])

  const onTabKeyDown = useCallback((event: KeyboardEvent<HTMLButtonElement>, current: TaskMode) => {
    const index = TAB_ORDER.indexOf(current)
    let next = current
    if (event.key === 'ArrowRight' || event.key === 'ArrowDown') {
      next = TAB_ORDER[(index + 1) % TAB_ORDER.length]
    } else if (event.key === 'ArrowLeft' || event.key === 'ArrowUp') {
      next = TAB_ORDER[(index - 1 + TAB_ORDER.length) % TAB_ORDER.length]
    } else if (event.key === 'Home') {
      next = TAB_ORDER[0]
    } else if (event.key === 'End') {
      next = TAB_ORDER[TAB_ORDER.length - 1]
    } else {
      return
    }
    event.preventDefault()
    focusTab(next)
  }, [focusTab])

  return (
    <section className="flex min-w-0 min-h-0 flex-1 flex-col border-border">
      <div className="shrink-0 border-b bg-card/60 px-3 pt-3 sm:px-5">
        <div className="mb-2 flex min-w-0 items-center gap-2">
          <div className="min-w-0 flex-1">
            <h1 className="font-serif text-base font-semibold tracking-[1px]">任务工作台</h1>
            {/* 唯一的权威状态说明。原来是单行 `truncate`：长失败原因会被省略号截掉，
                而页签内那份完整的重复 Callout 已按批 2 方案删除 —— 所以这里改成允许
                折行，失败原因必须完整可见，并保留 danger 时的 alert 语义。
                不截断由门禁「批2 三页签状态矩阵」按 scrollWidth/scrollHeight 锁住。 */}
            <p
              data-operation-detail="true"
              className="break-words text-[11px] text-muted-foreground"
              title={operationView.detail}
              role={operationView.tone === 'danger' ? 'alert' : undefined}
            >
              {operationView.detail}
            </p>
          </div>
          <StatusPill label={operationView.label} tone={operationView.tone} live={operationView.active} />
        </div>
        <div role="tablist" aria-label="任务类型" className="flex gap-1">
          {TAB_ORDER.map((tab) => (
            <ModeTab
              key={tab}
              mode={tab}
              active={mode === tab}
              onClick={() => setMode(tab)}
              onKeyDown={(event) => onTabKeyDown(event, tab)}
            />
          ))}
        </div>
      </div>

      {/* 常驻渲染三块面板：切换页签不丢草稿；hidden 保留状态且不被读屏聚焦。 */}
      <div
        id="task-panel-order"
        role="tabpanel"
        aria-labelledby="task-tab-order"
        hidden={mode !== 'order'}
        className="min-h-0 flex-1 flex-col data-[hidden=false]:flex"
        data-hidden={mode !== 'order'}
      >
        <OrderForm key={formKey} />
      </div>
      <div
        id="task-panel-cloud"
        role="tabpanel"
        aria-labelledby="task-tab-cloud"
        hidden={mode !== 'cloud'}
        className="min-h-0 flex-1 flex-col data-[hidden=false]:flex"
        data-hidden={mode !== 'cloud'}
      >
        <CloudForm key={formKey} />
      </div>
      <div
        id="task-panel-sss"
        role="tabpanel"
        aria-labelledby="task-tab-sss"
        hidden={mode !== 'sss'}
        className="min-h-0 flex-1 flex-col data-[hidden=false]:flex"
        data-hidden={mode !== 'sss'}
      >
        <SssForm key={formKey} />
      </div>
    </section>
  )
}

function ModeTab({
  mode,
  active,
  onClick,
  onKeyDown,
}: {
  mode: TaskMode
  active: boolean
  onClick: () => void
  onKeyDown: (event: KeyboardEvent<HTMLButtonElement>) => void
}) {
  const labels: Record<TaskMode, string> = {
    order: '订单处理',
    cloud: '云文档同步',
    sss: '闪时送下单',
  }
  return (
    <button
      id={`task-tab-${mode}`}
      type="button"
      role="tab"
      aria-selected={active}
      aria-controls={`task-panel-${mode}`}
      tabIndex={active ? 0 : -1}
      onClick={onClick}
      onKeyDown={onKeyDown}
      className={cn(
        'relative min-h-10 flex-1 rounded-t-md px-2 pb-2 pt-1.5 text-[13px] font-medium transition-colors sm:min-h-9',
        active ? 'bg-background text-foreground' : 'text-muted-foreground hover:bg-secondary/60 hover:text-foreground',
        active && "after:absolute after:inset-x-2 after:bottom-0 after:h-0.5 after:rounded-full after:bg-primary after:content-['']",
      )}
    >
      {labels[mode]}
    </button>
  )
}

/* ------------------------------------------------------------------ */
/* 订单处理                                                             */
/* ------------------------------------------------------------------ */

function OrderForm() {
  const { config, passwords, startOrder, workerAlive, operationActive, operationView, isAdmin, passwordReset } = useApp()
  const orderResetVersion = passwordResetVersion(passwordReset, 'order')
  const [url, setUrl] = useState(config?.target_url ?? '')
  const [phone, setPhone] = useState(config?.phone_number ?? '')
  const [passwordDraft, setPasswordDraft] = useState({
    value: passwords.order ?? '',
    version: orderResetVersion,
  })
  const password = passwordDraft.version === orderResetVersion ? passwordDraft.value : ''
  const setPassword = (value: string) => setPasswordDraft({ value, version: orderResetVersion })
  const [excel, setExcel] = useState(config?.excel_path ?? '')
  const [date, setDate] = useState(config?.order_date ?? '')
  const [count, setCount] = useState<number | null>(config?.order_count ?? null)
  const [remember, setRemember] = useState(true)
  const [fields, setFields] = useState<FieldErrors | null>(null)
  const [busy, setBusy] = useState(false)
  const [confirmOpen, setConfirmOpen] = useState(false)
  const [advancedOpen, setAdvancedOpen] = useState(() =>
    Boolean(!config?.target_url || !config?.phone_number || !config?.excel_path),
  )
  const [browser, setBrowser] = useState<'open' | 'save' | null>(null)

  const save = useCallback(async () => {
    if (!isWebTransport() || !isApiReady()) {
      return { ok: false, reason: '后端未连接，配置尚未保存' }
    }
    return api().save_order_config({ url, phone, excel, date, count })
  }, [url, phone, excel, date, count])
  const { state: saveState, schedule, retry } = useConfigSave(save)
  const firstSave = useRef(true)
  useEffect(() => {
    if (firstSave.current) {
      firstSave.current = false
      return
    }
    schedule()
  }, [url, phone, excel, date, count, schedule])

  const advancedMissing = Boolean(!config?.target_url || !config?.phone_number || !config?.excel_path)
  const excelError = modeError(fields, 'excel')

  function applyExcelResult(result: { path: string; error: string }) {
    if (result.path) {
      setExcel(result.path)
      setFields((prev) => ({ ...prev, excel: undefined }))
    }
    if (result.error) setFields((prev) => ({ ...prev, excel: { message: result.error } }))
  }

  async function chooseFile() {
    if (!isApiReady()) return
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

  async function onBrowserPick(path: string) {
    const picked = browser
    setBrowser(null)
    if (picked === 'save') {
      applyExcelResult(await api().new_template('order', path))
      return
    }
    applyExcelResult(await api().choose_excel('order', path))
  }

  function validateAndPreview() {
    const errors = validateOrderDraft({ isAdmin, url, phone, password, excel, date, count, today: formatISO(new Date()) })
    if (Object.keys(errors).length > 0) {
      setFields(toFieldErrors(errors))
      if (errors.url || errors.phone || errors.password || errors.excel) setAdvancedOpen(true)
      return
    }
    setFields(null)
    setConfirmOpen(true)
  }

  async function performStart() {
    setBusy(true)
    try {
      const payload: OrderFormPayload = {
        url, phone, password, excel, date,
        count: count === null ? '' : String(count),
        remember,
      }
      const errors = await startOrder(payload)
      if (errors && Object.keys(errors).length > 0) {
        setFields(errors)
        setConfirmOpen(false)
        if (errors.url || errors.phone || errors.password || errors.excel) setAdvancedOpen(true)
        return
      }
      setConfirmOpen(false)
    } finally {
      setBusy(false)
    }
  }

  const flow: Array<{ key: string; label: string; state: 'todo' | 'active' | 'done' | 'warning' | 'error'; detail?: string }> = [
    { key: 'prepare', label: '准备', state: 'done' },
    { key: 'validate', label: '校验', state: confirmOpen ? 'done' : 'active' },
    { key: 'confirm', label: '确认', state: confirmOpen ? 'active' : 'todo' },
    { key: 'run', label: '执行', state: workerAlive ? 'active' : operationView.key === 'success' ? 'done' : 'todo' },
    {
      key: 'result',
      label: '结果',
      // 只保留「结果这一步处于什么状态」这一独有信息；重复的说明文本已删除（批 2）。
      state: operationView.key === 'error' ? 'error' : operationView.needsReview ? 'warning' : operationView.key === 'success' ? 'done' : 'todo',
    },
  ]

  return (
    <div className="flex min-w-0 min-h-0 flex-1 flex-col">
      <div className="scroll-contain min-h-0 flex-1 overflow-y-auto px-3 pb-4 pt-3 sm:px-5">
        <FlowStrip steps={flow} label="订单处理流程" />

        {isAdmin && (
          <AdvancedSection
            id="order-advanced"
            title="管理网址与登录凭据"
            summary={advancedMissing ? '首次使用需先补齐，否则无法启动' : '已配置，可展开修改'}
            open={advancedOpen}
            onOpenChange={setAdvancedOpen}
            notice={advancedMissing ? '缺少网址、账号或排单表路径。首次使用请先完成这些必填配置；系统会按后端最终校验为准。' : undefined}
          >
            <Field label="管理网址" htmlFor="order-url" error={modeError(fields, 'url')} helper="用于登录管理后台">
              <TextInput id="order-url" value={url} onChange={(e) => setUrl(e.target.value)} placeholder="https://example.com/admin" autoComplete="url" />
            </Field>
            <Field label="手机号 / 账号" htmlFor="order-phone" error={modeError(fields, 'phone')} helper="用于登录管理后台">
              <TextInput id="order-phone" value={phone} onChange={(e) => setPhone(e.target.value)} autoComplete="username" />
            </Field>
            <Field label="登录密码" htmlFor="order-password" error={modeError(fields, 'password')} helper="只保存在系统凭据管理器">
              <TextInput id="order-password" type="password" value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="current-password" />
            </Field>
            <Field label="Excel 排单文件" htmlFor="order-excel" error={excelError} helper="支持 .xlsx / .xlsm">
              <div className="flex flex-wrap gap-1.5">
                <TextInput id="order-excel" value={excel} onChange={(e) => setExcel(e.target.value)} className="min-w-[12rem] flex-1" placeholder="选择排单 .xlsx 文件" />
                <GhostButton onClick={() => void chooseFile()}>选择文件</GhostButton>
                <GhostButton onClick={() => void newTemplate()}>新建模板</GhostButton>
              </div>
            </Field>
            {/* 低频设置：与闪时送页一致放进高级设置。默认值、保存语义与清除凭据行为都不变。 */}
            <div className="flex items-center gap-2 text-[12px] text-muted-foreground">
              <Switch checked={remember} onCheckedChange={setRemember} aria-label="保存到系统凭据管理器" />
              <span>保存到系统凭据管理器</span>
            </div>
          </AdvancedSection>
        )}
        {!isAdmin && (
          <Callout tone="neutral" title="按管理员预设运行">
            本账号无需填写管理网址、账号、密码和文件路径；任务会使用管理员预设配置。真正的强制拦截在服务端。
          </Callout>
        )}

        <Field label="目标日期" htmlFor="order-date" error={modeError(fields, 'date')} helper="留空默认今天；只允许今天或过去日期">
          <DateField value={date} onChange={setDate} invalid={Boolean(modeError(fields, 'date'))} label="选择目标日期" />
        </Field>
        <Field label="待处理订单数" htmlFor="order-count" error={modeError(fields, 'count')} helper="留空 = 处理全部订单">
          <Stepper id="order-count" value={count} onChange={setCount} invalid={Boolean(modeError(fields, 'count'))} />
        </Field>

      </div>

      <BottomDock
        status={<SaveStatus state={saveState} onRetry={retry} />}
        primaryLabel={operationActive ? '已有操作进行中' : '开始处理'}
        primaryDisabled={operationActive}
        primaryBusy={busy}
        onPrimary={validateAndPreview}
        workerAlive={workerAlive}
        tools={<ToolsMenu mode="order" />}
      />

      <ConfirmActionDialog
        open={confirmOpen}
        onOpenChange={setConfirmOpen}
        title="确认开始订单处理"
        description="将按当前配置读取订单并写入排单表；这不是闪时送真实下单，但仍会修改服务端文件。"
        details={<StartSummary items={[
          ['目标日期', date || '今天'],
          ['处理数量', count === null ? '全部' : `${count} 条`],
          ['排单表', excel || '使用已配置路径'],
          ['登录账号', phone || '使用服务端预设'],
        ]} />}
        acknowledge="我确认以上信息正确，并了解该操作会读取订单并写入排单表"
        confirmLabel="确认开始处理"
        busy={busy}
        onConfirm={performStart}
      />

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

type SssExecutionMode = 'dry_run' | 'preflight' | 'live'

function SssForm() {
  const {
    config, passwords, startSss, workerAlive, operationActive, operationView, isAdmin, passwordReset,
  } = useApp()
  const sssResetVersion = passwordResetVersion(passwordReset, 'sss')
  const [url, setUrl] = useState(config?.sss_url ?? '')
  const [account, setAccount] = useState(config?.sss_account ?? '')
  const [passwordDraft, setPasswordDraft] = useState({
    value: passwords.sss ?? '',
    version: sssResetVersion,
  })
  const password = passwordDraft.version === sssResetVersion ? passwordDraft.value : ''
  const setPassword = (value: string) => setPasswordDraft({ value, version: sssResetVersion })
  const [excel, setExcel] = useState(config?.sss_excel_path ?? '')
  const [orderSource, setOrderSource] = useState<'wps' | 'excel'>(config?.sss_order_source ?? 'wps')
  const [productName, setProductName] = useState(config?.sss_product_name ?? '轻食')
  const [executionMode, setExecutionMode] = useState<SssExecutionMode>(() => {
    if (config?.sss_dry_run) return 'dry_run'
    if (config?.sss_preflight) return 'preflight'
    return 'live'
  })
  const [remember, setRemember] = useState(true)
  const [fields, setFields] = useState<FieldErrors | null>(null)
  const [busy, setBusy] = useState(false)
  const [confirmOpen, setConfirmOpen] = useState(false)
  const [advancedOpen, setAdvancedOpen] = useState(() =>
    Boolean(!config?.sss_url || !config?.sss_account || !config?.sss_excel_path),
  )
  const [browser, setBrowser] = useState<'open' | 'save' | null>(null)
  const [dayPreview, setDayPreview] = useState<SssDayOrders | null>(null)
  const [dayKey, setDayKey] = useState('')
  const [dayLoading, setDayLoading] = useState(false)
  const [dayError, setDayError] = useState('')

  const commonAddress = config?.sss_common_address ?? ''
  const useFixedAddress = config?.sss_use_fixed_address ?? true
  const fixedLnt = String(config?.sss_fixed_lnt ?? '119.728224')
  const fixedLat = String(config?.sss_fixed_lat ?? '30.256632')
  const fixedAreaCode = config?.sss_fixed_area_code ?? '330110'
  const fixedAddressDetail = config?.sss_fixed_address_detail ?? '浙江农林大学东湖校区'

  const dryRun = executionMode === 'dry_run'
  const preflight = executionMode === 'preflight'

  const save = useCallback(async () => {
    if (!isWebTransport() || !isApiReady()) {
      return { ok: false, reason: '后端未连接，配置尚未保存' }
    }
    return api().save_sss_config({
      url, account, excel, order_source: orderSource, product_name: productName,
      common_address: commonAddress, use_fixed_address: useFixedAddress,
      fixed_lnt: fixedLnt, fixed_lat: fixedLat, fixed_area_code: fixedAreaCode,
      fixed_address_detail: fixedAddressDetail, dry_run: dryRun, preflight,
    })
  }, [
    url, account, excel, orderSource, productName, commonAddress, useFixedAddress,
    fixedLnt, fixedLat, fixedAreaCode, fixedAddressDetail, dryRun, preflight,
  ])
  const { state: saveState, schedule, retry } = useConfigSave(save)
  const firstSave = useRef(true)
  useEffect(() => {
    if (firstSave.current) {
      firstSave.current = false
      return
    }
    schedule()
  }, [
    url, account, excel, orderSource, productName, commonAddress, useFixedAddress,
    fixedLnt, fixedLat, fixedAreaCode, fixedAddressDetail, dryRun, preflight, schedule,
  ])

  const currentDayKey = previewLocalKey({
    url, account, orderSource, productName, executionMode,
  })
  const dayPreviewFresh = Boolean(dayPreview?.ok && dayKey === currentDayKey)
  const needsDayPreview = orderSource === 'wps' && !dayPreviewFresh
  const advancedMissing = Boolean(!config?.sss_url || !config?.sss_account || !config?.sss_excel_path)
  const excelError = modeError(fields, 'excel')

  async function readDayOrders() {
    if (dayLoading || operationActive) return
    setDayLoading(true)
    setDayError('')
    try {
      const result = await api().sss_day_orders()
      setDayPreview(result)
      setDayKey(currentDayKey)
      if (!result.ok) setDayError(result.reason || '读取云端当天名单失败')
    } catch (error) {
      setDayError(classifyRequestError(error).detail)
    } finally {
      setDayLoading(false)
    }
  }

  function applyExcelResult(result: { path: string; error: string }) {
    if (result.path) {
      setExcel(result.path)
      setFields((prev) => ({ ...prev, excel: undefined }))
    }
    if (result.error) setFields((prev) => ({ ...prev, excel: { message: result.error } }))
  }

  async function chooseFile() {
    if (!isApiReady()) return
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

  async function onBrowserPick(path: string) {
    const picked = browser
    setBrowser(null)
    if (picked === 'save') {
      applyExcelResult(await api().new_template('sss', path))
      return
    }
    applyExcelResult(await api().choose_excel('sss', path))
  }

  function validateAndConfirm() {
    if (needsDayPreview) {
      void readDayOrders()
      return
    }
    const errors = validateSssDraft({ isAdmin, url, account, password, excel, orderSource })
    if (Object.keys(errors).length > 0) {
      setFields(toFieldErrors(errors))
      if (errors.url || errors.account || errors.password || errors.excel) setAdvancedOpen(true)
      return
    }
    setFields(null)
    setConfirmOpen(true)
  }

  async function performStart() {
    setBusy(true)
    try {
      const payload: SssFormPayload = {
        url, account, password, excel, order_source: orderSource,
        product_name: productName, common_address: commonAddress,
        use_fixed_address: useFixedAddress, fixed_lnt: fixedLnt, fixed_lat: fixedLat,
        fixed_area_code: fixedAreaCode, fixed_address_detail: fixedAddressDetail,
        remember, dry_run: dryRun, preflight,
      }
      const errors = await startSss(payload)
      if (errors && Object.keys(errors).length > 0) {
        setFields(errors)
        setConfirmOpen(false)
        if (errors.url || errors.account || errors.password || errors.excel) setAdvancedOpen(true)
        return
      }
      setConfirmOpen(false)
    } finally {
      setBusy(false)
    }
  }

  const modeLabel = executionMode === 'live' ? '正式下单' : executionMode === 'preflight' ? '预检' : '模拟执行'
  const primaryLabel = operationActive
    ? '已有操作进行中'
    : needsDayPreview
      ? (dayLoading ? '正在读取云端名单…' : '先读取云端名单')
      : executionMode === 'live'
        ? '开始正式下单'
        : executionMode === 'preflight'
          ? '开始预检'
          : '开始模拟执行'
  const flow = [
    { key: 'prepare', label: '准备', state: 'done' as const },
    {
      key: 'preview',
      label: orderSource === 'wps' ? '名单预览' : '文件校验',
      state: orderSource === 'wps'
        ? (needsDayPreview ? 'active' as const : 'done' as const)
        : (confirmOpen ? 'done' as const : 'active' as const),
    },
    { key: 'confirm', label: '确认', state: confirmOpen ? 'active' as const : 'todo' as const },
    { key: 'run', label: '执行', state: workerAlive ? 'active' as const : operationView.key === 'success' ? 'done' as const : 'todo' as const },
    {
      key: 'result', label: '结果',
      // 只保留「结果这一步处于什么状态」这一独有信息；重复的说明文本已删除（批 2）。
      state: operationView.key === 'error' ? 'error' as const : operationView.needsReview ? 'warning' as const : operationView.key === 'success' ? 'done' as const : 'todo' as const,
    },
  ]

  return (
    <div className="flex min-w-0 min-h-0 flex-1 flex-col">
      <div className="scroll-contain min-h-0 flex-1 overflow-y-auto px-3 pb-4 pt-3 sm:px-5">
        <FlowStrip steps={flow} label="闪时送执行流程" />

        {isAdmin && (
          <AdvancedSection
            id="sss-advanced"
            title="闪时送网址、账号与文件路径"
            summary={advancedMissing ? '首次使用需先补齐' : '已配置，可展开修改'}
            open={advancedOpen}
            onOpenChange={setAdvancedOpen}
            notice={advancedMissing ? '缺少网址、账号或本地 Excel 路径。云端名单模式可暂时留空 Excel；本地名单模式必须选择文件。' : undefined}
          >
            <Field label="闪时送网址" htmlFor="sss-url" error={modeError(fields, 'url')} helper="闪时送下单平台地址">
              <TextInput id="sss-url" value={url} onChange={(e) => setUrl(e.target.value)} autoComplete="url" />
            </Field>
            <Field label="闪时送账号" htmlFor="sss-account" error={modeError(fields, 'account')} helper="用于登录闪时送平台">
              <TextInput id="sss-account" value={account} onChange={(e) => setAccount(e.target.value)} autoComplete="username" />
            </Field>
            <Field label="登录密码" htmlFor="sss-password" error={modeError(fields, 'password')} helper="只保存在系统凭据管理器">
              <TextInput id="sss-password" type="password" value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="current-password" />
            </Field>
            <Field
              label="本地名单 Excel（本地模式必填）"
              htmlFor="sss-excel"
              error={excelError}
              helper={orderSource === 'wps' ? '云端模式仅作为留档文件，可留空' : '午餐/晚餐两表：A=姓名 B=门牌号 C=电话'}
            >
              <div className="flex flex-wrap gap-1.5">
                <TextInput id="sss-excel" value={excel} onChange={(e) => setExcel(e.target.value)} className="min-w-[12rem] flex-1" placeholder="选择闪时送 .xlsx 文件" />
                <GhostButton onClick={() => void chooseFile()}>选择文件</GhostButton>
                <GhostButton onClick={() => void newTemplate()}>新建模板</GhostButton>
              </div>
            </Field>
            <div className="flex items-center gap-2 text-[12px] text-muted-foreground">
              <Switch checked={remember} onCheckedChange={setRemember} aria-label="保存到系统凭据管理器" />
              <span>保存到系统凭据管理器</span>
            </div>
          </AdvancedSection>
        )}

        <Field label="执行方式" helper="正式与模拟使用不同确认文字，不能仅靠按钮颜色">
          <SegmentedControl
            value={executionMode}
            onChange={setExecutionMode}
            label="闪时送执行方式"
            disabled={operationActive}
            options={[
              { value: 'dry_run', label: '模拟执行', description: '不创建订单，只预览报文', tone: 'neutral' },
              { value: 'preflight', label: '预检', description: '登录检查，不创建订单', tone: 'warning' },
              { value: 'live', label: '正式下单', description: '真实创建订单，可能产生费用', tone: 'danger' },
            ]}
          />
        </Field>

        <Field label="名单来源" helper="云端模式会先读取东湖午餐/晚餐当天标 1 的人；本地模式读取 Excel">
          <SegmentedControl
            value={orderSource}
            onChange={(value) => setOrderSource(value)}
            label="名单来源"
            options={[
              { value: 'wps', label: '云端当天名单', description: '读取 WPS 当天标 1' },
              { value: 'excel', label: '本地 Excel', description: '人工准备名单文件' },
            ]}
          />
        </Field>

        {orderSource === 'wps' && (
          <div className="mb-3.5">
            {needsDayPreview ? (
              <Callout tone={dayError ? 'danger' : 'warning'} title={dayError ? '名单预览失败' : '名单预览已失效或尚未读取'}>
                {dayError || (dayPreview ? '配置或执行方式已变化，请重新读取名单预览。' : '执行前需要先只读读取云端当天名单，生成结构化预览。')}
                <button type="button" className="mt-1.5 block text-left font-medium text-primary underline" disabled={dayLoading || operationActive} onClick={() => void readDayOrders()}>
                  {dayLoading ? '正在读取…' : '重新读取云端名单'}
                </button>
              </Callout>
            ) : dayPreview ? (
              <DayPreviewCard preview={dayPreview} />
            ) : null}
          </div>
        )}

        <Field label="商品名称" htmlFor="sss-product" helper="下单时商品“名称”的默认值">
          <TextInput id="sss-product" value={productName} onChange={(e) => setProductName(e.target.value)} />
        </Field>
      </div>

      <BottomDock
        status={<SaveStatus state={saveState} onRetry={retry} />}
        primaryLabel={primaryLabel}
        primaryDisabled={operationActive || dayLoading}
        primaryBusy={busy}
        onPrimary={validateAndConfirm}
        workerAlive={workerAlive}
        tools={<ToolsMenu mode="sss" />}
      />

      <ConfirmActionDialog
        open={confirmOpen}
        onOpenChange={setConfirmOpen}
        title={`确认${modeLabel}`}
        description={executionMode === 'live'
          ? '这是真实下单操作。服务端会创建订单，可能扣款或占用余额。请再次核对账号、名单来源与文件。'
          : executionMode === 'preflight'
            ? '预检会登录平台并检查，但不会创建订单；如发现风险会安全停止。'
            : '模拟执行只组装并预览报文，不会创建真实订单。'}
        details={<StartSummary items={[
          ['执行方式', modeLabel],
          ['名单来源', orderSource === 'wps' ? `云端当天名单${dayPreview?.target_date ? `（${dayPreview.target_date}）` : ''}` : '本地 Excel'],
          ['账号', account || '使用服务端预设'],
          ['本地文件', excel || (orderSource === 'wps' ? '云端模式可不选' : '使用已配置路径')],
        ]} />}
        acknowledge={executionMode === 'live'
          ? '我确认执行真实下单，并已核对账号、名单与执行方式'
          : executionMode === 'preflight'
            ? '我确认执行预检，不会创建真实订单'
            : '我确认执行模拟，不会创建真实订单'}
        confirmLabel={executionMode === 'live' ? '确认正式下单' : executionMode === 'preflight' ? '开始预检' : '开始模拟'}
        busy={busy}
        danger={executionMode === 'live'}
        onConfirm={performStart}
      />

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

function DayPreviewCard({ preview }: { preview: SssDayOrders }) {
  const meals = Object.entries(preview.meals ?? {})
  return (
    <div className="rounded-lg border bg-card p-3">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px]">
        <span className="font-medium text-foreground">云端当天名单预览</span>
        <span className="text-muted-foreground">目标日期：<b className="tabular text-foreground">{preview.target_date || '—'}</b></span>
        {preview.date_text && <span className="text-muted-foreground">{preview.date_text}</span>}
      </div>
      {meals.length === 0 ? (
        <p className="mt-1.5 text-[11px] text-muted-foreground">没有返回可展示的餐次结构化数据</p>
      ) : (
        <div className="mt-2 grid gap-1.5 sm:grid-cols-2">
          {meals.map(([name, info]) => (
            <div key={name} className="rounded-md border bg-secondary/30 px-2.5 py-2 text-[11px]">
              <p className="font-medium text-foreground">{name}</p>
              {info.skipped ? (
                <p className="mt-0.5 text-warning">不下单：{info.reason || '没有当天列'}</p>
              ) : (
                <p className="mt-0.5 text-muted-foreground">
                  下单 <b className="tabular text-foreground">{info.orders}</b> 人 · 标 1 <b className="tabular">{info.marked}</b> · 地址不送 <b className="tabular">{info.skipped_address}</b>
                </p>
              )}
              {info.warnings?.length ? <p className="mt-0.5 text-warning">{info.warnings.join('；')}</p> : null}
            </div>
          ))}
        </div>
      )}
      {preview.archive_error && <p className="mt-1.5 text-[11px] text-destructive">留档 Excel 写入失败：{preview.archive_error}</p>}
    </div>
  )
}

/* ------------------------------------------------------------------ */
/* 共享：底栏、返回、工具菜单、密码清除、摘要                          */
/* ------------------------------------------------------------------ */

function StartSummary({ items }: { items: Array<[string, string]> }) {
  return (
    <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 rounded-md border bg-secondary/40 px-3 py-2 text-[11px]">
      {items.map(([label, value]) => (
        <div key={label} className="contents">
          <dt className="text-muted-foreground">{label}</dt>
          <dd className="min-w-0 truncate text-foreground">{value}</dd>
        </div>
      ))}
    </dl>
  )
}

function SaveStatus({ state, onRetry }: { state: ReturnType<typeof useConfigSave>['state']; onRetry: () => void }) {
  const view = saveStateView(state)
  return (
    <span className="inline-flex min-w-0 items-center gap-1 text-[11px]">
      <span className={cn(
        'truncate',
        view.tone === 'warn' && 'text-warning',
        view.tone === 'success' && 'text-success',
        view.tone === 'progress' && 'text-primary',
        view.tone === 'muted' && 'text-muted-foreground',
      )}>{view.label}</span>
      {view.canRetry && (
        <button type="button" onClick={onRetry} className="shrink-0 font-medium text-primary underline">
          重试保存
        </button>
      )}
    </span>
  )
}

function BottomDock({
  status,
  primaryLabel,
  primaryDisabled,
  primaryBusy,
  onPrimary,
  workerAlive,
  tools,
}: {
  status: ReactNode
  primaryLabel: string
  primaryDisabled: boolean
  primaryBusy: boolean
  onPrimary: () => void
  workerAlive: boolean
  tools?: ReactNode
}) {
  const { stopTask } = useApp()
  const [confirmingStop, setConfirmingStop] = useState(false)
  return (
    <div
      className="shrink-0 border-t bg-background/95 px-3 pt-2.5 backdrop-blur sm:px-5"
      style={{ paddingBottom: 'calc(0.75rem + var(--safe-bottom) + var(--keyboard-inset, 0px))' }}
    >
      <div className="mb-2 flex min-h-5 min-w-0 items-center gap-2 text-[11px] text-muted-foreground">
        {status}
        {tools}
      </div>
      {/* 底栏只留业务动作：日志入口唯一化到右上角悬浮按钮（components/LogFab.tsx），
          展开/收起都由它负责，这里不再放第二颗「日志」按钮。 */}
      <div className="flex gap-2">
        <Button
          className="btn-serif-primary h-10 flex-1 rounded-[8px] text-sm"
          disabled={primaryDisabled || primaryBusy}
          onClick={onPrimary}
        >
          {primaryBusy ? '执行中…' : primaryLabel}
        </Button>
        <Button
          variant="outline"
          className="h-10 w-20 rounded-[8px] border-destructive/45 bg-card text-xs text-destructive hover:bg-destructive/5 hover:text-destructive"
          disabled={!workerAlive}
          onClick={() => setConfirmingStop(true)}
        >
          停止
        </Button>
      </div>
      <ConfirmStopDialog open={confirmingStop} onOpenChange={setConfirmingStop} onConfirm={stopTask} />
    </div>
  )
}

/* 停止确认：是=停止 / 否=继续 / 取消=返回；文案去掉旧浏览器时代内容。 */
function ConfirmStopDialog({
  open,
  onOpenChange,
  onConfirm,
}: {
  open: boolean
  onOpenChange: (v: boolean) => void
  onConfirm: () => void | Promise<void>
}) {
  const [busy, setBusy] = useState(false)
  async function confirm() {
    setBusy(true)
    try {
      await onConfirm()
      onOpenChange(false)
    } finally {
      setBusy(false)
    }
  }
  return (
    <Dialog open={open} onOpenChange={(next) => { if (!busy) onOpenChange(next) }}>
      <DialogContent className="sm:max-w-sm rounded-lg">
        <DialogHeader>
          <DialogTitle className="font-serif">停止当前任务</DialogTitle>
          <DialogDescription>
            停止后服务端会等待当前操作安全收尾，并保留未完成状态供核对。请查看日志确认结果。
          </DialogDescription>
        </DialogHeader>
        <DialogFooter className="gap-2">
          <Button variant="ghost" className="h-9 text-xs" disabled={busy} onClick={() => onOpenChange(false)}>
            返回
          </Button>
          <Button
            variant="outline"
            className="h-9 rounded-[6px] text-xs"
            disabled={busy}
            onClick={() => onOpenChange(false)}
          >
            继续执行
          </Button>
          <Button
            className="h-9 rounded-[6px] bg-destructive text-xs text-white hover:bg-destructive/90"
            disabled={busy}
            onClick={() => void confirm()}
          >
            {busy ? '停止中…' : '停止任务'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

function ToolsMenu({ mode }: { mode: TaskMode }) {
  const { clearPassword, checkUpdates, isAdmin } = useApp()
  const isSss = mode === 'sss'
  const passwordLabel = isSss ? '清除闪时送密码' : '清除管理后台密码'
  const toolsLabel = isSss ? '闪时送更多工具' : '订单处理更多工具'
  const accountHint = isSss ? '闪时送账号' : '管理后台账号'
  const [clearOpen, setClearOpen] = useState(false)
  const [clearing, setClearing] = useState(false)
  const [clearError, setClearError] = useState('')

  if (!isAdmin) return null

  async function doClear() {
    setClearing(true)
    setClearError('')
    const result = await clearPassword(mode === 'sss' ? 'sss' : 'order')
    setClearing(false)
    if (result.ok) {
      setClearOpen(false)
    } else {
      setClearError([result.reason || '清除密码失败，请重试', result.next_action]
        .filter(Boolean).join('；'))
    }
  }

  return (
    <>
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <button className="inline-flex shrink-0 items-center gap-0.5 rounded px-1 py-0.5 text-muted-foreground hover:bg-secondary hover:text-foreground" aria-label={toolsLabel}>
            <MoreHorizontal className="size-3.5" />
            更多
          </button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="start" className="rounded-md text-xs">
          <DropdownMenuItem onClick={() => { setClearError(''); setClearOpen(true) }}>{passwordLabel}</DropdownMenuItem>
          <DropdownMenuItem onClick={() => checkUpdates(true)}>检查更新</DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>
      <Dialog open={clearOpen} onOpenChange={(next) => { if (!clearing) setClearOpen(next) }}>
        <DialogContent className="sm:max-w-sm rounded-lg">
          <DialogHeader>
            <DialogTitle className="font-serif">{passwordLabel}</DialogTitle>
            <DialogDescription>
              将从系统凭据管理器删除本机保存的{accountHint}密码；服务端确认成功后，当前表单密码框会同步清空。失败或结果未知时会保留草稿。
            </DialogDescription>
          </DialogHeader>
          {clearError && <p role="alert" className="rounded-md border border-destructive/40 bg-destructive/5 px-3 py-2 text-xs text-destructive">{clearError}</p>}
          <DialogFooter className="gap-2">
            <Button variant="ghost" className="h-9 text-xs" disabled={clearing} onClick={() => setClearOpen(false)}>取消</Button>
            <Button className="h-9 rounded-[6px] text-xs" disabled={clearing} onClick={() => void doClear()}>
              {clearing ? '清除中…' : '确认清除'}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  )
}
