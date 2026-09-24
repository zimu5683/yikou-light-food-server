# 一口轻食 Web 前端

React 19 + TypeScript + Vite 8 + Tailwind 4，构建为单文件产物供
`app/web/server.py` 静态提供。

## 开发

```bash
pnpm install
pnpm dev        # Vite 本地开发；无后端时会退化为 mock 数据
pnpm build      # 产物写入 frontend/dist/index.html
pnpm test       # Node 内置 test runner，覆盖 lib/ 纯函数与 bridge 事件去重
pnpm lint       # oxlint
pnpm check:anchors   # 变异锚点自检：mutation-check.mjs 每个 find 在目标文件里恰好命中一次
```

注意：Python 服务读取的是 `frontend/dist/index.html`，改完前端后必须重新
`pnpm build`；仓库根目录的 CI 也会执行 build/lint/test。

## 目录

- `src/lib/bridge.ts` — HTTP/桥接传输、事件分发与全部接口类型
- `src/lib/` — 格式化、主题、日志面板几何与扩散圆心等纯函数与 hooks
- `src/hooks/useApp.tsx` — 全局状态与后端事件到 React 的唯一入口
- `src/components/` — 业务组件；`components/ui/` 为无领域 UI 原子
- `src/App.tsx` — 手机/平板/桌面三种响应式布局

## 浏览器交互检查与变异检查

这两项**已接进 CI**（`.github/workflows/tests.yml`），不需要有 Chrome 的本地设备也能拿到结论：

- `browser-interaction-check.mjs`：frontend job 里每次 push / PR 都跑；
- `mutation-check.mjs`：单独一个 `mutation` job，只在 **PR** 与 **手动触发**（Actions 页 → Run workflow）
  时跑 —— 每个场景都要重建一次 dist 并完整跑一遍浏览器门禁，耗时明显更长。

CI 的 ubuntu runner 自带 `google-chrome`，脚本按 `$CHROME` → `google-chrome` → `chromium`
→ `chromium-browser` 顺序查找；换别的 Chromium 内核浏览器（Edge/Brave/Vivaldi）时用
`CHROME=/path/to/binary` 指过去即可（Firefox/Safari 不走 CDP，跑不了）。

本地手动跑：

```bash
pnpm build
node scripts/browser-interaction-check.mjs      # 合成 mock 后端 + 本机 Headless Chrome，退出码非 0 表示有断言失败
SCENARIO=uncertain-operation-as-success node scripts/mutation-check.mjs   # 只跑 FE-1（R6-8）变异
node scripts/mutation-check.mjs                 # 全部变异场景
```

- `scripts/browser-interaction-check.mjs`：只服务 `dist/index.html`，`/api/*` 全部是脚本内合成 JSON
  与请求计数；不连真实 WPS/闪时送、不读系统凭据、不写正式文件。断言基于真实渲染与真实点击。
  第 7 节专门覆盖手机端日志悬浮按钮：唯一入口、与标题栏/日志头部不重叠、水波圆心等于按钮中心、
  运行中自动展开后可退出、快速开关、减弱动效、动画 API 缺失/抛错、竖屏/横屏/安全区/软键盘；
  7f/7g 用**合成日志**（长单行 / 多行 / 订单摘要 / 错误行）覆盖折叠与展开不重复、搜索命中隐藏行、
  复制完整原文、运行中开关日志。
  第 8 节覆盖**手机端提示条（toast）与日志退出按钮**：成功/失败提示显示期间用 CDP 真实鼠标
  事件（走命中测试）点右上角按钮能立即开合、连续叠多条提示仍可退出、提示条不压日志头部
  搜索/工具入口与底栏主动作、窄屏+搜索展开与运行中长状态的最坏情况、横屏、刘海安全区、
  模态弹窗仍拦住背后的按钮、桌面提示条仍在右下角。
  加 `CAPTURE_MOBILE_TASK=1` 会额外产出手机任务页三页签截图与同屏重复计数
  （批 2 清单取证，产物是截图目录里的 mobile-task-matrix JSON），正常跑检查时不进入该段。
