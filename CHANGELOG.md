# 更新记录

只记录**已发布**的版本；开发过程资料见 `design/archive/`（历史，含逐轮迭代日志），
当前手册与规则见 `README.md` 与 `docs/`。

## 3.6.19

一次「回退跨运行阻断 + 补上诊断日志导出」的版本：移除 3.6.17 引入的「人工核对 +
打确认字才能重跑」流程（重跑现在直接可用，提交前仍会站内对账去重），并新增应用内的
闪时送诊断日志查看/复制/保存。

### 闪时送：移除跨运行阻断与人工核对流程

- **不再阻断**：存在历史「已发送未知」记录、跨作用域无法归属的记录、权威位置登记
  冲突，或 journal/登记文件损坏、不可读时，新的运行**照常提交**（日志里只打印
  「提示：…本批继续提交 / 不阻断本批」）。运行开始时的只读对账保留：站内已出现的
  记录仍会自动标记 resolved 并清理；查不到的记录原样保留（仅作排查账本），旧的
  迁移/镜像来源文件不被改写、删除。
- **删除接口与界面**：`sss_uncertain_records` / `sss_uncertain_resolve` /
  `start_sss_review` 三个管理员接口、只读核对 worker（`run_sss_review_job`）与界面
  「未决记录」面板（勾选 + 备注 + 逐字确认 + 二次确认）全部移除；未决记录相关前端
  库与契约测试同步删除。
- **文案**：批次结束后的下一步统一改为「可直接再运行一次补单：重跑前会先做站内
  对账，已存在的订单不会重复提交；本批不会自动重发」，不再出现「禁止重试或重跑」；
  “订单未确认”逐单提示同步改写。
- **保留的保护**：同一权威 journal + 同一批次的跨进程提交锁（并发第二个进程
  `blocked_concurrent`、不产生 POST）；提交前只读对账；运行内绝不自动重发。

### 新增：闪时送诊断日志查看/导出

- 新增只读接口 `sss_diagnostics_files`（列出 `sss-diagnostics/*.jsonl`）与
  `sss_diagnostics_read`（按文件名读取；仅允许该目录内的 .jsonl，单次最多 1 MiB，
  超出只返回尾部并对齐行边界）。仅管理员可用（与「更多工具」整组同权限）。
- 界面入口：闪时送页「更多 → 闪时送诊断日志」：文件列表（日期 + 大小）、内容查看、
  「复制全部」（WebView 里最稳）与「保存 .jsonl」（浏览器/桌面下载能力；不可用时
  提示改用复制）。
- 新增前端纯函数库 `frontend/src/lib/sssDiagnostics.ts`（文件名校验与后端同口径、
  大小格式化、截断提示）+ node:test 单测。

### 测试与文档

- 守卫与反例体系按新契约重写：位置切换 / 跨作用域 / 损坏文件不再阻断（改为锁定
  「照常提交 + 旧文件字节不变 + 记录保留」），批次锁仍拦住同批次并发双发，站内可见
  时提交前对账保证恰好一次；探针 21 个场景全部 SAFE，变异门禁 5/5（重新锚定到新可
  观测量），独立反例子进程用例同步改写。
- 删除核对功能相关测试（`test_sss_uncertain_review*.py`、前端契约测试）；
  `docs/SSS-不确定记录处置.md` 改写为「已下线」现状速查；README 与
  `docs/SSS-下单失败与重试排查.md` 同步更新。

### 验证

- `python -m pytest -q`：**1549 passed**；`ruff check app tests scripts` 通过；
  `python -m compileall -q app` 与 `git diff --check` 通过。
- 前端：`pnpm build` / `pnpm lint`（0 警告）/ `pnpm test`（210）/
  `pnpm check:anchors`（18/18）与 headless Chrome 浏览器门禁（本机）全部通过。
- 未改动闪时送提交节流与对账参数。

### 已知问题（如实记录，本版未改）

