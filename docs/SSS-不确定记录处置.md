# 闪时送「未解决的不确定记录」阻断处置手册

- 适用版本：`v3.6.14`（[../app/__init__.py](../app/__init__.py)，`__version__` 在第 3 行）。
- 适用对象：现场值守人员与管理员（三个处置入口**仅管理员**可用）。
- 本文只描述**已实现且已验证**的行为；行号引用以本仓库当前工作区为准，验证命令见第 6 节。
- 一句话：**看到阻断就停手** —— 不要重跑、不要补发、不要清数据；先在闪时送 App 只读核对，
  再由管理员带审计解除，或直接手工补单让下一轮自动确认。

---

## 1. 这个阻断是什么

### 1.1 根因：非幂等 POST + 结果未知

闪时送下单 POST **不是幂等操作**，平台报文里也没有客户端幂等字段。正式运行会在日志里明确写：

> 平台接口未探测到客户端幂等字段：采用“至少一次提交 + 对账确认”语义，不承诺 exactly-once
> —— `app/ordering/runner.py:266-269`

因此当 POST 结果未知时（传输层超时/断线、2xx 但响应语义不明），程序**不会**把它当成“失败可以重发”，
而是把它记成一条**活跃的“已发送未知”记录**写进本地 journal
（`app/ordering/runner.py:519-522` → `app/ordering/uncertain.py:1212` `append_uncertain_records`）。

典型现场：上一轮批量提交（例如 62 单）过程中网络异常或读超时，**POST 是否落库未知**，
journal 里就留下了一批活跃未决记录；此后每一轮都会被闸门挡下。

这条安全属性由独立反证探针锁定（`tests/independent_final_counterexample_probe.py:871`，
场景 `m6-post-timeout`，登记于 `:1125`）：

1. 传输层异常（断线/读超时/连接重置）与语义不明的 2xx（`{}` / `code=0` / `success=null`）
   一律归类为“不确定”，**不得**成为可重发的“明确失败”；
2. POST 已落库后超时：本地必须保留活跃未决记录；
3. 第二次运行即使站内列表仍读不到，也**不得再 POST**，必须 `blocked_uncertain`。

### 1.2 闸门：任何 POST 之前先只读对账

下一轮运行在**提交任何 POST 之前**会先做一次严格只读对账
（`app/ordering/runner.py:423-424` → `app/ordering/uncertain.py:1475` `resolve_pending_records`）：

- 站内同一天**查得到**且订单指纹匹配 → 自动标记 `resolved`、清理记录、解除阻断
  （`app/ordering/uncertain.py:1502-1511`；日志「跨运行不确定记录：只读对账已确认并清理 N 条」）；
- 站内**查不到** → **保持阻断**，本批不提交任何 POST
  （`app/ordering/runner.py:443-457`：`semantics="uncertain-journal-guard"`、
  `status="blocked_uncertain"`、`next_action="先只读核对站内订单与本地不确定记录；未确认前不要重跑或补发"`）。

用户手机/页面上看到的文案来自 `app/api/bridge.py:1610-1612`：

> 闪时送任务被阻断：存在未解决的不确定记录，请先只读核对站内订单与本地记录；未确认前不要重跑或补发

**为什么“站内查不到”不等于“没落单”**：平台订单列表有最终一致性延迟；订单可能已落库但送达日被平台
改到相邻日期；登录/查询本身也可能失败。任一种情况下重发 POST 都会造成**重复下单**。所以设计上是
fail-closed：查不到 → 阻断，把判断权交给人工核对，再由管理员带审计解除。其他被探针锁死的相关性质：

| 探针场景 | 锁定的安全性质 | 位置 |
|---|---|---|
| `journal-paths` | 换 journal 路径不得造成跨进程重复 POST（安全期望 1 单） | `tests/independent_final_counterexample_probe.py:135`（登记于 `:1086`） |
| `crash-journal-paths` | 首进程 POST 后崩溃、次进程换路径也不得盲目重发 | `tests/independent_final_counterexample_probe.py:784`（登记于 `:1088`） |
| `m4-json-corrupt` | JSON 语法损坏的 journal 必须 fail-closed，不能被当成空 journal 放行 | `tests/independent_final_counterexample_probe.py:825`（登记于 `:1123`） |
| `m6-post-timeout` | POST 超时/断线 → 保留未决记录 + 二次运行 `blocked_uncertain` | `tests/independent_final_counterexample_probe.py:871`（登记于 `:1125`） |

### 1.3 闸门不止一个（先分辨再动手）

`status=blocked_uncertain` 有三个来源，处置动作不同：

