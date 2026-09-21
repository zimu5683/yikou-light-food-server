/**
 * `lib/logDisplay.ts` 的回归锁。
 *
 * 要锁住的是**重复展示**这一个缺陷，而不是某段实现：
 * 1. 折叠态已经显示完整的内容，不得再提供「展开明细」；
 * 2. 展开必须是**替换**摘要，不能摘要 + 全文两份同屏；
 * 3. 能不能展开只取决于「是否真的有隐藏内容」，与字符串长度无关；
 * 4. 搜索命中被折叠的行时，命中内容必须变成可见（强制展开）；
 * 5. 复制拿到的是完整原文，折叠不影响数据。
 *
 * 用合成数据（长单行 / 多行 / 订单摘要 / 错误行 / 跨字段命中）覆盖，不依赖 DOM。
 */
import assert from 'node:assert/strict'
import test from 'node:test'

import {
  logCopyText,
  logRowRender,
  logRowToggle,
  logRowView,
  normalizeLogQuery,
} from './logDisplay.ts'

/** 合成一条「长单行」：>120 字但只有一行 —— 旧规则会误判成可展开。 */
const LONG_SINGLE_LINE =
  `WPS 只读核对完成：目标日期 2026-09-20，目标表「一口轻食排单表」，`
  + `读取 37 行，其中待核对 1 行、已确认 36 行，未发现重复写入痕迹；`
  + `本次为只读查询，不会上传、不会解除阻断、不会自动重试，也不会恢复写入；`
  + `如需继续请先人工只读核对云端与日志。`

/** 合成一条多行日志：首行是摘要，后面还有隐藏内容。 */
const MULTI_LINE = [
  '闪时送下单失败：地址无法自动识别',
  '原始地址：A座12层 靠窗那个工位',
  '规则建议：A12 / 学三 / 教5',
  '处理办法：在待确认地址里补全后重试，不要直接重跑本批',
].join('\n')

/** 合成一条订单摘要：每个字段都已经逐行显示。 */
const ORDER_SUMMARY = 'W123456｜张三｜小份｜已下单'

test('长单行没有隐藏内容：不提供展开，整行直接显示', () => {
  assert.ok(LONG_SINGLE_LINE.length > 120, '前置条件：这条合成数据确实超过旧的长度阈值')
  const view = logRowView(LONG_SINGLE_LINE, '')
  assert.equal(view.expandable, false, '单行日志换行就能看全，不该有展开入口')
  assert.deepEqual(view.visibleLines, [LONG_SINGLE_LINE])
  assert.deepEqual(view.fullLines, [LONG_SINGLE_LINE])
  assert.equal(logRowToggle(view, false), 'none')
  const render = logRowRender(view, false)
  assert.equal(render.full, false)
  assert.deepEqual(render.lines, [LONG_SINGLE_LINE], '折叠态就是完整内容，不截断')
})

test('多行日志折叠只看首行，展开用完整内容替换摘要（不是追加第二份）', () => {
  const view = logRowView(MULTI_LINE, '')
  assert.equal(view.expandable, true)
  assert.deepEqual(view.visibleLines, ['闪时送下单失败：地址无法自动识别'])

  const collapsed = logRowRender(view, false)
  assert.deepEqual(collapsed.lines, view.visibleLines)

  const expanded = logRowRender(view, true)
  assert.deepEqual(expanded.lines, view.fullLines, '展开 = 完整内容')
  // 关键：展开后的行数与完整内容一致。旧实现是「摘要 + <pre>全文</pre>」，
  // 首行会出现两次 —— 这里用「首行只出现一次」把它钉死。
  assert.equal(expanded.lines.filter((line) => line === view.visibleLines[0]).length, 1)
  assert.equal(expanded.lines.length, view.fullLines.length)
})

test('订单摘要逐字段显示完整：永不折叠，也不提供重复全文的展开', () => {
  const view = logRowView(ORDER_SUMMARY, '')
  assert.equal(view.orderSummary, true)
  assert.equal(view.expandable, false)
  assert.deepEqual(view.visibleLines, ['W123456', '张三', '小份', '已下单'])
  assert.deepEqual(view.fullLines, view.visibleLines, '折叠态已经等于完整内容')
  assert.equal(logRowToggle(view, false), 'none')
})

test('订单摘要带超长字段也不可展开：判断依据是隐藏内容，不是长度', () => {
  const longField = `W123456｜张三｜小份｜已下单｜备注：${'很长的备注'.repeat(30)}`
  const view = logRowView(longField, '')
  assert.ok(longField.length > 120, '前置条件：这条合成数据确实超过旧的长度阈值')
  assert.equal(view.expandable, false)
  assert.equal(view.visibleLines.length, view.fullLines.length)
})

test('尾部空行不算隐藏内容；中间空行算（排版信息要留得住）', () => {
  assert.equal(logRowView('只有一行\n', '').expandable, false)
  assert.equal(logRowView('只有一行\n\n\n', '').expandable, false)
  assert.equal(logRowView('只有一行\n   \n', '').expandable, false, '纯空白行也不该造出展开入口')
  const withBlank = logRowView('首行\n\n第三行', '')
  assert.equal(withBlank.expandable, true)
  assert.deepEqual(withBlank.fullLines, ['首行', '', '第三行'])
})

test('CRLF 归一化后再判断行数（Windows 换行不会把首行撑成两行）', () => {
  const view = logRowView('第一行\r\n第二行', '')
  assert.deepEqual(view.fullLines, ['第一行', '第二行'])
  assert.deepEqual(view.visibleLines, ['第一行'])
  assert.equal(logRowView('唯一一行\r\n', '').expandable, false)
})