- 3.6.18 新增的提交最小间隔节流在 2026-10-09 晚的实测批次中**未能消除批量驳回**：
  间隔自动放宽到上限 10 秒后，平台仍对约 3/4 的请求返回
  `java.lang.IndexOutOfBoundsException`（当晚 99 单成功 26）。该现象与发送间隔无关
  （放宽间隔不改变失败比例），根因与后续处置另行排查。

## 3.6.18

一次「把建单发送节奏压回平台受理上限之内」的版本：定位并修复 2026-10-09
「每 3 单成功 1 单、失败单烧平台序列号」的批量驳回，新增提交最小间隔节流与
平台超速特征的自适应放宽。现场数据见
[docs/SSS-下单失败与重试排查.md](docs/SSS-下单失败与重试排查.md) 的 2026-10-09 章。

### 闪时送：提交最小间隔节流（新增配置）

- **根因（2026-10-09 现场）**：平台对同一账号的建单受理上限约 **1 单 / 2.1 秒**。
  4 路滚动补位下，被驳回的请求 0.36 秒即回、车道立刻补发，实际发送节奏被推到约
  1.37 单/秒 ≈ 3 倍超速；超速请求被平台以快速内部异常驳回（HTTP 200 + code=500 +
  `java.lang.IndexOutOfBoundsException: Index: 0, Size: 0`，约 0.3-1 秒返回），且
  **仍消耗一个平台订单序列号**——实测实际订单不到 100 单而序列号到 200 多
  （五轮合计 238 次请求 ≈ 238 个序列号），失败单成为不可见、不扣费的「幽灵订单」。
  逐单着色统计：单轮 95 单成功 33、后续四轮成功率恒约 1/3，成功位置严格
  「每 3 单 1 单」；33 个成功 × 约 2.1 秒 ≈ 本轮 69.2 秒耗时，成功几乎严格按平台
  节奏铺满全程；同一订单首轮失败、次轮成功（payload 相同），证明与订单内容无关，
  只与发送节奏有关（也与 10-08「网页手动失败、单单重试成功」的现场一致）。
- **修复**：新增配置 `sss_submit_min_interval_s`（秒，出厂 **2.5**，0 = 关闭节流；
  无界面入口，改 config.json 生效）。提交器在每次 POST 前经 `_SubmitPacer` 统一
  排队，相邻两次 POST 的起点至少间隔该值；并发路数不变（4 路 + 2.5 秒间隔最多约
  3 个请求在途，低于平台上限）。95 单预计约 4 分钟跑完。
- **自适应放宽**：命中平台超速特征（HTTP 200 内部异常且含 `IndexOutOfBounds`、
  耗时 ≤1.5 秒）时间隔 ×1.5（上限 10 秒）并打印一行提示；平台若再收紧，程序会
  自己慢下来。只影响节奏，不改变任何结果分类与「不自动重发」语义。
- **未发送语义**：节流等待期间出现停止/401/余额不足的任务记为「未发送」（请求确实
  没有发出），重登后仍按未发送语义允许补发。
- **可观测**：`开始下单：…，提交最小间隔 2.5s`；本轮汇总行追加 `；提交最小间隔
  X 秒`；每条诊断新增 `min_interval_s` 字段（该请求发出时生效的间隔）。
- **回退**：把 `sss_submit_min_interval_s` 配成 0 即完全关闭节流（恢复旧行为）。

### 测试与文档

- 新增回归：最小间隔拉开 POST 起点、0 = 关闭节流保持旧行为、平台快速驳回自动放宽、
  `_SubmitPacer` 上限与停止中止、超速特征不误伤显式失败、诊断字段、配置夹紧/NaN/
  旧配置加载默认值/显式 0 往返保留、`resolve_submit_min_interval_s` 兜底解析。
- `design/SSS-对账提速与并发.md` 修正旧结论：4 路实测的 0.44-0.48 单/秒正是平台
  受理上限（当时贴着上限未越线），且 4/8/16/32 路压测只压了「校验失败」分支，
  不能代表真实建单路径。