| 闸门 | 触发条件 | 结果 `semantics` | 位置 |
|---|---|---|---|
| 跨作用域闸门 | 本机同一账号存在**无法安全归属**的活跃记录（旧网址写法、归属字段缺失） | `cross-scope-authority-guard` | `app/ordering/runner.py:314-354` |
| 权威位置切换闸门 | 另一个已登记/默认权威位置仍有本账号的活跃记录 | `authority-location-guard` | `app/ordering/runner.py:362-412` |
| **未决记录闸门（本手册主场景）** | 当前批次键下仍有活跃未决记录 | `uncertain-journal-guard` | `app/ordering/runner.py:443-457` |

前两类的记录**不在当前批次键下**，所以「未决记录」面板可能显示 0 条记录却仍在阻断 —— 此时不要反复点
解除；按日志提示人工整理对应位置的记录（见第 4 节），必要时先把闪时送网址改回正式写法。

### 1.4 阻断的“钥匙”：批次键与 journal 文件

- 批次键 = `送达日|规范化账号`（`app/ordering/uncertain.py:209-215`）。`source`（excel/wps）刻意不参与，
  防止用切换名单来源绕过阻断；账号只做去空白/NFKC 归一化，不合并不同号码。
- 权威 journal 路径（`app/ordering/uncertain.py:390-405`，根目录见 `:267-286`）：
  - **Android**：`YIKOU_DATA_DIR` = `filesDir/config`
    （`android/app/src/main/python/android_bootstrap.py:56-69`，第 61 行）→
    `/data/data/com.yikou.lightfood/files/config/sss-authoritative/<sha256(origin|account)[:24]>.json`
  - Termux / 桌面：`$XDG_STATE_HOME/yikou-light-food/sss-authoritative/<digest>.json`
    （默认 `~/.local/state/...`）
  - 显式覆盖：配置 `sss_authoritative_uncertain_path` 或环境变量 `YIKOU_SSS_AUTHORITATIVE_PATH`
- 批次锁：`app/ordering/uncertain.py:160-176`，落在 `sss-locks/<sha256(batch_key)[:32]>.lock`；
  拿不到锁时本批直接 `blocked_concurrent` 且**零 POST**（`app/ordering/runner.py:288-305`）。
- Android 的 journal 位于 App 私有目录，**Termux 无权限读取**，因此现场必须使用 App/网页内的入口；
  桌面版可以直接查看该文件（只读）。

---

## 2. 现场处置流程

> 总原则：**先停手 → 只读核对 → 人工判定 → 管理员带审计处置 → 再重跑**。

### 步骤 0：停手

不要重跑、不要补发、不要清 App 数据/卸载、不要删改任何 `sss-authoritative` 文件、不要并行多开。
阻断期间所有 POST 都被闸门挡下，重跑只会重复触发阻断（并可能新增记录）。

### 步骤 1：在闪时送 App 里核对当天订单

用闪时送商家 App（或站内后台）按**姓名 + 电话 + 送达时间**核对：这些单到底有没有落单。
注意送达日可能被平台改到相邻日期，所以也看一眼前后一两天。

### 步骤 2：站内确实没有 → 二选一

- **A. 直接手工补单**（推荐，最省事）：在闪时送 App 里把缺的单补上。
  下一轮运行时，闸门的只读对账会查到它们、自动 `resolved` 并解除阻断
  （`app/ordering/uncertain.py:1502-1511`）；随后提交前的“下单前站内对账”会确认它们已存在，
  **不会重复提交**（`app/ordering/submission.py:534-548`，日志「Excel 订单均已在站内确认，本次不发送任何下单请求」）。
  补单时姓名/电话/送达时间要尽量与名单一致，否则对账可能认不出来。
- **B. 用新入口解除**：页面「未决记录与只读核对」面板 → 点「只读核对站内订单」
  （需要闪时送登录密码，**零 POST**）→ 核对结果里这些记录都被判成「站内缺失」→
  管理员点「已确认站内无这些订单 → 解除阻断」。该操作会把记录标成 `discarded`，
  下一轮会**真的重发**，所以证据要求很严（见 3.3）。

### 步骤 3：站内已经有 → 标记为已确认

管理员点「已在站内找到 → 标记为已确认」（`decision=station_present`）：记录标 `resolved`，
本批不会再提交这些订单。这是安全方向，即使判断错也不会因此多下单（提交前还会独立对账）。

### 步骤 4：还不确定 → 保持阻断

管理员点「只记录备注（保持阻断）」（`decision=keep`）：只写审计、什么都不改，阻断保持。

### 步骤 5：重跑

面板/列表显示当前批次 `active=0` 后，再正常重跑本批。重跑时提交前仍会再做一次只读对账。