test('搜索命中隐藏行：强制展开，命中行进入渲染结果并带高亮下标', () => {
  const view = logRowView(MULTI_LINE, '学三')
  assert.deepEqual(view.hiddenHits, [2], '命中在第 3 行（折叠时看不到）')
  assert.equal(view.revealForSearch, true)
  const render = logRowRender(view, false)
  assert.equal(render.full, true, '没有用户展开动作也要显示完整内容')
  assert.deepEqual(render.lines, view.fullLines)
  assert.deepEqual(render.hitLines, [2])
  assert.ok(render.lines[2].includes('学三'), '命中内容确实可见')
})

test('搜索命中首行：不强制展开，只在折叠态高亮', () => {
  const view = logRowView(MULTI_LINE, '地址无法')
  assert.deepEqual(view.visibleHits, [0])
  assert.deepEqual(view.hiddenHits, [])
  assert.equal(view.revealForSearch, false)
  const render = logRowRender(view, false)
  assert.equal(render.full, false)
  assert.deepEqual(render.hitLines, [0])
})

test('搜索命中多处（首行 + 多个隐藏行）：展开后逐行高亮，下标不串位', () => {
  const view = logRowView(MULTI_LINE, '地址')
  assert.deepEqual(view.visibleHits, [0])
  assert.deepEqual(view.hiddenHits, [1, 3], '第 2 行「原始地址」与第 4 行「待确认地址」都命中')
  const render = logRowRender(view, false)
  assert.equal(render.full, true)
  assert.deepEqual(render.hitLines, [0, 1, 3])
  for (const index of render.hitLines) {
    assert.ok(render.lines[index].includes('地址'), `第 ${index + 1} 行确实是命中行`)
  }
})

test('搜索大小写与首尾空白不影响命中；空搜索词不产生任何高亮', () => {
  const msg = 'ERROR 订单提交失败'
  assert.deepEqual(logRowView(msg, '  error  ').visibleHits, [0])
  assert.deepEqual(logRowView(msg, '').visibleHits, [])
  assert.deepEqual(logRowView(msg, '   ').visibleHits, [])
  assert.equal(logRowView(msg, '').wholeHit, false)
  assert.equal(normalizeLogQuery('  ERRor '), 'error')
})

test('跨字段命中：过滤按整条原文匹配，渲染整块标记，避免「筛出来看不出命中哪」', () => {
  // 搜索词里带全角分隔符：原文匹配得上，但拆成字段后没有任何一行包含它。
  const view = logRowView(ORDER_SUMMARY, '｜张')
  assert.deepEqual(view.visibleHits, [])
  assert.deepEqual(view.hiddenHits, [])
  assert.equal(view.wholeHit, true)
  const render = logRowRender(view, false)
  assert.equal(render.wholeHit, true)
  // 普通日志的正常命中不会误报成整块命中。
  assert.equal(logRowView(MULTI_LINE, '学三').wholeHit, false)
})

test('强制展开期间不给「收起」死按钮，改显示说明；用户自己展开的仍可收起', () => {
  const hit = logRowView(MULTI_LINE, '学三')
  assert.equal(logRowToggle(hit, false), 'forced', '命中隐藏行时是强制展开，收起按钮点了也没用')
  assert.equal(logRowToggle(hit, true), 'toggle', '用户主动展开过就允许收起')
  const plain = logRowView(MULTI_LINE, '')
  assert.equal(logRowToggle(plain, false), 'toggle')
  assert.equal(logRowToggle(plain, true), 'toggle')
  assert.equal(logRowToggle(logRowView(LONG_SINGLE_LINE, ''), false), 'none')
})

test('复制取完整原文：折叠不丢数据，订单摘要的分隔符也不被改写', () => {
  const rows = [
    { ts: '12:00:01', level: 'INFO', msg: MULTI_LINE },
    { ts: '12:00:02', level: 'OK', msg: ORDER_SUMMARY },
    { ts: '12:00:03', level: 'ERROR', msg: LONG_SINGLE_LINE },
  ]
  const text = logCopyText(rows)
  const lines = text.split('\n')
  assert.equal(lines[0], `12:00:01 INFO ${MULTI_LINE.split('\n')[0]}`, '多行日志原文逐行保留')
  assert.ok(text.includes('处理办法：在待确认地址里补全后重试，不要直接重跑本批'), '隐藏行也在复制结果里')
  assert.ok(text.includes(ORDER_SUMMARY), '订单摘要保留全角分隔符原文')
  assert.ok(text.includes(LONG_SINGLE_LINE), '长单行完整复制')
  assert.equal(lines.filter((line) => line.includes('W123456')).length, 1, '每条日志只出现一次')
})

test('折叠不影响数据：任何折叠态下完整内容都能还原原文', () => {
  for (const msg of [LONG_SINGLE_LINE, MULTI_LINE, ORDER_SUMMARY, '单行']) {
    const view = logRowView(msg, '')
    // 订单摘要的原文用全角分隔符连接字段；普通日志用换行连接。
    const restored = view.orderSummary ? view.fullLines.join('｜') : view.fullLines.join('\n')
    assert.equal(restored, msg, `原文可完整还原：${msg.slice(0, 20)}`)
    assert.equal(view.expandable, logRowToggle(view, false) !== 'none')
  }
  assert.deepEqual(logRowView('', '').fullLines, [''], '空消息不崩，渲染成一行空内容')
})