### 验证

- `python -m pytest -q`：**1608 passed / 1 xpassed**（含本版新增用例）。
- 独立反例探针 `tests/independent_final_counterexample_probe.py` 全部场景 BLOCKED
  （预期）；`python -m compileall -q app` 与 `git diff --check` 通过。
- 未改动前端；干跑/预检路径不受节流影响（不产生 POST）。

### 发布补记：CI 验证、出包与发布结果

- main 推送（`2639a66`）后 CI 全绿：Tests **1608 passed / 1 xpassed**（Python 3.11 与
  3.13 各一遍，含 `ruff check app tests scripts`「All checks passed!」、前端构建/lint、
  锚点唯一性与 headless Chrome 浏览器门禁、Workspace hygiene；`mutation` 作业按设计
  只在 PR/手动触发时运行，本次跳过），Android APK（main）出包成功。相关 run：
  Tests 37882785144、Android APK（main）37882785058。
- tag `v3.6.18` 触发的发布流水线（run 37883489473）成功：**Release v3.6.18 已发布**
  （https://github.com/zimu5683/yikou-light-food-server/releases/tag/v3.6.18），
  APK `yikou-light-food-3.6.18-arm64.apk` **19,836,741 字节**，sha256
  `99d5ad4c…cce6dab8`（资产内 `.sha256` 与出包日志一致）。
- 本机网络对 `github.com:443` 继续间歇性握手失败：`git push` 均在数次重试内完成
  （main 第 2 次、tag `v3.6.18` 第 1 次成功），未使用 API 镜像。仅影响推送通道，
  不改动仓库内容。

## 3.6.17

一次「落单证据收紧 + 可取证」的版本：闪时送下单失败不再凭错误文字认定事务已回滚，
提交诊断改为脱敏 JSONL，并修复跨餐任务标识与工作线程登录上下文两处缺陷。
现场事实、离线复现与报文核对见 [docs/SSS-下单失败与重试排查.md](docs/SSS-下单失败与重试排查.md)。

### 闪时送：失败分类收紧（技术异常不再可重发）

- **内部异常不算“明确拒绝”**：响应体里带 Java 异常/堆栈（`exception` 字段或 message
  含 `…Exception`/`…Error`/`系统异常` 等）一律保持“已发送未知”，只做只读对账，
  不再进入“可以直接补发”的人工重试决策；仅有错误码、或 `success=false` 带泛化
  文案（如“参数错误”“系统繁忙”）同样不确定。
- **服务端错误码不算业务失败**：`success=false` 且 `code`/`errorCode` 在 500–599，
  以及 POST 收到 HTTP 429 与**全部 5xx**（原先是固定集合 429/500/502/503/504）
  都归类为不确定，保留前置记录。
- **显式拒绝改为白名单**：只有可识别的参数校验拒绝（地址无效 / 地址必填 / 手机号
  无效 / 姓名、门牌号、商品等必填缺失）才判“明确未落单”，对账后可单单重试；
  余额不足（`余额不足` / `欠费` / `insufficient balance`）单独识别为余额类。
- **内部异常不再误判为登录失效**：`token` 字样出现在 Java 异常文本里时不再触发
  重登路径（401 恢复只认真正的登录态失效报文），测试里的同类用例同步改为 401。

### 闪时送：跨餐任务标识冲突

- 任务标识从「第 N 行 姓名」改为含子表名（`午餐第 63 行 姓名`）。原先午餐、晚餐
  同行同名共用一个标识，会共用 `task_by_id` 与状态集合，可能把另一餐的成功当作
  本餐确认、或清掉另一餐的未决记录；客户端 `client_request_id` 生成材料未变，
  历史 journal 仍按原 UUID 核对。

### 闪时送：工作线程补登录上下文与请求头

