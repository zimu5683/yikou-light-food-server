/**
 * 变异检查（mutation check）：证明新增的浏览器检查**真的能失败**，不是只靠单测。
 *
 * 每个场景都：
 * 1. 把 `frontend/` 复制到一个隔离的临时目录（`node_modules` 用符号链接，不复制、不安装依赖）；
 * 2. 在副本里注入变异（改真实源码，不是改测试）；
 * 3. 用副本自己的 vite 重新构建副本的 dist；
 * 4. 运行副本里的 `browser-interaction-check.mjs`（它从自己所在目录解析 dist，所以服务的是变异产物）；
 * 5. 断言：必须**失败**（退出码非 0），且指定的浏览器断言确实变成 FAIL，
 *    同时控制组断言仍然 PASS（证明页面没崩，失败是断言真的抓到了变异）。
 *
 * 场景：
 * - `uncertain-as-success`：把 `uncertain` 整条渲染链路改回"当成 success"（W6 回归）。
 * - `no-single-flight`：去掉单飞闸门的拒绝语义（上传/恢复连点重复提交回归）。
 *
 * 边界：只读原工作区；不 commit/reset/checkout/clean；不安装依赖；不访问真实平台。
 *
 * 运行：node frontend/scripts/mutation-check.mjs               # 跑全部场景
 *       SCENARIO=no-single-flight node frontend/scripts/mutation-check.mjs
 * 保留临时目录：KEEP_MUTANT=1 node frontend/scripts/mutation-check.mjs
 */
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { spawnSync } from 'node:child_process'
import { fileURLToPath } from 'node:url'
import assert from 'node:assert/strict'

const HERE = path.dirname(fileURLToPath(import.meta.url))
const FRONTEND = path.resolve(HERE, '..')
const WORKSPACE = path.resolve(FRONTEND, '..')

const RESULT_COUNTS = 'src/lib/resultCounts.ts'
const WPS_FAILURE = 'src/lib/wpsFailure.ts'
const SINGLE_FLIGHT = 'src/lib/singleFlight.ts'
const OPERATION_STATUS = 'src/lib/operationStatus.ts'
const MAIN_ENTRY = 'src/main.tsx'
const BROWSER_CHECK = 'scripts/browser-interaction-check.mjs'

/** 入口脚本的渲染调用：R8 页面异常注入的锚点。 */
const MAIN_RENDER = `createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <AppProvider>
      <App />
    </AppProvider>
  </StrictMode>,
)`

/**
 * R8：页面异常门禁的注入反证。
 *
 * 三种注入都只落在**隔离副本**的入口脚本里（`src/main.tsx`），页面 load 之后触发：
 * - `uncaught`：定时器里 throw → CDP `Runtime.exceptionThrown`（未捕获异常）；
 * - `rejection`：未处理的 Promise 拒绝 → CDP `Runtime.exceptionThrown`（Uncaught (in promise)）；
 * - `console-error`：`console.error` → CDP `Runtime.consoleAPICalled`（type=error）。
 *
 * 注入不改变任何 UI 行为：页面照常渲染、其它 UI 断言照常通过，
 * 但「R8 页面无未预期异常」这一条必须单独失败并让浏览器检查退出码非 0。
 */
const PAGE_ERROR_INJECTIONS = [
  {
    name: 'page-error-uncaught',
    description: '注入未捕获异常：R8 异常门禁必须失败，而 UI 断言仍通过',
    snippet: "    setTimeout(() => { throw new Error('R8 注入：未捕获异常') }, 50)",
  },
  {
    name: 'page-error-rejection',
    description: '注入未处理 Promise 拒绝：R8 异常门禁必须失败，而 UI 断言仍通过',
    snippet: "    setTimeout(() => { Promise.reject(new Error('R8 注入：未处理的 Promise 拒绝')) }, 50)",
  },
  {
    name: 'page-error-console-error',
    description: '注入 console.error：R8 异常门禁必须失败，而 UI 断言仍通过',
    snippet: "    setTimeout(() => { console.error('R8 注入：console.error 门禁', { injected: true }) }, 50)",
  },
]