> 若只读核对返回 `review_failed`（登录/查询失败）或出现 `scan_failed`，**不能**据此解除阻断 ——
> 那表示“读不到”，不是“站内没有”（`app/ordering/runner.py:842-858`、`:860-864`）。

---

## 3. 三个新入口用法

三个入口都是 `POST /api/<方法名>`，**仅管理员**：普通用户在 HTTP 白名单层直接 403 `admin_only`
（`tests/test_web_roles.py:190-201`），绕过 HTTP 直接调用也会得到 `ok=false, status=forbidden`
（`app/api/bridge.py:1342-1346`）。三者**永不发 POST、永不写云端**。

### 3.1 `sss_uncertain_records` —— 只读列出阻断记录（无网络）

```jsonc
// POST /api/sss_uncertain_records
{}
```

返回（`app/api/bridge.py:1214-1252`）：

- `records[]`：`journal_id`/`identifier`/`name`/`phone`（脱敏）/`delivery_time`/`door_num`/
  `status`/`error`/`created_at`/`batch_id`；
- `counts{active,inflight,unresolved,resolved,discarded}`、`journal`/`batch_key`/`delivery_date`；
- `review` 快照：`available`/`stale`/`journal_matches`/`covers_active`/`checked_at`/`age_s`/
  `wide_window_days`/`journal_fingerprint`/`counts`/`classifications`（`app/api/bridge.py:1163-1212`）；
- `next_action`：有记录时提示「先只读核对站内订单；确认后由管理员在未决记录面板解除阻断」，
  没有记录时提示「当前批次没有活跃未决记录，无需处置」。

手机号脱敏只保留前 3 后 4（`app/ordering/uncertain.py:1352-1366`）。读失败返回
`journal_unreadable`，**绝不假装“没有记录”**（`app/api/bridge.py:1231-1237`）。

### 3.2 `start_sss_review` —— 只读核对（零 POST，需要密码）

```jsonc
// POST /api/start_sss_review
{"password": "闪时送登录密码"}
```

- 仅管理员；需要已保存闪时送**网址 + 账号**，密码必填；`remember` 被忽略（只读动作不写凭据）
  （`app/api/bridge.py:933-947`）；
- 复用正式运行的登录/对账代码（`app/ordering/runner.py:642-888`）：先严格只读对账
  （站内查到的自动 `resolved`），再对剩余记录做 **±3 天宽窗复核**
  （`_REVIEW_WIDE_WINDOW_DAYS = 3`，`app/ordering/constants.py:49`），把每条分类成
  `station_missing` / `station_found_other_day` / `scan_failed`；
- 返回 `status`：`review_ok`（已全部确认并解除）/ `review_blocked`（仍有记录）/
  `review_failed`（读不到）；`review.counts` 与 `review.journal_fingerprint` 是后续解除的证据；
- 失败语义：登录/查询失败一律 `review_failed`，**绝不当成“站内没有”**
  （`app/ordering/runner.py:661-662`、`:879-884`）；
- worker 结束后页面会自动刷新一次未决记录，不需要轮询。

### 3.3 `sss_uncertain_resolve` —— 管理员带审计处置（永不发 POST、永不联网）

```jsonc
// POST /api/sss_uncertain_resolve
{"decision": "station_absent",              // station_absent | station_present | keep
 "confirm":  "station_absent",              // 必须与 decision 逐字相同
 "note":     "人工核对说明，至少 4 个字符",   // 写进 journal 审计
 "record_ids": ["<journal_id>", "..."]}     // 必须显式列出，不支持整库清空
```

| decision | 动作 | 前置证据 | 影响 |
|---|---|---|---|
| `station_present` | 记录标 `resolved` | 无（安全方向） | 本批不再重发这些订单 |
| `station_absent` | 记录标 `discarded` | 必须有一次**新鲜且覆盖全部所选记录**的只读核对，每条都判 `station_missing` | **下一轮会真的重发** |
| `keep` | 什么都不改 | 无 | 保持阻断（`changed=false`） |

校验规则（`app/api/bridge.py:1134-1136`、`:1342-1373`、`:1464-1505`）：

- `confirm` 必须与 `decision` 逐字相同，否则 `confirmation_required`；
- `note` 至少 4 个字符（`_SSS_RESOLVE_MIN_NOTE = 4`），否则 `note_required`；
- `record_ids` 必须显式列出（`invalid_record_ids`）；不在当前批次的 id → `unknown_record_ids`；
- `station_absent` 额外要求（全部满足才允许解除，因为解除后下一轮会真的重发）：
  - 有一次只读核对快照，且距现在 **≤ 600 秒**（`_SSS_REVIEW_TTL_S = 600.0`，
    `app/ordering/constants.py:51-53`）→ 否则 `review_stale`；
  - journal 指纹与核对时一致（`app/ordering/uncertain.py:1422-1432`）→ 否则 `journal_changed`；
  - 所选记录**全部**被分类为 `station_missing`，且宽窗内没有命中
    （`station_found_other_day`）→ 否则 `review_required` / `station_state_changed`；