- `fork()` 除 token 外现在复制登录 Session 的请求头与 Cookie，每个 worker 仍有独立
  Session/CookieJar（不跨线程共享可变会话）；建单请求补 `terminal: web` 头，与网页端
  请求拦截器一致（它是 HTTP header，不是 JSON 字段）。
- 恢复已确认/重试成功后清除该单的失败计数与错误文案，不再同时报“已确认”和旧失败；
  重登后对账确认的订单一并计入最终 confirmed。

### 新增：脱敏提交诊断

- 每次真实提交写一条 JSONL 到用户数据目录 `sss-diagnostics/YYYY-MM-DD.jsonl`（目录
  0700、文件 0600、`O_NOFOLLOW`）：批次/本地请求标识、餐次与行号、开始时间、耗时、
  并发路数、分类、HTTP 状态、响应 `success`/`code`、技术异常类名，以及请求 payload
  与实际发出 HTTP body 的 sha256 前 16 位摘要。
- **不保存**凭据、姓名、手机号、地址、原始请求体或响应正文；token 与 Cookie 只记录
  用进程内随机密钥生成的 HMAC 上下文摘要（仅用于同一进程内比较是否变化）。
- 失败诊断同时写入界面运行日志（`提交诊断：{…}`），本轮有提交时日志末尾给出诊断文件
  路径；写诊断失败只给警告，不改变订单结果——防重复仍由前置 journal 与只读对账负责。

### 测试与文档

- 新增 `tests/test_sss_submission_regressions.py`（内部异常不重发、未决记录跨运行保留、
  跨餐同名同行不互相确认、恢复后清理失败状态、Cookie 独立复制、全部 5xx、诊断脱敏
  与写失败不改变结果）。
- `tools/r7-acceptance/mutation_check_r6_4.py` 的 m6 变异锚点随 `_post_one` 新结构更新；
  `tests/test_independent_final_counterexamples.py` 的 2xx 分类用例改为“泛化文案仍
  不确定、可识别校验拒绝才显式”，与新行为一致。
- 新增 [docs/SSS-下单失败与重试排查.md](docs/SSS-下单失败与重试排查.md)（现场事实、
  离线复现的问题、网页报文核对、诊断字段与取证方法），README 增加指引。

### 验证

- `python -m pytest -q`：**1582 passed / 1 xpassed**。本机另有 5 项环境性失败
  （4 项 WPS 多进程信号量 `PermissionError`、1 项缺 `cryptography` 模块），在未改动的
  `HEAD` 基线上同样失败，与本版无关；CI（ubuntu）不受影响。
- 变异门禁 `test_r6_4_mutations_force_probe_defect_before_injected_restored` 与反例探针
  全绿：m6 锚点随 `_post_one` 新结构更新后，注入仍被判 DEFECT、恢复后通过。
- `python -m compileall -q app tests scripts tools` 通过；`pyflakes` 对改动文件无告警
  （本机 Android 运行时装不了 ruff 的 wheel，ruff 门禁以 CI 结果为准）。
- 未改动前端；`git diff --check` 无空白错误。

### 发布补记：CI 验证与出包结果

- main 推送后 CI 全绿：Tests **1587 passed / 1 xpassed**（Python 3.11 与 3.13 各一遍，
  含 `ruff check app tests scripts`「All checks passed!」、前端构建/lint/浏览器门禁与
  Workspace hygiene），Android APK（main）出包成功。
- tag `v3.6.17` 触发的发布流水线（run 37723744276）成功：**Release v3.6.17 已发布**，
  APK `yikou-light-food-3.6.17-arm64.apk` **19,832,197 字节**，sha256
  `3901b71d…68f610`（资产内 `.sha256` 与出包日志一致）。相关 run：Tests 37723655053、
  Android APK（main）37723654958。