- `scripts/mutation-check.mjs`：把 `frontend/` 复制到临时目录（`node_modules` 走符号链接），在副本里
  注入变异并重新构建，要求「指定断言必须变成 FAIL、控制组必须仍然 PASS」，用来证明断言不是空转。
  日志相关变异：`SCENARIO=log-fab-not-wired`（按钮不再开合）、`SCENARIO=log-close-stuck-closing`
  （关闭收尾不落终态）、`SCENARIO=log-duplicate-expand`（可展开退回按长度判断 / 展开追加全文 /
  搜索不揭示隐藏命中 —— 三条都必须让 7f 的对应断言变成 FAIL）、`SCENARIO=toast-covers-log-fab`
  （提示条退回贴顶，必须让第 8 节的真实点击断言变成 FAIL）。
- FE-1（R6-8：uncertain 不得被渲染成“已完成”）的缺口、断言清单与三段式证据，见
  `scripts/browser-interaction-check.mjs` 的「5g FE-1 uncertain 权威状态」场景，以及
  `scripts/mutation-check.mjs` 的 `uncertain-operation-as-success` 变异。

## 手机端日志：右上角悬浮按钮 + 水波扩散

手机布局（<640px）下日志面板由**右上角**悬浮按钮 `components/LogFab.tsx` 开合
（`z-40`，始终浮在全屏日志层 `z-30` 之上）：展开时**圆形 clip-path 从这颗按钮的圆心
扩散**，再点同一颗按钮反向收回；运行中按钮角上一颗呼吸 LED。底栏不再有日志按钮 ——
只留「开始 / 停止」等业务动作：

- `lib/reveal.ts` — 纯函数：圆心表达式、半径、裁剪值、降级判定，以及按钮让位宽度
  `FAB_CLEARANCE`（标题栏第一行与日志头部据此在右侧让位，避免被 fixed 按钮压住）；
- `lib/useLogReveal.ts` — 开合状态机：谁触发、第几次展开；
- `components/LogConsole.tsx` — 全屏面板与开合动画；关闭有三级降级（无 WAAPI / 减弱动效 /
  `animate()` 抛错或收尾丢失），任何情况下都收得回，不会把用户困在日志层里；关闭只隐藏
  界面，不停止任务、不清日志。

触发点：右上角悬浮按钮；「开始处理/开始下单」真正跑起来后按权威运行状态自动展开
（`App.tsx`）；日志区出现待确认地址时自动展开。平板/桌面仍是日志常驻分栏，没有悬浮按钮。

手机端**提示条（toast）不漂浮**：`App.tsx` 在任务面板上方渲染一段
`.phone-notice-region`（`index.css` 把 sonner 的浮层覆盖成流式排布），提示条**占据正常
布局空间**，它下面的内容整体下移 —— 因此不会盖住标题栏/权威状态区、右上角日志按钮、
底栏「开始/停止」、待处理输入或确认弹窗（弹窗 `z-50` 仍在提示区域 `z-40` 之上）。
区域高度由 `ResizeObserver` 实测成 `--phone-notice-height` / `--phone-notice-bottom`，
全屏日志层据此把内容让到提示区域之下（见 `LogConsole.tsx`）。空闲时区域高度为 0、
布局与改动前一致。上限有两条：`45vh`（常规视口）与 `calc(100vh - 15rem)`
（极短视口的安全线：15rem ≈ 标题栏 + 任务面板固定 chrome，随字体缩放一起长），
超出则在区域内滚动 —— 横屏 320 高 + 1.25× 字体 + 多条提示时也不会把底栏挤出屏幕。
桌面/平板没有这个区域，仍是 sonner 默认的右下角浮层（行为不变）。

## 日志面板：常驻只留四样，低频操作收进菜单

头部默认只有**标题 + 一个任务状态 + 关闭入口（右上角悬浮按钮）+ 日志正文**：

- 搜索是**图标入口**，点开才出现输入框；关闭搜索会同时清空筛选 —— 不留「看不见的筛选」
  让日志莫名其妙少一截；
- 级别筛选、自动滚动、复制、清理都在**工具菜单**里；警告/错误条数以角标留在菜单入口上
  （默认级别是「全部」，警告与错误**不默认过滤**）；