const PAGE_ERROR_SCENARIOS = PAGE_ERROR_INJECTIONS.map((injection) => ({
  name: injection.name,
  description: injection.description,
  mutations: [
    {
      file: MAIN_ENTRY,
      name: `入口脚本注入 ${injection.name}`,
      find: MAIN_RENDER,
      replace: `${MAIN_RENDER}

// R8 注入（只存在于隔离副本）：页面每次加载后制造一个页面级异常，
// 用于验证「即使其它 UI 断言全绿，异常门禁也必须非零退出」。
window.addEventListener('load', () => {
${injection.snippet}
})`,
    },
  ],
  mustFail: [
    'R8 页面无未预期异常（未捕获异常 / 未处理拒绝 / console.error / 资源错误）',
  ],
  mustStillPass: [
    '布局无横向溢出 360x800',
    'Tab 方向键切换并聚焦',
    'R8 异常采集通道自检：订阅已生效且基线确有预期内页面错误被采集',
    'FE-1 uncertain 权威状态渲染“结果不确定 · 待核对”（真实 DOM）',
  ],
}))

/**
 * 变异：把 uncertain→success 的整条渲染链路改回「R6 之前的旧行为」。
 *
 * 只改 resultCounts 会被 wpsFailure 的失败卡片遮住，所以两层都翻转，
 * 才是真实的「把 uncertain 当成 success」回归。
 */
const UNCERTAIN_MUTATIONS = [
  {
    file: RESULT_COUNTS,
    name: 'rows_unknown 恒为 false，实际写入行数退回计划数',
    find: `  const rowsUnknown = result.rows_unknown === true
    || executionRaw?.rows_unknown === true
    || (execution !== null && execution.rowsVerified === null)
    || execution === null`,
    replace: '  const rowsUnknown = false',
  },
  {
    file: RESULT_COUNTS,
    name: 'verifiedRows 用计划行数顶替实际行数',
    find: '  const verifiedRows = execution?.rowsVerified ?? null',
    replace: '  const verifiedRows = execution?.rowsVerified ?? plan?.update ?? 0',
  },
  {
    file: RESULT_COUNTS,
    name: 'uncertain 渲染成「上传完成」（impliesComplete=true）',
    find: `  if (isUncertain(result)) {
    return {
      tone: 'warning',
      title: '上传结果不确定 · 待核对',
      impliesComplete: false,
      nextStep: result.next_action || '只读核对云端与日志，不要重新上传。',
    }
  }`,
    replace: `  if (isUncertain(result)) {
    return {
      tone: 'success',
      title: '上传完成',
      impliesComplete: true,
      nextStep: result.next_action || '',
    }
  }`,
  },
  {
    file: WPS_FAILURE,
    name: 'uncertain 不再产生失败视图（失败卡片不再遮挡“成功”）',
    find: `  if (status === 'uncertain') {
    return withGuards({
      ...base(code || 'uncertain', 'warning', '上传结果不确定 · 待核对',
        reason || '可能已写入但无法确认；不能当作成功，也不能当作零写入。',
        next),
      nextStep: next || '只读核对云端与日志，不要重新上传。',
      needsManualReconcile: true,
    })
  }`,
    replace: `  if (status === 'uncertain') {
    return null
  }`,
  },
]

/**
 * 变异（FE-1 / R6-8）：把**权威** uncertain 渲染成 success / “已完成”。
 *
 * 这是 R7 §5.4 实测的缺口：只改 `operationStatus.ts` 时，前端单测会失败，
 * 但浏览器检查原先 95 PASS 全绿。改完 `browser-interaction-check.mjs` 的
 * “5g. FE-1”场景后，这个变异必须让浏览器检查退出码非 0。
 */
const UNCERTAIN_OPERATION_MUTATIONS = [
  {
    file: OPERATION_STATUS,
    name: '权威 uncertain 渲染成“已完成 / success”',
    find: `    case 'uncertain':
      return resultView(modeLabel, 'uncertain', \`\${modeLabel}结果不确定 · 待核对\`, '无法确认执行结果；请先只读核对云端与日志，不要直接重试或重跑本批。', 'warning', true, nextAction, reason, common)`,
    replace: `    case 'uncertain':
      return resultView(modeLabel, 'success', \`\${modeLabel}已完成\`, '任务已正常结束，可查看结果与日志。', 'success', false, nextAction, reason, common)`,
  },
]