- 本机网络对 `github.com:443` 超时（`git push` 不可用），本次提交与标签改走
  `api.github.com` 的 Git Data API 镜像：按本地对象逐个建 blob/tree/commit/tag，
  每一步都比对 SHA，远端 `refs/heads/main` = `1fd9cfe`、tag 对象 `edcea13f`
  与本地逐字一致。仅影响推送通道，不改动仓库内容。

## 3.6.16

一次「与桌面版（`yikou-light-food-desktop` v3.6.1）对齐」的版本：补齐杭电信工校区，
闪时送创建订单并发出厂回到 4 路，并回移两处桌面版更严的安全/报告行为。

### 杭电信工（来自桌面版 v3.6.1）

- **校区判定**认中文全称与英文全称（`杭电信工` / `杭电` / `杭州电子科技大学` /
  `信息工程学院` / `hangzhou dianzi` / `information engineering`），并且**商品规格
  （加料）里出现「杭电信工」时直接判杭电** —— 杭电客户的取餐点就是在商品选项里选的，
  地址写法很不稳定（实测有英文全称的写法）。
- **取餐点只有 `北门` 与 `东1门`**：规格里选的门优先（`杭电信工（北门外卖架）` /
  `杭电信工（东1门）`），规格没写门名时回退看地址原文；两边都认不出就**降级为待确认**
  （界面弹窗让人工填一次，没填的以原始地址追加到表尾，不丢单）。
- **规格指向杭电信工、收货地址却明显在别的校区** → 交人工确认，不照着任一边写
  （杭电两条选项都是 ¥0，客户选错校区时写哪边都是错的）。
- **子表名与「类型」列**：两张表按用户口径叫「杭电午餐/杭电晚餐」，类型列写「午餐/晚餐」
  （其余校区仍是「中餐/晚餐」，与协作者云端表已有行一致）；排单模板 13 → 15 张表，
  「清空旧数据」与「地址排序」同步覆盖（北门 → 东1门，认不出排表尾）。
- **云同步**登记《杭电午餐9月.xlsx》（杭电晚餐店家还没开放，本地子表暂时不上传）；
  地址顺序默认 `北门、东1门`；界面「地址排序」同步列出 8 张子表。
- **衣锦规格兼容新文案**：`联建门口外面柜` 也判「外卖柜」（原来只认 `联建门口外卖柜`，
  平台选项改版后会把外卖柜单误判成校门口）。

### 闪时送：创建订单并发出厂 8 路 → 4 路

- 出厂默认 `sss_max_workers` 8 → **4**（`DEFAULTS_REVISION` 1 → 2）；迁移规则不变：
  仅当取值仍等于上一代出厂默认（8）时搬到 4，用户显式改过的其它取值
  （1 = 串行回退、2、6、12、20…）一律保留，`sss_read_timeout_s` 30 秒不变。
- 取值改为统一走 `resolve_create_workers()`：夹到 `[1, 20]`；配置对象缺字段或取值
  不可解析时按**串行 1 路**保守回退（拿不准并发语义时不并发发非幂等 POST）。
- 依据是桌面版同一套口径（吞吐 = 并发 ÷ 每单耗时；4 路已在这条线性区），
  取舍见 [design/SSS-对账提速与并发.md](design/SSS-对账提速与并发.md) §3.7。

### 云同步：回滚删除前先证明区间身份（来自桌面版 R10）

- 插入新客户行之后若后续步骤失败，回滚是**按行号区间删行**；协作者在这期间插行/重排过，
  直接删就会删掉别人的数据。现在删除前先只读核对两件事（区间里的非空姓名必须都属于
  本次新增的人；本次新增的人不得出现在区间之外），核对不过**不删**并提示人工核对。
- 拿不到姓名列信息、或只读核对本身失败 → 同样按不通过处理（宁可残留空行，不删错行）。

### 云同步：计划摘要补上「被拒绝的表数」

- `planned_summary.rows.blocked` = 被批次日期闸门整表拒绝的子表数（与 `summarize_plan`
  的 `blocked` 同口径），避免「整表被拒绝」在摘要里显示成「没有变更」。