- 需要用户处理的**待确认地址输入**直接渲染在正文上方，验证码／未决状态由页面级弹窗承载，
  都不进菜单；
- 日志尾部不再有装饰标语。

折叠/展开规则在 `lib/logDisplay.ts`（纯函数，可单测）：**能不能展开只看「是否真的有隐藏内容」**，
不看字符串长度 —— 长单行直接显示完整内容、订单摘要逐字段本来就完整，两者都不给「展开明细」；
多行日志折叠只显示首行，展开是**原位替换**摘要（不是摘要 + 追加全文）；搜索命中被折叠的行时
强制展开并把第一条命中滚进可视区。

复制用 `lib/clipboard.ts`：优先 Clipboard API，非安全上下文／权限被拒时退回
`textarea + execCommand`，两条都不通时给出「长按手动选择」的下一步 —— 真机 WebView 上不会静默失效。

## 任务页：权威状态一处 + 闲置态去冗余

- **权威运行状态只在「任务工作台」头部**（状态胶囊 + 说明）。标题栏只留品牌／版本／当前任务类型／
  安全模式／主题入口；订单、闪时送、云文档三个页签内那份重复的 `operationView` Callout 已删除。
- 头部说明行由单行 `truncate` 改为**允许折行**（并保留 `title` 与 danger 时的 `alert` 语义）：
  页签内那份完整 Callout 删掉后，长失败原因必须仍然看得全。
- `FlowStrip` 改为**条件渲染**：只有真正在跑（`workerAlive` / `operationActive`）、待核对
  （`operationView.needsReview`）或失败（`operationView.key === 'error'`）时才出现 ——
  闲置态不再常驻一条流程条；流程条只保留「结果」这一步的状态，不重复头部说明。
- 云文档页删掉与头部／折叠区重复的 chrome：原始预览全文 `details`、「当前写入」状态项、
  「尚无预览」中性 Callout、底栏「预览编号 …」调试片段，以及高级设置上重复的 `notice`
  （顶部「先完成云同步准备」Callout 已经承载同一句话）。
- 长 Callout／确认弹窗 description 一律压到一句话；细则下沉到 `details`、摘要区或删除。
- 闪时送「本次执行方式」重复说明块已删除；模式选项自身的 description、主按钮文字、
  真实下单确认弹窗（含「可能扣款或占用余额」）与标题栏安全模式继续承载同一语义。
- 订单页「保存到系统凭据管理器」开关移入高级设置（与闪时送页一致）；默认值、保存语义、
  清除凭据行为均未改动。
- 恢复入口一律保留：`PendingInteractionRecoveryBanner`、WPS 恢复卡片与退场弹窗、
  名单预览失败 Callout 的「重新读取云端名单」、验证码／地址补录、上传单飞闸门。

对应门禁：`node scripts/browser-interaction-check.mjs` 的「批2 三页签状态矩阵」（三页签 ×
就绪／运行／成功／失败／待核对，断言权威状态只在头部一处、页签内无重复块、说明不被截断）
与「批2 草稿与高级设置」；变异场景 `batch2-duplicate-status-restored`、
`batch2-header-detail-truncated`、`batch2-remember-default-flipped` 证明这些断言不是空转。
`batch2-duplicate-status-restored` 的订单页锚点就锁在新的 `FlowStrip` 条件渲染上：
锚点失效时 `pnpm check:anchors` 会先失败。
`CAPTURE_MOBILE_TASK=1 CAPTURE_LABEL=before|after` 可产出前后对照截图矩阵。

`pnpm check:anchors`（`scripts/check-mutation-anchors.mjs`）解析 `scripts/mutation-check.mjs` 里
每个 `find:` 锚点（字符串字面量或常量名，模板字面量按 JS 语义解码转义），断言它在同一条目
`file:` 指向的目标文件里**恰好出现一次**：锚点拼错、重复或目标文件改名都会先在这里失败，
不用等浏览器门禁跑完。

接口字段与 Python 返回值的契约由仓库根目录 `tests/test_frontend_contract.py`
反向校验，改字段名时两边要一起改。