const SCENARIOS = [
  {
    name: 'uncertain-operation-as-success',
    description: '把权威操作状态 uncertain 渲染成“已完成 / success”（R6-8 / FE-1 回归）',
    mutations: UNCERTAIN_OPERATION_MUTATIONS,
    mustFail: [
      'FE-1 uncertain 权威状态渲染“结果不确定 · 待核对”（真实 DOM）',
      'FE-1 uncertain 权威状态渲染“不要重试/重跑”防重复提示',
      'FE-1 uncertain 权威状态区不出现“已完成/成功”完成文案',
      'FE-1 uncertain 整页不出现“已完成/上传完成”等误导性完成文案',
      'FE-1 uncertain 流程条“结果”步骤不显示成功勾选（仍是待核对步骤）',
      'FE-1 uncertain 日志面板展开后仍然显示不确定状态（不显示已完成）',
      'FE-1 uncertain 390x844 窄屏：显示“结果不确定 · 待核对 + 不要重试/重跑”且无完成文案/无横向溢出',
      'FE-1 uncertain 800x450 横屏：显示“结果不确定 · 待核对 + 不要重试/重跑”且无完成文案/无横向溢出',
      'FE-1 uncertain 360x800 更窄：显示“结果不确定 · 待核对 + 不要重试/重跑”且无完成文案/无横向溢出',
      'FE-1 uncertain 断线恢复后回到“结果不确定 · 待核对”，且不会自动重传',
      'FE-1 uncertain 云文档页签同样渲染“结果不确定 · 待核对”',
      'FE-1 uncertain 刷新后权威状态仍是“结果不确定 · 待核对”',
    ],
    mustStillPass: [
      '布局无横向溢出 360x800',
      'Tab 方向键切换并聚焦',
      'FE-1 弹窗可见性判据：收起的日志面板不算可见弹窗（尺寸/visibility/display/opacity/inert/命中测试）',
      'FE-1 弹窗可见性判据：日志面板收起后不再被判为可见弹窗',
      'FE-1 uncertain 单击主要动作不触发危险执行（0 次下单/上传请求）',
      'FE-1 uncertain 危险执行按钮在勾选明确确认句之前不可点击',
      'FE-1 uncertain 状态下“停止”不可点击且不发 stop_task',
      'FE-1 running 只有弹窗内显式确认才提交停止（且只提交一次）',
      'FE-1 uncertain 刷新页面不会自动重传（危险请求 0 次）',
    ],
  },
  {
    name: 'uncertain-as-success',
    description: '把 uncertain 整条渲染链路改回「当成 success」',
    mutations: UNCERTAIN_MUTATIONS,
    /**
     * 这些浏览器断言在变异后**必须**变成 FAIL。名字与
     * browser-interaction-check.mjs 一致。
     */
    mustFail: [
      'W6 uncertain 不显示“上传完成/已完成”',
      'W6 uncertain 机器可读语义：complete=false / rowsUnknown=true / 无已核实行数',
      'W6 uncertain 实际写入行数显示未知，不显示计划数冒充',
      'W6 uncertain 明示这不是已完成',
      'W6 计划 1 行 + 实际未核实：机器可读语义仍为 rowsUnknown=true / verifiedRows=unknown',
      'W6 计划 1 行 + 实际未核实：不得显示“已核实写入 1 行”',
      'F1：ok+success 但实际未核实时，卡片本身不得显示“上传完成”',
    ],
    mustStillPass: [
      '布局无横向溢出 360x800',
      'Tab 方向键切换并聚焦',
      'W6 noop 显示“无需写入”而不是上传完成',
    ],
  },
  {
    name: 'no-single-flight',
    description: '让单飞闸门不再拒绝在途中的重复提交（上传/恢复连点回归）',
    mutations: [
      {
        file: SINGLE_FLIGHT,
        name: 'begin() 不再拒绝在途请求（去掉了重复提交保护）',
        find: `  begin(): number | null {
    if (this.inFlight) return null
    this.inFlight = true
    this.seq += 1
    return this.seq
  }`,
        replace: `  begin(): number | null {
    this.inFlight = true
    this.seq += 1
    return this.seq
  }`,
      },
    ],
    mustFail: [
      'F2 上传确认连点/双击只提交一次（单飞闸门）',
      'W3 双击/连点只提交一次且无并发',
    ],
    mustStillPass: [
      '布局无横向溢出 360x800',
      'W3 备注过短本地拦截，不发出请求',
    ],
  },
  ...PAGE_ERROR_SCENARIOS,
]

