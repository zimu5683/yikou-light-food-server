import assert from 'node:assert/strict'
import test from 'node:test'
import { compareIsoDate, validateOrderDraft, validateSssDraft } from './formValidation.ts'

test('管理员订单表单校验必填与未来日期', () => {
  const errors = validateOrderDraft({ isAdmin: true, url: '', phone: '', password: '', excel: '', date: '2026-09-20', count: null, today: '2026-09-19' })
  assert.deepEqual(Object.keys(errors).sort(), ['date', 'excel', 'password', 'phone', 'url'])
  assert.match(errors.date, /今天或过去/)
  assert.equal(compareIsoDate('2026-09-19', '2026-09-19'), 0)
})

test('非管理员不校验被隐藏/后端强制的字段', () => {
  const errors = validateOrderDraft({ isAdmin: false, url: '', phone: '', password: '', excel: '', date: '2026-09-19', count: 2, today: '2026-09-19' })
  assert.deepEqual(errors, {})
})

test('数量与本地 Excel 名单来源有对应校验', () => {
  assert.match(validateOrderDraft({ isAdmin: true, url: 'u', phone: 'p', password: 'x', excel: 'f.xlsx', date: '', count: 0, today: '2026-09-19' }).count, /1～9999/)
  assert.match(validateSssDraft({ isAdmin: true, url: 'u', account: 'a', password: 'x', excel: '', orderSource: 'excel' }).excel, /必须选择/)
  assert.deepEqual(validateSssDraft({ isAdmin: false, url: '', account: '', password: '', excel: '', orderSource: 'excel' }), {})
})