### 验证

- `python -m pytest -q`：**1557 passed / 0 failed**（另有 1 xpassed）。
- `ruff check`（CI 规则集）与 `compileall` 通过。
- `pnpm test` 219 passed；`pnpm lint` 0 warning；`pnpm build` 与 `pnpm check:anchors`（18/18）通过。
- 浏览器门禁（headless Chrome + CDP）：**250/250 PASS**，未预期页面异常 0（预期内 6 条见门禁白名单）。

### 发布补记：Termux 运行时包轮转与 APK 出包结果

- 首次出包（run 37661179771）在「下载并校验 Termux 运行时包」这一步 404 失败：Termux 轮转包版本
  时把 `proot 5.1.107.95` 与 `libtalloc 2.4.3` 从 pool 移除（两个 URL 均已 404）。按官方索引
  `dists/stable/main/binary-aarch64/Packages` 重新冻结：proot → `5.1.107.96`、
  libtalloc → `2.5.0`（deb 内成员随 soname 变成 `libtalloc.so.2.5.0`，打进 APK 仍是 `libtalloc.so`），
  `libandroid-shmem 0.7` 未变；两个新 sha256 与「本机独立下载 deb 后计算的值」逐字一致，
  并用脚本自身的解包函数确认三个包的成员路径都还能取出。curl.se 的 CA bundle 冻结值本次复核仍匹配
  （`a41b5d35…0505`，188900 字节），未改动。
- pin 修好后 run 37662427326 出包成功：**Release v3.6.16 已发布**
  （APK 19,824,521 字节，sha256 `995c114a…af48`，内嵌 versionName 3.6.16）。

## 3.6.15

一次以「提速」为主的版本：闪时送对账不再翻全量历史、下单并发翻倍；云同步预览去掉两类非风险提示。

### 闪时送：对账提速（单次 69 秒 → 约 2-6 秒）

- **空壳按空页处理**：服务端对「时间窗内确实没有订单」返回的是 `success:true` + `total:0` 的空壳
  （没有 `records` 字段）。旧代码把它当结构异常，于是每次「空窗」都被判成查询失败。
- **删掉「无过滤全量扫描复核」**：提交前站内本就没有本批订单（0 匹配是常态），旧逻辑因此每次运行
  都把账号全部历史订单翻一遍（2026-09-26 生产实测 **69 秒/次**：30+ 页 × 每页约 2 秒）。
- **新增只读「时间窗自检」**：账号有订单、而「按构造必然包含它的时间窗」却查不到 → 判定服务端不再
  接受 `startTime/endTime`，本次对账 fail-closed 停止提交（结论按进程缓存 10 分钟，不重复探测）。
  `YIKOU_SSS_SERVER_PREFILTER=0` 仍可退回旧的「无过滤扫描」行为。
- **窗口查询一页取完**（预筛页大小 300；窗口内通常 100-140 条）；「只读核对未决记录」的 ±3 天
  宽窗复核也改走服务端时间窗（原先同样是全量扫描）。
- 对账复查 3 次 × 0.5 秒 → 4 次 × 2 秒（扫描变便宜后重估；全程只读，绝不重发 POST）。

### 闪时送：下单并发 4 → 8 路

- 出厂默认 `sss_max_workers` 4 → **8**、`sss_read_timeout_s` 20 → **30 秒**（并发翻倍后若平台在
  建单上排队，超时会被归类「已发送未知」并要求人工只读核对，所以留出余量）。
- 这两个字段界面上从来没有入口，旧配置里保存的只可能是旧出厂默认；因此新增一次性迁移
  `defaults_revision`：仅当取值仍等于旧出厂默认（4 / 20.0）时搬到新默认，用户显式改过的其它取值
  （1 = 串行回退、2、6、12…）一律保留。
