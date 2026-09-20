/**
 * 纯本地预校验：只挡住明显无效输入，最终校验与权限强制仍由后端负责。
 * 非管理员不校验被隐藏/服务端强制的字段，避免前端误拦截。
 */

export interface OrderDraft {
  isAdmin: boolean
  url: string
  phone: string
  password: string
  excel: string
  date: string
  count: number | null
  today: string
}

export interface SssDraft {
  isAdmin: boolean
  url: string
  account: string
  password: string
  excel: string
  orderSource: 'wps' | 'excel'
}

export type DraftErrors = Record<string, string>

function isValidIsoDate(value: string): boolean {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value)
  if (!match) return false
  const year = Number(match[1])
  const month = Number(match[2])
  const day = Number(match[3])
  const date = new Date(year, month - 1, day)
  return date.getFullYear() === year && date.getMonth() === month - 1 && date.getDate() === day
}

export function compareIsoDate(a: string, b: string): number {
  return a < b ? -1 : a > b ? 1 : 0
}

export function validateOrderDraft(draft: OrderDraft): DraftErrors {
  const errors: DraftErrors = {}
  if (draft.isAdmin) {
    if (!draft.url.trim()) errors.url = '请输入管理网址'
    if (!draft.phone.trim()) errors.phone = '请输入手机号或账号'
    if (!draft.password) errors.password = '请输入登录密码'
    if (!draft.excel.trim()) errors.excel = '请选择排单 Excel 文件'
  }
  const date = draft.date.trim()
  if (date) {
    if (!isValidIsoDate(date)) {
      errors.date = '日期格式应为 YYYY-MM-DD'
    } else if (compareIsoDate(date, draft.today) > 0) {
      errors.date = '只允许选择今天或过去日期'
    }
  }
  if (draft.count !== null) {
    if (!Number.isInteger(draft.count) || draft.count < 1 || draft.count > 9999) {
      errors.count = '请输入 1～9999 的整数，或留空处理全部'
    }
  }
  return errors
}

export function validateSssDraft(draft: SssDraft): DraftErrors {
  const errors: DraftErrors = {}
  if (draft.isAdmin) {
    if (!draft.url.trim()) errors.url = '请输入闪时送网址'
    if (!draft.account.trim()) errors.account = '请输入闪时送账号'
    if (!draft.password) errors.password = '请输入登录密码'
    if (draft.orderSource === 'excel' && !draft.excel.trim()) {
      errors.excel = '本地 Excel 模式必须选择名单文件'
    }
  }
  return errors
}

export function toFieldErrors(errors: DraftErrors): Record<string, { message: string }> {
  const fields: Record<string, { message: string }> = {}
  for (const [key, message] of Object.entries(errors)) fields[key] = { message }
  return fields
}
