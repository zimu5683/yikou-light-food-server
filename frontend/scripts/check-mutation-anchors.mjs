#!/usr/bin/env node
/**
 * 变异锚点自检（pnpm check:anchors）。
 *
 * 变异检查靠「把源码里的一段精确文本替换掉」注入回归：如果 find 锚点拼错、重复出现、
 * 或指向别的文件，变异要么静默不生效、要么改错位置，「变异必须让门禁失败」的证明
 * 就变成空转。本脚本把锚点唯一性单独钉死，见 frontend/scripts/mutation-check.mjs。
 *
 * 只读源码、不执行 mutation-check.mjs：
 * - 顶层 const 字符串常量（目标文件路径与 MAIN_RENDER）先建表；
 * - 每个行首 file: 与 find: 配对：find 的值可以是字符串字面量，也可以是常量名；
 * - 模板字面量按 JS 语义解码转义（换行、反引号、美元符等）；
 * - 含真实插值表达式的 find 无法静态解析，直接报错（锚点必须是死字符串）。
 *
 * 运行：node frontend/scripts/check-mutation-anchors.mjs
 */
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const HERE = path.dirname(fileURLToPath(import.meta.url))
const FRONTEND = path.resolve(HERE, '..')
const MUTATION_CHECK = path.join(HERE, 'mutation-check.mjs')
const SOURCE = fs.readFileSync(MUTATION_CHECK, 'utf8')

const LF = String.fromCharCode(10)
const ESCAPES = {
  n: LF,
  r: String.fromCharCode(13),
  t: String.fromCharCode(9),
  b: String.fromCharCode(8),
  f: String.fromCharCode(12),
  v: String.fromCharCode(11),
  '0': String.fromCharCode(0),
  "'": "'",
  '"': '"',
  '`': '`',
  '\\': '\\',
  '$': '$',
}

const HEX4 = /^[0-9a-fA-F]{4}$/
const HEX2 = /^[0-9a-fA-F]{2}$/

/** 解析从 start 开始的字符串字面量；返回 { value } 或 { error }，非字面量返回 null。 */
function parseLiteral(text, start) {
  const quote = text[start]
  if (quote !== "'" && quote !== '"' && quote !== '`') return null
  let value = ''
  let index = start + 1
  while (index < text.length) {
    const char = text[index]
    if (char === '\\') {
      const next = text[index + 1]
      if (next === undefined) return { error: '字面量以反斜杠结尾' }
      if (next === 'u') {
        const hex = text.slice(index + 2, index + 6)
        if (!HEX4.test(hex)) return { error: '非法的 unicode 转义' }
        value += String.fromCharCode(parseInt(hex, 16))
        index += 6
        continue
      }
      if (next === 'x') {
        const hex = text.slice(index + 2, index + 4)
        if (!HEX2.test(hex)) return { error: '非法的十六进制转义' }
        value += String.fromCharCode(parseInt(hex, 16))
        index += 4
        continue
      }
      if (next === LF) { index += 2; continue }
      if (Object.prototype.hasOwnProperty.call(ESCAPES, next)) {
        value += ESCAPES[next]
        index += 2
        continue
      }
      return { error: '未知转义序列' }
    }
    if (quote === '`' && char === '$' && text[index + 1] === '{') {
      return { error: '锚点含插值表达式，无法静态解析' }
    }
    if (char === quote) return { value, end: index + 1 }
    value += char
    index += 1
  }
  return { error: '字符串字面量没有闭合' }
}

/** 顶层 const 字符串常量表（file: 目标与 MAIN_RENDER 都用它解析）。 */
function collectConstants(text) {
  const constants = new Map()
  const pattern = /^const +([A-Za-z_$][A-Za-z0-9_$]*) *= */gm
  let match
  while ((match = pattern.exec(text)) !== null) {
    const parsed = parseLiteral(text, pattern.lastIndex)
    if (parsed && typeof parsed.value === 'string') constants.set(match[1], parsed.value)
  }
  return constants
}

const CONSTANTS = collectConstants(SOURCE)

/** 行首 KEY: 的值位置。 */
function entries(text, key) {
  const pattern = new RegExp('^ *' + key + ': *', 'gm')
  return [...text.matchAll(pattern)].map((match) => ({
    index: match.index,
    valueStart: match.index + match[0].length,
  }))
}

function resolveToken(text, start, what) {
  const parsed = parseLiteral(text, start)
  if (parsed) {
    if (parsed.error) throw new Error(what + '：' + parsed.error)
    return parsed.value
  }
  const identifier = text.slice(start).match(/^[A-Za-z_$][A-Za-z0-9_$]*/)
  if (!identifier) throw new Error(what + '：无法识别的字面量')
  if (!CONSTANTS.has(identifier[0])) throw new Error(what + '：未知常量 ' + identifier[0])
  return CONSTANTS.get(identifier[0])
}

/** 取 find 前面最近的 name: 作为可读标签。 */
function describeName(text, findIndex) {
  const pattern = /^ *name: */gm
  let label = ''
  let match
  while ((match = pattern.exec(text)) !== null) {
    if (match.index > findIndex) break
    const parsed = parseLiteral(text, pattern.lastIndex)
    label = parsed && typeof parsed.value === 'string'
      ? parsed.value
      : text.slice(pattern.lastIndex).split(LF)[0].trim().replace(/[',]+$/, '')
  }
  return label
}

const files = entries(SOURCE, 'file')
const finds = entries(SOURCE, 'find')

console.log('[check-anchors] mutation-check.mjs：' + finds.length + ' 个 find 锚点 / ' + files.length + ' 个 file 目标')

if (finds.length === 0) {
  console.error('[check-anchors] FAILED：没有解析到任何 find: 锚点（mutation-check.mjs 的结构变了？）')
  process.exit(1)
}

let passed = 0
const failures = []

finds.forEach((find, position) => {
  const ordinal = String(position + 1).padStart(2, '0') + '/' + String(finds.length).padStart(2, '0')
  const label = ordinal + ' 「' + describeName(SOURCE, find.index) + '」'
  try {
    const targetEntry = [...files].reverse().find((entry) => entry.index < find.index)
    if (!targetEntry) throw new Error('前面没有 file: 目标')
    const relative = resolveToken(SOURCE, targetEntry.valueStart, 'file 目标')
    const anchor = resolveToken(SOURCE, find.valueStart, 'find 锚点')
    if (!anchor) throw new Error('find 锚点是空字符串')
    const absolute = path.resolve(FRONTEND, relative)
    if (!absolute.startsWith(FRONTEND + path.sep)) throw new Error('目标文件越出 frontend/：' + relative)
    if (!fs.existsSync(absolute)) throw new Error('目标文件不存在：' + relative)
    const haystack = fs.readFileSync(absolute, 'utf8')
    const count = haystack.split(anchor).length - 1
    if (count !== 1) throw new Error('在 ' + relative + ' 出现 ' + count + ' 次（要求恰好 1 次）')
    passed += 1
    console.log('[check-anchors] OK  ' + label + ' → ' + relative + ' ×1')
  } catch (error) {
    failures.push(label + ' → ' + error.message)
    console.error('[check-anchors] FAIL ' + label + ' → ' + error.message)
  }
})

if (failures.length > 0) {
  console.error('[check-anchors] 失败：' + passed + '/' + finds.length + ' 个锚点唯一命中，' + failures.length + ' 个不通过')
  process.exitCode = 1
} else {
  console.log('[check-anchors] 通过：' + passed + '/' + finds.length + ' 个锚点在目标文件中恰好出现 1 次')
}