- 每轮提交后新增一行实测汇总：`本轮提交 N 单，耗时 X 秒，每单平均 Y 秒（吞吐 Z 单/秒）`；
  均时接近读取超时 60% 时会附一句「把并发调低 / 把读取超时调高」。
- 依据：生产实测 4 路吞吐 0.44-0.48 单/秒 ≈ 4 ÷ 每单 8.4-9.2 秒（吞吐随并发线性，平台没有按账号
  串行建单）；4/8/16/32 路压测无排队、无限流、无 429。真实建单路径的收益按汇总行的每单均时复核。

### 云同步预览：两类提示不再算风险

- 「已忽略日期区间外的日期样式格 …（协作者标记列）」不再出现在风险警告里；标记列的识别逻辑不变，
  「云端表里没有当天列」这条**真风险**仍会带上「备注右侧那些格子是协作者标记列」的提示。
- 「地址「…」不在排序清单里，将排到表格最后面」不再逐条上报：**排序行为不变**（清单外地址照旧排到
  表尾，每行落到第几行在变更列表里能看到），前 5 个仍保留为结构化数据 `sort.unknown_addresses`。

### CI

- Termux 再次轮转 proot 包：旧 pin `proot_5.1.107.94` 从 pool 移除，APK 构建在
  「Fetch kdocs-cli and Android runtime」步骤 404（run 36295518213）。按官方
  `dists/stable/main/binary-aarch64/Packages` 升到 **5.1.107.95** 并重新冻结 SHA256；
  另外两个 Termux 包（libtalloc 2.4.3、libandroid-shmem 0.7）未变。
- curl.se 的 Mozilla CA bundle 同一天也更新了：冻结的 `CA_SHA256` 不再匹配
  （run 36296682990）。按「本机下载 curl.se/ca/cacert.pem 后计算的 sha256」重新冻结为
  `a41b5d35…0505`（121 张证书 / 188900 字节）。
- 两处 pin 修好后 run 36297150256 出包成功：**Release v3.6.15 已发布**
  （APK 19,804,177 字节，sha256 `3661e14f…9efa`，内嵌 versionName 3.6.15）。

### 验证

- `python -m pytest -q`：**1536 passed / 1 failed / 1 xpassed**。唯一失败是文档引用门禁在本地两份
  **未入库**的复查稿里发现的死引用（与代码无关，全新 checkout / CI 不受影响）。
- `ruff check`（CI 规则集）与 `compileall` 通过。

## 3.6.14

一次以「精简」为主的重构，**不改变三个任务模式的业务行为**。

### 文档

- 删除 WPS 云同步的交接文档（564 行）；README 436 → 158 行，云同步长规则移入
  `docs/WPS-SYNC-RULES.md`，README 只保留「是什么 / 快速开始 / 要点 / 开发 / 已知限制」。
- `design/` 历史资料与 `tools/` 一次性探针归档到 `design/archive/`；
  `design/` 只保留仍被代码注释引用的方案文档（APK 计划、云同步方案、网页版设计等）。
- 新增门禁 `tests/test_doc_references.py`：文档里的相对路径必须真实存在，
  且已删除/已归档的文档不许在旧路径复活。

### 前端

- 三个页签的流程条改为**只在「任务在跑 / 待核对 / 失败」时渲染**，闲置态不再占屏幕。
- 云同步页删除 5 处冗余 chrome：重复的「当前写入」状态项、与上方重复的设置提示、
  原始预览全文转储、调试性的「预览编号」、无预览时的占位提示；9 处长句文案压到一句。
- **安全闸门一个都没动**：预览 → 确认 → 上传、真实下单二次确认、未决记录处置、
  WPS 恢复处置、上传单飞闸门、地址排序与凭据开关。
- 新增门禁 `pnpm check:anchors`（`frontend/scripts/check-mutation-anchors.mjs`）：
  变异检查的 18 个精确源码锚点必须在目标文件里唯一命中。

### 后端

- 删除无消费者的桥接通道 `echo_test` / `frontend_report` / `pop_reports`
  （含 HTTP 白名单项与 50 条上限的快照缓存）。
