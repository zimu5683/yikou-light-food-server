import assert from 'node:assert/strict'
import test from 'node:test'
import { compareIsoDate, validateOrderDraft, validateSssDraft } from './formValidation.ts'

test('订单表单校验必填与未来日期', () => {
  const errors = validateOrderDraft({ url: '', phone: '', password: '', excel: '', date: '2026-09-20', count: null, today: '2026-09-19' })
  assert.deepEqual(Object.keys(errors).sort(), ['date', 'excel', 'password', 'phone', 'url'])
  assert.match(errors.date, /今天或过去/)
  assert.equal(compareIsoDate('2026-09-19', '2026-09-19'), 0)
})

test('填齐之后不再报必填错', () => {
  const errors = validateOrderDraft({ url: 'u', phone: 'p', password: 'x', excel: 'f.xlsx', date: '2026-09-19', count: null, today: '2026-09-19' })
  assert.deepEqual(errors, {})
})

test('数量与本地 Excel 名单来源有对应校验', () => {
  assert.match(validateOrderDraft({ url: 'u', phone: 'p', password: 'x', excel: 'f.xlsx', date: '', count: 0, today: '2026-09-19' }).count, /1～9999/)
  assert.match(validateSssDraft({ url: 'u', account: 'a', password: 'x', excel: '', orderSource: 'excel' }).excel, /必须选择/)
  assert.deepEqual(validateSssDraft({ url: '', account: '', password: '', excel: '', orderSource: 'excel' }), {
    url: '请输入闪时送网址',
    account: '请输入闪时送账号',
    password: '请输入登录密码',
    excel: '本地 Excel 模式必须选择名单文件',
  })
})
