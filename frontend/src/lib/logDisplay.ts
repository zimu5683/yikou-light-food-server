/**
 * 日志行的展示模型：折叠时到底看得见什么、展开后替换成什么、搜索命中在哪一行。
 *
 * **为什么单独成模块**：这套判断原先写在 `components/LogConsole.tsx` 里，规则是
 * `lines.length > 1 || row.msg.length > 120` —— **只看字符串长度**。它带来三类重复展示：
 *
 * 1. 长单行日志（>120 字但只有一行）也显示「展开明细」，点开是同一行的副本；
 * 2. 订单摘要（`W123｜张三｜小｜已下单`）本来就逐字段显示完整，仍然提供「展开明细」，
 *    点开是全文的第二份；
 * 3. 展开是**追加**在摘要下面的（摘要 + `<pre>全文</pre>`），同一份内容同屏出现两次。
 *
 * 这里的规则改成「折叠态是否真的漏掉了内容」：
 * - 订单摘要：字段全展示 → 永不折叠；
 * - 普通日志：折叠态 = 首行 → 只有确实存在**非空**的后续行才可展开；
 * - 长单行：自动换行就能看全 → 不可展开（长度不再是依据）；
 * - 搜索命中被折叠的行 → 强制展开，命中内容必须可见。
 *
 * 纯函数、无 React/DOM：`node --test` 可直接覆盖（`.tsx` 不能）。
 */
import { isOrderSummary, splitOrderSummary } from './format.ts'

/** 搜索词归一化：过滤、命中判断、高亮共用同一口径。 */
export function normalizeLogQuery(query: string): string {
  return query.trim().toLowerCase()
}

/**
 * 普通日志按行拆开。
 *
 * 统一 CRLF；去掉**尾部**空行 —— 它们不是「被折叠的内容」，留着会让一条
 * `"a\n"` 之类的日志误判成「有隐藏内容」而多出一个展开按钮。
 * 中间的空行保留（那是排版信息）。
 */
function toLines(msg: string): string[] {
  const lines = msg.replace(/\r\n?/g, '\n').split('\n')
  while (lines.length > 1 && lines[lines.length - 1].trim() === '') lines.pop()
  return lines
}

function hitIndexes(lines: string[], needle: string): number[] {
  if (!needle) return []
  const out: number[] = []
  lines.forEach((line, index) => {
    if (line.toLowerCase().includes(needle)) out.push(index)
  })
  return out
}

export interface LogRowView {
  /** 折叠时渲染的行：订单摘要 = 全部字段；普通日志 = 首行。 */
  visibleLines: string[]
  /** 完整内容（行）。折叠/展开都从这里取，保证不丢数据。 */
  fullLines: string[]
  /** 折叠时是否真的还有看不到的内容。false = 不提供展开入口。 */
  expandable: boolean
  /** 折叠可见行里命中搜索的下标（相对 visibleLines）。 */
  visibleHits: number[]
  /** 折叠隐藏行里命中搜索的下标（相对 fullLines）。 */
  hiddenHits: number[]
  /** 命中隐藏行 → 必须展开，否则命中内容被折叠藏住。 */
  revealForSearch: boolean
  /**
   * 整条消息命中、但没有任何**单行**命中（搜索词跨行或跨 `｜` 字段）。
   * 过滤是按整条原始消息匹配的，这种命中也得有可见反馈，否则「筛出来了却看不出命中哪」。
   */
  wholeHit: boolean
  /** 订单摘要行：逐字段展示，本来就完整。 */
  orderSummary: boolean
}

/**
 * 计算一条日志的折叠/展开/命中模型。
 *
 * `query` 允许是未归一化的原始输入（内部会 trim + 转小写）。
 */
export function logRowView(msg: string, query: string): LogRowView {
  const orderSummary = isOrderSummary(msg)
  const fullLines = orderSummary ? splitOrderSummary(msg) : toLines(msg)
  // 订单摘要把每个字段都渲染出来，没有折叠隐藏的内容。
  const visibleLines = orderSummary ? fullLines : fullLines.slice(0, 1)
  const expandable = !orderSummary && fullLines.slice(1).some((line) => line.trim() !== '')
  const needle = normalizeLogQuery(query)
  const visibleHits = hitIndexes(visibleLines, needle)
  const hiddenHits = expandable
    ? hitIndexes(fullLines.slice(1), needle).map((index) => index + 1)
    : []
  const anyLineHit = visibleHits.length > 0 || hiddenHits.length > 0
  return {
    visibleLines,
    fullLines,
    expandable,
    visibleHits,
    hiddenHits,
    revealForSearch: hiddenHits.length > 0,
    wholeHit: needle !== '' && !anyLineHit && msg.toLowerCase().includes(needle),
    orderSummary,
  }
}

export interface LogRowRender {
  /** 当前该渲染的行。 */
  lines: string[]
  /** 命中搜索的行下标（相对 lines）；wholeHit 时为空数组。 */
  hitLines: number[]
  /** true = 正在展示完整内容（展开态）；false = 折叠摘要。 */
  full: boolean
  wholeHit: boolean
}

/**
 * 折叠/展开到底渲染哪些行。
 *
 * 展开是**替换**摘要（`lines = fullLines`），不是把全文追加在摘要下面 ——
 * 后者会让同一份内容同屏出现两次，正是要修掉的重复展示。
 * 搜索命中隐藏行时强制展开：命中内容必须可见，不能被折叠藏住。
 */
export function logRowRender(view: LogRowView, expanded: boolean): LogRowRender {
  const full = view.expandable && (expanded || view.revealForSearch)
  return {
    lines: full ? view.fullLines : view.visibleLines,
    hitLines: full
      ? [...view.visibleHits, ...view.hiddenHits].sort((a, b) => a - b)
      : view.visibleHits,
    full,
    wholeHit: view.wholeHit,
  }
}

/**
 * 展开入口该显示什么。
 *
 * - `none`：没有隐藏内容 → 不渲染按钮（短日志、长单行、订单摘要都是这一类）；
 * - `forced`：搜索命中隐藏行、自动展开中 → 显示说明而不是按钮，避免出现
 *   「点了没反应」的死控件（强制展开期间收起按钮本就无效）；
 * - `toggle`：用户可自由展开/收起。
 */
export function logRowToggle(view: LogRowView, expanded: boolean): 'none' | 'forced' | 'toggle' {
  if (!view.expandable) return 'none'
  if (view.revealForSearch && !expanded) return 'forced'
  return 'toggle'
}

/**
 * 复制用文本：逐条**完整原文**（不折叠、不改分隔符）。
 *
 * 刻意不做 `formatLogMsg` 的 `｜` → 换行改写：复制是取数据，不是渲染，
 * 改写分隔符会丢掉原文结构。
 */
export function logCopyText(rows: ReadonlyArray<{ ts: string; level: string; msg: string }>): string {
  return rows.map((row) => `${row.ts} ${row.level} ${row.msg}`).join('\n')
}