- 删除桌面版更新提示轨道（`DESKTOP_REPOSITORY` 与 `desktop_update:available` 事件），
  版本检查收敛为单轨道。
- 删除 legacy_一口轻食.py（旧版脚本，非运行入口）。

### CI

- **浏览器门禁接入流水线**：`frontend` job 每次 push / PR 跑
  `browser-interaction-check.mjs`（真实 headless Chrome + CDP，202 条界面断言）。
- 新增 `mutation` job（PR 与手动触发）：注入已知缺陷，要求指定断言必须变 FAIL，
  证明上面那些断言不是空转。
- 新增 `workflow_dispatch`，可在 Actions 页手动触发。

### 验证

- `pytest -q`：**1515 passed / 0 failed**（另有 1 xpassed）。
- `pnpm test` 219 passed；`pnpm build` 与 `pnpm check:anchors`（18/18）通过。
- 独立审计 7/7 PASS（锚点唯一性、门禁一致性、变异语义、安全语义、门禁非空转、
  前后端契约、文档死引用）。
- 说明：浏览器门禁与变异门禁在本版**第一次接入 CI**，此前只在本机手动跑；
  本机无 Chrome/Chromium，故这两项的实跑结论以 CI 为准。

### 发布后修正（CI 首次接入暴露；只动测试与 CI，不影响 APK 内容）

- 文档门禁曾把 `frontend/dist` 判成坏引用 —— 它是 gitignore 的构建产物，全新 checkout
  里本就不存在，而 APK 流水线的 pytest 跑在 `pnpm build` 之前，直接挡住了出包。
  改为用 `git check-ignore` 判定：被 ignore 的路径不算仓库内容。
- 浏览器门禁：W3 恢复弹窗断言不再要求「内容必须溢出」（CI 上内容刚好放得下是更好的结果，
  却被判失败）；CDP 连接等待 15s → 90s，Chrome 进程退出时立刻报退出码。
- 变异门禁：`batch2-header-detail-truncated` 的 uncertain 两格改为控制组 ——
  批2 矩阵在 390px 取样，该状态 33 字的说明本来就放得下，注入 `truncate` 不产生可见裁切。
- `mutation` job 超时 60 → 120 分钟（13 个场景实测约 46 分钟）。
- CI 实测：浏览器门禁 **250 PASS / 0 FAIL**；变异门禁 **13/13 场景通过**。
- Windows runner 上 python 用例的 43 个失败（子进程中文编码 + POSIX 语义，与 3.6.13
  记录的同一批）**不再修**：产品只在 Android / Termux 上跑，Windows 与 macOS 不是交付目标。
  CI 的 python 矩阵从「Windows + Ubuntu + macOS × 3.11/3.13」收敛为
  **只跑 Ubuntu × 3.11/3.13**，并关掉 `fail-fast` —— 原来 Windows 一红会把
  ubuntu/macOS 一起取消，连最接近 Termux 的 Linux 信号都拿不到。
  历史记录见 `design/archive/迭代进展.md`。
- **删除 Windows / macOS 支持代码**（桌面三平台由另一个项目负责）：`app/core/config.py`
  的 `%APPDATA%` / `Application Support` 数据目录与目录 fsync 跳过、`app/wps/atomicio.py`
  与 `app/ordering/uncertain.py` 的 `msvcrt` 锁分支与 `LOCALAPPDATA` / `darwin` 状态目录、
  `app/wps/cli.py` 的 `kdocs-cli.exe` 查找、`app/web/server.py` 的 `gethostbyname_ex` 兜底、
  `scripts/fetch_kdocs_cli.py` 的 windows/darwin 平台条目。Android / Termux / Linux
  行为逐字未变；全量测试 `1516 passed / 0 failed`。

## 3.6.13

此前版本的记录见 `design/archive/迭代进展.md`（2026-09-23 条目起）。