function log(message) {
  console.log(`[mutation-check] ${message}`)
}

function fail(message) {
  console.error(`[mutation-check] FAILED: ${message}`)
  process.exitCode = 1
}

function copyFrontend(destination) {
  fs.cpSync(FRONTEND, destination, {
    recursive: true,
    dereference: false,
    filter: (source) => {
      const rel = path.relative(FRONTEND, source)
      if (!rel) return true
      const head = rel.split(path.sep)[0]
      // node_modules 用符号链接；dist 由副本自己重新构建。
      return head !== 'node_modules' && head !== 'dist' && head !== '.git'
    },
  })
  fs.symlinkSync(path.join(FRONTEND, 'node_modules'), path.join(destination, 'node_modules'), 'dir')
}

function applyMutations(copyDir, mutations) {
  /** @type {Map<string, string>} */
  const sources = new Map()
  const read = (rel) => {
    if (!sources.has(rel)) sources.set(rel, fs.readFileSync(path.join(copyDir, rel), 'utf8'))
    return sources.get(rel)
  }
  for (const mutation of mutations) {
    const source = read(mutation.file)
    const count = source.split(mutation.find).length - 1
    assert.equal(count, 1, `变异锚点在副本里不唯一：${mutation.name}（命中 ${count} 次）`)
    sources.set(mutation.file, source.replace(mutation.find, mutation.replace))
    log(`已应用变异：${mutation.name}`)
  }
  for (const [rel, source] of sources) {
    fs.writeFileSync(path.join(copyDir, rel), source)
  }
}

function buildCopy(copyDir) {
  const vite = path.join(copyDir, 'node_modules', '.bin', 'vite')
  const result = spawnSync(vite, ['build'], { cwd: copyDir, encoding: 'utf8', timeout: 300_000 })
  const output = `${result.stdout || ''}${result.stderr || ''}`.trim()
  if (result.status !== 0) {
    throw new Error(`副本构建失败（exit ${result.status}）：\n${output.slice(-2000)}`)
  }
  assert.ok(
    fs.existsSync(path.join(copyDir, 'dist', 'index.html')),
    '副本 dist/index.html 未生成',
  )
  log('副本已重新构建（vite build）')
}

function runMutatedBrowserCheck(copyDir) {
  const script = path.join(copyDir, BROWSER_CHECK)
  const screenDir = fs.mkdtempSync(path.join(os.tmpdir(), 'yikou-mutant-screens-'))
  const result = spawnSync(process.execPath, [script], {
    cwd: WORKSPACE,
    encoding: 'utf8',
    timeout: 900_000,
    env: { ...process.env, UI_SCREEN_DIR: screenDir },
  })
  const output = `${result.stdout || ''}${result.stderr || ''}`
  return { status: result.status, output, screenDir }
}

function parseResults(output) {
  const marker = '__RESULTS__ '
  const at = output.lastIndexOf(marker)
  if (at < 0) return null
  const line = output.slice(at + marker.length).split('\n')[0]
  try {
    return JSON.parse(line)
  } catch {
    return null
  }
}