- 处置在批次锁内写盘（`app/api/bridge.py:1416-1422`），并发操作返回 `operation_conflict`；
- 成功返回 `post_sent=false`、`cloud_write=false`、`affected`/`remaining`，以及
  `audit{actor,note,at,journal,journal_fingerprint,reviewed_at}`
  （`app/api/bridge.py:1456-1462`、`:1517-1525`）。

页面上的对应按钮：`只读核对站内订单`、`刷新未决记录（只读）`、
`已在站内找到 → 标记为已确认`、`已确认站内无这些订单 → 解除阻断`、`只记录备注（保持阻断）`；
确认弹窗必须勾选「我已人工只读核对站内订单，并了解本次处置只写本地审计、不会发送任何 POST」
（`frontend/src/components/UncertainPanel.tsx:432-450`、`:548`）。

---

## 4. 禁止事项

| 禁止 | 为什么 |
|---|---|
| 重跑 / 补发 / 连点「开始下单」 | 阻断未解除前闸门会挡下；解除后重跑才是正确动作。绕过闸门就会重复下单 |
| 清 App 数据 / 卸载重装 | journal 在 `filesDir/config` 内，卸载即清除（`android/app/src/main/python/android_bootstrap.py:56-57`）。记录没了、站内订单还在 → 下一轮盲目重发 |
| 删除/改写 `sss-authoritative/*.json` 或锁文件 | 这就是权威记录；损坏会被 fail-closed（`app/ordering/uncertain.py:1145`）甚至更糟。要改必须走 3.3 的入口 |
| 并行重跑 / 多开窗口 / 多设备同时跑 | 批次锁会拒绝（`blocked_concurrent`），但不要试图绕过；并发 POST 无法对账 |
| 换网址写法（尾点、中文域名等）、切 `sss_uncertain_path`/`YIKOU_SSS_UNCERTAIN_PATH`、切权威路径、换账号写法、切 excel/wps | 跨作用域闸门与位置闸门会阻断，并可能新增“无法安全归属”的记录；批次键按规范化账号计算，切 source 无效 |
| 把「只读核对失败」当「站内没有」 | 读不到 ≠ 没落单；据此解除会重复下单 |
| 在 Termux 里直接改 Android App 私有目录的 journal | 无权限（App 私有目录），且格式校验会 fail-closed（`app/ordering/uncertain.py:1145`） |
| 用旧版 APK / 别的账号跑同一批 | 版本与账号身份不一致，对账口径不同 |

---

## 5. 证据与判读速查

| 证据 | 含义 | 位置 |
|---|---|---|
| `review.counts.station_confirmed` | 只读对账已确认并清理的记录数 | `app/ordering/runner.py:778-786` |
| `review.counts.station_missing` | 宽窗内也查不到（解除 `station_absent` 的必要条件） | `app/ordering/runner.py:816-821` |
| `review.counts.station_found_other_day` | 宽窗内命中（送达日不同）→ 不能按“站内没有”解除 | `app/api/bridge.py:1489-1497` |
| `review.counts.scan_failed` | 读不到；>0 时不产生解除证据 | `app/ordering/runner.py:842-858` |
| `review.journal_fingerprint` | 核对时的 journal 指纹（CAS 锚点） | `app/ordering/uncertain.py:1422-1432` |
| `review.stale` / `review.age_s` | 核对快照是否超过 600 秒 | `app/api/bridge.py:1197-1206` |
| `audit.actor/note/at` | 处置人、说明、时间（journal 审计） | `app/api/bridge.py:1456-1462`、`:1517-1525` |

---

## 6. 验证命令与版本

```bash
# 未决记录闸门与三个入口的自动化测试
python -m pytest tests/test_sss_uncertain_review.py tests/test_web_roles.py -q

# 独立反证探针（不进入 pytest 收集；退出码 0 = 场景都显示“已阻断”）
python3 tests/independent_final_counterexample_probe.py --only m6-post-timeout,journal-paths,m4-json-corrupt
```

- 版本：`v3.6.14`；`app/__init__.py` 的 `__version__` 与 `android/version.properties` 的
  `versionName=3.6.14` / `versionCode=3061400` 同步。
- 本手册对应的实现基线：`app/ordering/runner.py`、`app/ordering/uncertain.py`、
  `app/api/bridge.py`、`app/ordering/constants.py`、`app/ordering/submission.py`。