function runScenario(scenario) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), `yikou-mutation-${scenario.name}-`))
  const copyDir = path.join(root, 'frontend')
  log(`[${scenario.name}] ${scenario.description}`)
  log(`[${scenario.name}] 隔离副本：${copyDir}`)
  const problems = []
  try {
    copyFrontend(copyDir)
    applyMutations(copyDir, scenario.mutations)
    buildCopy(copyDir)

    const { status, output, screenDir } = runMutatedBrowserCheck(copyDir)
    fs.rmSync(screenDir, { recursive: true, force: true })

    const results = parseResults(output)
    if (!results) {
      console.error(output.slice(-4000))
      problems.push('变异副本的浏览器检查没有输出 __RESULTS__（脚本崩溃或找不到 Chrome）')
      return problems
    }

    const byName = new Map(results.map((item) => [item.name, item]))
    const stillPassing = scenario.mustFail.filter((name) => byName.get(name)?.ok !== false)
    const brokenControls = scenario.mustStillPass.filter((name) => byName.get(name)?.ok !== true)

    console.log('')
    console.log(`[${scenario.name}] 变异副本浏览器检查：exit=${status} pass=${results.filter((r) => r.ok).length} fail=${results.filter((r) => !r.ok).length}`)
    for (const name of scenario.mustFail) {
      console.log(`  ${byName.get(name)?.ok === false ? 'FAIL(预期)' : 'PASS(意外)'} ${name}`)
    }
    for (const name of scenario.mustStillPass) {
      console.log(`  ${byName.get(name)?.ok === true ? 'PASS(预期)' : 'FAIL(意外)'} ${name}`)
    }
    // R8：把变异副本里异常采集的摘要带出来（不含任何真实凭据/客户数据）。
    // 优先展示「未预期」条目，这样注入反证的证据里能直接看到被采集到的异常原文。
    const r8Lines = output.split('\n').filter((line) => line.startsWith('[R8]'))
    if (r8Lines.length > 0) {
      const summary = r8Lines.filter((line) => line.startsWith('[R8] 页面异常采集'))
      const unexpected = r8Lines.filter((line) => line.startsWith('[R8] 未预期'))
      const expected = r8Lines.filter((line) => line.startsWith('[R8] 预期内'))
      const shown = [...summary, ...unexpected.slice(0, 4), ...expected.slice(0, 1)]
      console.log(`  [${scenario.name}] 变异副本 R8 采集摘要：`)
      for (const line of shown) console.log(`    ${line}`)
      if (r8Lines.length > shown.length) console.log(`    …（其余 ${r8Lines.length - shown.length} 行见完整输出与脱敏日志 page-errors.json）`)
    }
    console.log('')

    if (status === 0) {
      problems.push('变异后浏览器检查仍然 exit=0：新增断言没有真正覆盖该回归')
    }
    if (stillPassing.length > 0) {
      problems.push(`变异后这些断言仍然 PASS，说明检查没有覆盖到：${stillPassing.join('；')}`)
    }
    if (brokenControls.length > 0) {
      problems.push(`控制组断言在变异副本里失败（页面可能崩溃，无法证明是断言抓到的）：${brokenControls.join('；')}`)
    }
    if (problems.length === 0) {
      log(`[${scenario.name}] 通过：${scenario.mustFail.length} 条浏览器检查全部变为 FAIL，退出码 ${status}`)
    }
    return problems
  } finally {
    if (process.env.KEEP_MUTANT === '1') {
      log(`[${scenario.name}] 保留临时目录：${root}`)
    } else {
      fs.rmSync(root, { recursive: true, force: true })
    }
  }
}

function main() {
  assert.ok(fs.existsSync(path.join(FRONTEND, 'dist', 'index.html')),
    '请先在 frontend/ 运行 pnpm build（变异检查以当前 dist 为对照基线）')

  const only = process.env.SCENARIO
  const scenarios = only ? SCENARIOS.filter((item) => item.name === only) : SCENARIOS
  assert.ok(scenarios.length > 0, `未知场景：${only}（可选：${SCENARIOS.map((s) => s.name).join(' / ')}）`)

  let failed = 0
  for (const scenario of scenarios) {
    const problems = runScenario(scenario)
    for (const problem of problems) fail(`[${scenario.name}] ${problem}`)
    if (problems.length > 0) failed += 1
    else console.log('')
  }
  log(`全部场景完成：${scenarios.length - failed}/${scenarios.length} 通过`)
}

main()
