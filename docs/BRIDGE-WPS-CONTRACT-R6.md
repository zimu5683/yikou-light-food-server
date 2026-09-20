# Bridge / WPS 最终请求-响应契约（R6 安全修复后，给前端 D）

- 基线：`main c9a79cf` / v3.6.12；本文件对应 A 会话 R6 接线修复（W1/W3/W5/W6/W7/R6-9）。
- 适用对象：`frontend/` 的所有调用方（`CloudForm.tsx`、`TaskPanel.tsx`、
  `lib/bridge.ts`、`lib/taskOutcome.ts`、`lib/operationStatus.ts`）。
- 只描述**已实现且已验证**的行为；证据命令见文末第 10 节。
- 权威实现：`app/api/bridge.py`、`app/web/server.py`；底层契约见
  `docs/OPTIMIZATION-WPS.md`（B 会话）。

---

## 1. 调用约定与权限

```text
POST /api/<method>
Content-Type: application/json
Cookie: yikou_session=<token>        # 或 X-Yikou-Token / ?token=（本地直连）
```

请求体可以是 **JSON 数组（按位置传参）** 或 **JSON 对象（按关键字传参）**：

```jsonc
["pv-xxxx"]                                  // wps_upload(preview_id)
{"operation_id": "wps-...", "decision": "retire_guarded", ...}   // wps_recovery_resolve
```

失败状态码：

| HTTP | `code` | 含义 |
| --- | --- | --- |
| 401 | `login_required` | 未登录 |
| 403 | `admin_only` | 该桥接方法仅管理员（白名单外一律 403） |
| 400 | `bad_arguments` / `bad_json` | 参数或 JSON 不合法 |
| 404 | `unknown_method` | 方法不存在 |
| 500 | `call_failed` | 非预期异常逃出桥接方法。只读入口（preview / check_copies / day_orders）即使走到这里也**已经释放互斥槽位**，重试即可；`operation_status().active` 必为 `false`。前端应把它当“可重试的服务端错误”，不要当成业务状态 |

### 1.1 角色白名单（服务端强制，前端隐藏按钮不算）

| 方法 | 普通用户 | 管理员 |
| --- | --- | --- |
| `wps_status` / `wps_recovery_status` / `pending_interactions` / `operation_status` | ✅ | ✅ |
| `wps_preview` / `wps_upload` / `wps_check_copies` / `sss_day_orders` | ✅ | ✅ |
| `start_order` / `start_sss` / `stop_task` / `drain_events` / `resolve_*` | ✅ | ✅ |
| **`wps_recovery_resolve`**（恢复/退场旧任务） | ❌ **403 `admin_only`** | ✅ |
| `save_order_config` / `save_sss_config` / `save_wps_config` / `clear_password` / `new_template` / `choose_excel` / `check_updates` / `install_update` / `wps_authorize` / `wps_logout` | ❌ 403 | ✅ |

`wps_recovery_resolve` 除了 HTTP 白名单，Bridge 方法内部还会再校验一次
`is_admin`：绕过 HTTP 直接调用（脚本/旧客户端）返回
`{"ok": false, "status": "forbidden", "code": "forbidden"}`，且不改任何文件。

---

## 2. 计划口径 vs 执行口径（W6，前端必须分流）

从本版本起，预览/上传结果里**行数有两种口径，绝不能混用**：

| 字段 | `kind` | 含义 |
| --- | --- | --- |
| `planned_summary` | `"plan"` | **计划**要改几行：`rows.to_update / to_append / unchanged / skipped / warned` |
| `execution_summary` | `"execution"` | **实际**结果：`sheets.*` 是**表数**，`rows.*` 才是行数 |
| `summary` / `stats` | `"execution"` | 旧字段名保留，但**含义已改为执行口径**（= `execution_summary`） |

```jsonc
"execution_summary": {
  "kind": "execution",
  "contract_version": 1,
  "status": "success",
  "executed": true,
  "counts_source": "apply_plan",        // 或 rejected_before_write
  "sheets": {"total": 1, "verified": 1, "noop": 0, "failed": 0,
             "uncertain": 0, "skipped": 0, "blocked": 0, "other": 0},
  "rows":    {"verified": 1, "failed": 0, "uncertain": 0, "skipped": 0,
              "planned": 1},
  "rows_unknown": false,
  "proven_no_write": false,
  "written_sheets": 1,
  "failed_sheets": 0,
  "note": "rows.verified 只统计 apply_plan 逐格回读校验通过的行；无法证明时为 null（未知）…",
  "next_action": ""
}
```

固定统计口径（后端唯一实现：`Bridge._wps_execution_summary`）：

1. `sheets.verified` = 状态 `ok/verified` 的**表数**；`sheets.noop` = `noop` 表数；
   `sheets.failed` = `failed/stale_batch` 表数；`sheets.uncertain`；`sheets.skipped`；
   `sheets.blocked`；`sheets.other` = 未知/畸形状态表数。
2. `rows.verified` = 逐表执行器回读证明的 `people` 求和；**只要（a）某张 ok 表拿不到
   整数计数、（b）整轮结果不是确定的（`uncertain` 非 false）、（c）存在
   `uncertain/other` 表、（d）执行了却一张表都没回报、或（e）顶层状态是
   `failed/error/blocked/rejected` 但又有表声称 ok（自相矛盾），就为 `null`（未知）**。
   `noop`、`not_started` 表按 0 计入。只有在“确实零写入”
   （`proven_no_write=true`）或整轮确定成功/部分成功时才给出整数。
3. `rows.failed` = 0（执行器对 `failed/stale_batch` 表保证零写入）或 `null`。
4. `rows.uncertain` = 0（确实没有不确定表）或 `null`（未知）。
5. `rows.skipped` = 0（skipped 表在写入前被跳过）或 `null`。
6. `rows.planned` = 计划行数，**只作对照**，永远不能拿去当成功行数展示。
7. `rows_unknown = true` 表示上面任意一个是 `null`。
8. `proven_no_write = true` 表示这次调用**已被证明**没有发生任何云端写入
   （例如在取占位/消费令牌之前就拒绝）。
9. `counts_source` 说明这些计数是怎么来的，前端**不要**把它当成状态：
   `apply_plan`（真实执行器返回）、`rejected_before_write`（写入之前就拒绝，
   计数可信为 0）、`unknown_after_exception`（写入之后结果组装异常：行数与
   `written/failed` 全部未知）。

前端渲染要求：

- 旧代码 `result.summary.to_update` 显示“更新 N 行”必须改为读
  `planned_summary.rows.to_update`（并标注“计划”），成功行数只能读
  `execution_summary.rows.verified`；
- `execution_summary.rows.verified === null`（或 `rows_unknown === true`）时，
  显示“**实际写入行数未知，请只读核对云端**”，不得显示 0 或计划数；
- 表数只能读 `execution_summary.sheets.*`，不得当行数用。

---

## 3. `wps_preview()`

请求：`POST /api/wps_preview`，无参数（`body=[]`）。

拒绝（`ok=false`，且全部可证明零云端写入）：

| `code` | 触发 | `next_action` |
| --- | --- | --- |
| `wps_disabled` | `wps_enabled=false`（W5，服务端强制） | 在「云文档同步」中开启后再试 |
| `missing_excel` / `missing_tables` / `effective_tables_error` | 未选排单表 / 测试副本未配置 / 目标表非法 | 相应配置后重试 |
| `local_file_unreadable` / `local_file_changed` | 本地表读不到 / 读取期间被改 | 文件稳定后重新预览 |
| `local_state_blocked` | 账本/意图日志损坏（`status="blocked"`） | 先修复本地日志/账本 |
| `unauthenticated` | 未授权云文档 | 去授权后重新预览 |
| `plan_failed` / `preview_format_failed` / `unexpected` | 计划/格式化失败（`status="failed"`） | 查看日志后重试 |
| `operation_conflict` | W7：上传/任务/其他只读入口占用中 | 等待后重新预览 |

成功（`ok=true, status="preview_ready"`）关键字段：

```text
preview_id, state, created_at, expires_at, expires_in, ttl_seconds,
local_sha256, context_fingerprint, plan_fingerprint, fingerprint{local_sha256,context,plan},
target_date, target_tables, tables[], blocked[], warnings[], text,
summary, stats,                       # = 计划摘要（预览阶段没有执行结果）
planned_summary{kind:"plan"}, execution_summary{kind:"execution", proven_no_write:true},
test_mode, next_action="wps_upload(preview_id)"
```

`tables[i]`：`sheet/file_id/drive_id/target_date/target_col/target_header/weekday_number/
append_row/last_data_row/row_keys/insert_blocks/format_rows/columns/sort_enabled/
sort_range/sort_key_col/sort_probe_col/sort_mismatch/unknown_addresses/blocked_reason/
previous_batch/warnings/changes[]/counts{to_update,to_append,unchanged,skipped,warned,blocked}`

`changes[j]`：`kind("existing"/"new")/name/phone/address/meal_type/meal_kind/row/slot/
delta/local_meals/ledger_prev/local_rows/total_before/total_after/target_col/
target_occupied/target_ok/needs_write/target_blocked/fill_type/fill_kind/fill_formula/
insert_row/detail`

**W5**：`wps_enabled=false` 时预览直接拒绝，**不读云端、不建计划、不发令牌**。

---

## 4. `wps_upload(preview_id)`

请求：`POST /api/wps_upload`，`["<preview_id>"]`。

### 4.1 成功

```jsonc
{
  "ok": true, "status": "success", "reason": "", "code": "", "next_action": "",
  "written": 1, "failed": 0, "written_verified": 1,
  "planned_summary": {"kind": "plan", "rows": {"to_update": 1, "to_append": 0, ...}},
  "execution_summary": {"kind": "execution", "sheets": {...}, "rows": {...}},
  "summary": "<= execution_summary>", "stats": "<= execution_summary>",
  "summary_stats": "<= planned_summary>",
  "operation_id": "op-000001-xxxxxx", "preview_id": "pv-...", "target_date": "2026-09-20",
  "tables": [...], "blocked": [], "warnings": [], "text": "...",
  "executor_status": "ok", "executor_next_action": "", "executor_operation_id": "wps-...",
  "journal_path": "...", "recovery": null,
  "verification_missing": false, "contradictory": false,
  "possible_write": true, "uncertain": false, "test_mode": false,
  "failed_sheets": [], "result": {...B 执行器原始返回...}
}
```

`status="noop"` 同样是 `ok=true`（执行器确认无需写入），不要渲染成“成功更新 N 行”。

### 4.2 非成功

| `status` | 含义 | `next_action` | 用户动作 |
| --- | --- | --- | --- |
| `uncertain` | 可能已写但无法确认（含 `verification_missing/contradictory`） | 固定「只读核对，不重新上传」 | **不要重试**；只读核对云端，确认后重新预览 |
| `blocked` | 本地账本/意图日志不可用或仍有防重复闸门 | `fix_journal` / 人工核对指引 | 联系管理员修复或走恢复入口 |
| `failed` | 明确失败，未成功写入 | `repreview` | 查原因后重新预览 |
| `partial` | 部分表失败 | 建议 `repreview` | 核对失败表 |
| `recovered` / `not_started` | 处理了历史未完成操作，**本轮计划没有执行** | `repreview` | 重新预览（旧令牌不可复用） |
| `rejected` | 前置闸门拒绝（下表） | 见 `code` | 见下表 |

拒绝 `code`（全部可证明零写入，`written=0, failed=0`、
`execution_summary.proven_no_write=true`）：

| `code` | 触发 | 说明 |
| --- | --- | --- |
| `missing_preview` | 无参/空 `preview_id` | 旧无参上传已禁用 |
| `wps_disabled` | `wps_enabled=false`（W5） | 关闭动作一发生（`save_wps_config({enabled:false})`）就**立刻作废所有未使用令牌**，关闭期间旧 `preview_id` 一律不可用；重新开启后也必须重新预览 |
| `preview_not_found` / `preview_expired` / `preview_consumed` | 不存在 / 超 10 分钟 / 已消费 | 重新预览 |
| `preview_changed` / `preview_invalidated` | 本地文件、目标表配置、日期/时段、排序、地址顺序、计划指纹变化 | 重新预览 |
| `operation_conflict` | 有互斥操作（订单/闪时送/上传/授权/更新/只读入口） | 等待或查 `operation_status` |
| `local_file_changed` / `local_file_unreadable` / `unauthenticated` | 本地表变化或不可读 / 未授权 | 处理后重新预览 |
| `cloud_error` / `unexpected` / `local_state_blocked` | 执行期异常 | `written/failed` 可能为 `null`（未知），必须按“未知”展示 |

**W1（跨进程旧计划）**：计划携带构建时的账本快照；`apply_plan` 在跨进程锁内比较
“计划快照 vs 锁内最新磁盘账本”，不一致时零云端写入并返回
`status="failed"` / `next_action="repreview"`（`reason` 含“请重新预览”）。
前端只需按 `next_action` 提示重新预览即可。

---

## 5. `wps_check_copies()` 与 `sss_day_orders()`

两者都是只读云入口，**W7：与上传/任务共用同一互斥槽位**。

**与 W5 的边界（明确，不是遗漏）**：`wps_enabled=false` 只拒绝 `wps_preview` /
`wps_upload`（会派生上传/写入链路）；`wps_check_copies`（副本结构核对诊断）与
`sss_day_orders`（闪时送下单来源名单）都是**只读**入口，且后者属于下单流程而不是
云同步开关的语义范围，因此不受 `wps_enabled` 影响。它们同样受进程内互斥保护。

**异常安全**：三个只读入口在内部抛异常时（例如 `effective_tables()` 配置非法、
`kdocs-cli` 缺失、非 `WpsCloudError` 的意外异常）会**先释放互斥槽位再把异常抛出**
（既有异常语义不变），不会出现“一次失败把 Bridge 永久锁死、之后所有操作都
`operation_conflict`”的情况。前端如遇 500，重试即可；`operation_status().active`
必须是 `false`。

冲突时返回：

```jsonc
// wps_check_copies
{"ok": false, "reason": "busy", "status": "rejected", "code": "operation_conflict",
 "operation_id": "op-...", "message": "已有云文档上传进行中，已拒绝核对云文档副本…",
 "next_action": "等待云文档上传结束或查询 operation_status"}

// sss_day_orders
{"ok": false, "reason": "<同上 message>", "status": "rejected",
 "code": "operation_conflict", "operation_id": "op-...", "next_action": "..."}
```

成功结构不变：`wps_check_copies → {ok, drifted[], tables[], all_aligned}`；
`sss_day_orders → {ok, target_date, date_text, total, meals{...}, archive, ...}`。

前端行为：遇到 `operation_conflict` 时不要重试轰炸，提示“另一个任务正在运行，
请等待后刷新”，并可用 `operation_status` 显示当前是谁在跑（`mode`）。

---

## 6. `wps_recovery_status()`

只读，普通用户可调用。普通用户返回 `scope="summary"`（无 `operations`）；
管理员返回 `scope="admin"`（白名单 DTO）。

本版本新增 `retired_guarded`（管理员带审计退场、但保留同目标防重复闸门）：

```jsonc
"counts": {"planned":0,"writing":0,"ledger_pending":0,"uncertain":0,
           "verified":0,"failed":0,"not_started":0,"retired_guarded":1},
"summary": {..., "uncertain_count":0, "failed_count":0, "not_started_count":0,
            "retired_guarded_count":1, "has_pending":false, "needs_review":true,
            "guidance":"存在不确定结果：请先只读核对云端与日志，不要直接重传"},
"next_action": "manual_reconcile"
```

管理员 `operations[]` 中该操作：`status="retired_guarded"`、`pending=false`、
`error_code="wps_recovery_retired_guarded"`、
`allowed_next_actions=["manual_reconcile"]`。

状态含义与下一步：

| `status` | 含义 | 允许动作 |
| --- | --- | --- |
| `planned` / `writing` / `ledger_pending` | 未完成（可能已写云端） | 管理员走恢复流程（`recover_journal`） |
| `uncertain` | 无法判定 | 只读核对（`manual_reconcile`） |
| `retired_guarded` | 已带审计退场，**同日期+云表仍被闸门阻断** | 只读核对；证实后才能清除闸门 |
| `failed` | 已确认未写入 | 重新预览 |
| `not_started` | 已确认完全未执行 | 重新预览 |
| `verified` | 已确认完成 | 无 |

---

## 7. `wps_recovery_resolve(payload)` —— 管理员专用（W3）

请求（数组单对象或对象键值均可）：

```jsonc
// 两种合法写法，二选一（数组里必须是**对象**，不要放 JSON 字符串）
[{"operation_id": "wps-0123456789abcdef", "decision": "retire_guarded",
  "confirm": "retire_guarded", "note": "人工核对云端后仍无法判定",
  "confirm_structure_checked": true}]

{"operation_id": "wps-0123456789abcdef", "decision": "retire_guarded",
 "confirm": "retire_guarded", "note": "人工核对云端后仍无法判定",
 "confirm_structure_checked": true}
```

把 JSON 字符串放进数组（`["{...}"]`）不会被解析：那种写法只会被当成
`operation_id` 并返回 `invalid_operation_id`（失败关闭，但前端会永远调不通）。

| 参数 | 必填 | 说明 |
| --- | --- | --- |
| `operation_id` | ✅ | 必须是内部格式 `wps-<16位小写hex>`；用 `wps_recovery_status` 取 |
| `decision` | ✅ | `retire_guarded` / `cloud_verified` / `cloud_untouched` / `keep`（别名 `retire/abandon/...` 归一到 `retire_guarded`） |
| `confirm` | ✅ | **逐字确认**：必须与 `decision` 完全相同 |
| `note` | ✅ | ≥4 字符的人工核对说明，写入 journal 审计 |
| `confirm_structure_checked` | 仅 `retire_guarded` | 必须为 `true`（确认已人工核对云端表结构） |

失败响应（HTTP 200，`ok=false`；`changed=false`，不改任何文件）：

| `code` | 触发 | `next_action` |
| --- | --- | --- |
| `forbidden` | 非管理员（HTTP 层是 403 `admin_only`） | 联系管理员 |
| `invalid_operation_id` | ID 格式非法 | 用 `wps_recovery_status` 重新取 |
| `decision_not_allowed` | 决策不在白名单（附 `allowed_decisions`） | 选择允许的决策 |
| `note_required` | 备注为空或过短 | 补充 `note` |
| `confirmation_required` | `confirm != decision` | 重新提交并逐字确认 |
| `structure_confirmation_required` | 未传 `confirm_structure_checked=true` | 确认已核对后重试 |
| `operation_conflict` | 其他互斥操作进行中 | 等待后重试 |
| `not_found` | 该 operation 不存在/已归档 | 重新查询 |
| `not_pending` | 已确认完成，无需备注 | 无需操作 |
| `cli_unavailable` | 需要读云端的决策但 kdocs-cli 不可用 | 先完成授权 |
| `cloud_verify_failed` / `cloud_not_untouched` | 云端**证明不了**“已完成/完全未执行” | `manual_reconcile`（**不得**当作可重传） |
| `journal_write_failed` / `journal_unreadable` / `local_state_blocked` | 本地日志不可写/不可读 | 先修复本地日志 |
| `internal_error` | 未预期异常 | 查看日志后重试 |

成功响应：

```jsonc
{
  "ok": true,
  "status": "retired_guarded" | "cloud_verified" | "cloud_untouched"
            | "keep_recorded" | "already_retired",
  "code":   "<同上>",
  "reason": "已按管理员决策处理，本地 journal 已写入审计",
  "next_action": "manual_reconcile" | "repreview",
  "contract_version": 1,
  "read_only": false,
  "cloud_write": false,                  // 本入口永不写云端
  "operation_id": "wps-0123456789abcdef",
  "operation_ref": "wps-op:abc123def456",
  "changed": true,                       // already_retired 时为 false
  "verified_on_disk": true,              // 重新读盘确认效果已落盘
  "scope": {
    "operation_ref": "wps-op:...",
    "target_dates": ["2026-09-20"],
    "target_refs": ["wps-target:..."],
    "sheet_count": 1,
    "guard_retained": true,
    "blocking": "retired_guarded" | "pending" | "none"
  },
  "audit": {"actor": "admin@example.com", "at": "2026-09-20T10:00:00",
            "decision": "retire_guarded", "note_recorded": true,
            "duplicate": false, "effects": {"cloud_written": false,
            "guard_retained": true, "blocking": "retired_guarded",
            "auto_retry_allowed": false}},
  "recovery": {"counts": {...}, "next_action": "manual_reconcile",
               "error_code": "", "summary": {...}}
}
```

语义要点（前端必须如实呈现）：

- `retire_guarded`：只把旧任务带审计地退出全局 pending；**同一 `target_date`+云表
  的防重复闸门仍在**，`apply_plan` 会以 `uncertain` + `manual_reconcile` 拒绝写入。
  即：**未知写入结果不会因为“退场/归档”变成可以自动重传**。
- `cloud_verified` / `cloud_untouched`：会重新只读云端并按实际证据判定；证明不了就
  失败（`cloud_verify_failed` / `cloud_not_untouched`），**不是**强制清除。
- `keep`：只写审计备注，保持阻断。
- 重复请求幂等：已退场再提交同一 `retire_guarded` → `status="already_retired"`、
  `changed=false`，**不再写盘**。
- 响应里**不含** sheet 原名、`file_id`、客户姓名/电话/地址、异常原文。
- 前端没有“强制清除 uncertain”入口；不要为它设计按钮。

---

## 8. 任务事件里的 `blocked_concurrent`（R6-9）

闪时送 runner 拿不到批次级跨进程锁时返回 `status="blocked_concurrent"`
（未发送任何 POST）。Bridge 映射：

```jsonc
// task:error payload
{
  "status": "blocked_concurrent", "result_status": "blocked_concurrent",
  "ok": false, "success": false, "real_order": false,
  "stopped": true, "partial": false, "blocked": true,
  "uncertain": false, "needs_review": false,
  "message": "另一个任务正在运行，请等待后刷新（本次未发送任何下单请求）",
  // runner 有更详细的说明时优先用它（例如“另一进程正在处理同一批次…未发送任何 POST”）；
  // runner 没给时 Bridge 兜底为“另一个任务正在运行，请等待后刷新”。
  "next_action": "另一进程正在处理同一批次或跨进程锁不可用；未发送任何 POST，请等待锁释放后重试…"
}
```

前端要求：

- **新增 `blocked_concurrent` 分支**，标题“另一个任务正在运行”，
  提示“请等待后刷新”，`level` 用 `warning`/`info`（不是 error），
  `needsReview=false`（不需要对账）；
- **不得**落入“站内对账失败 / 任务失败 / 结果不确定”分支：
  本次没有发送任何下单请求；
- 日志级别为 `WARN`。

---

## 9. `operation_status()` 新增 mode

只读入口也占互斥槽位，冲突时会把占用者显示出来：

```text
order / sss / wps_upload / wps_authorize / wps_logout / check_update / install_update
wps_preview / wps_check_copies / sss_day_orders / wps_recovery_resolve   ← 新增
```

因此 `operation_status().mode` 与 `_MODE_LABELS` 的中文名可用于“谁在跑”的提示：

| mode | 中文 |
| --- | --- |
| `wps_preview` | 云文档预览 |
| `wps_check_copies` | 云文档副本核对 |
| `sss_day_orders` | 云端当天名单读取 |
| `wps_recovery_resolve` | 旧任务恢复/退场 |

---

## 10. 验证证据（本契约的可复现命令）

```bash
# W1 真实 build_plan/apply_plan 流经 Bridge（含 stale 零写入 + 旧接线反证）
python3 -m pytest -q tests/test_bridge_wps_e2e.py

# W5/W7 服务端闸门与只读入口互斥
python3 -m pytest -q tests/test_bridge_wps_gates.py

# W3 管理员权限/确认/审计/重复请求保护
python3 -m pytest -q tests/test_bridge_wps_admin_recovery.py

# W6 计划口径 vs 执行口径
python3 -m pytest -q tests/test_bridge_wps_result_counts.py

# R6-9 blocked_concurrent 映射
python3 -m pytest -q tests/test_bridge_task_results.py

# HTTP 权限（普通用户 403 / 管理员恢复入口）
python3 -m pytest -q tests/test_web_roles.py

# 全量（本轮实测 1348 passed, exit 0）
python3 -m pytest -q
ruff check app tests
```

本轮独立复核结论（离线，合成数据 / 临时目录 / FakeCli）：

- W1 两层保护（B 的计划快照比较 + Bridge 的共享账本对象比较）**各自都能独立拦住**
  并发提交；两层同时关掉才复现旧接线的真实写云；
- 用 `os.fork()` 起真进程在窗口内提交账本 → 生产 Bridge 路径零云端写入；
- W7 冲突时云端调用次数为 0，真实线程并发下只读入口立即返回且无死锁；
- `wps_enabled=False` 时预览/上传连 `kdocs-cli` 都不会被调用。

---

## 11. 前端迁移清单（按优先级）

> 第 1、2、4、5 条是**独立复核在真实前端代码上实测到的用户可见缺陷**（不是理论风险）。
> 后端已经按契约返回正确字段；在 D 完成这些改动前，界面会显示下面“实测现象”一列。

| # | 文件 / 位置 | 必须改成 | 不改的实测现象 |
| --- | --- | --- | --- |
| 1 | `frontend/src/components/CloudForm.tsx:993`（`result.summary.to_update` 等） | 计划行数读 `planned_summary.rows.*` 并标注“计划”；成功行数读 `execution_summary.rows.verified`，为 `null` 时显示“实际写入行数未知，请只读核对云端” | **成功上传（计划 1 行 / 实际 1 行）渲染成“更新 0 · 新增 0 · 跳过 0 · 不变 0”**，真实写入行数无处可看 |
| 2 | `frontend/src/lib/bridge.ts` 的 `WpsUploadResult` 类型 | 补 `planned_summary` / `execution_summary` / `rows_unknown` / `summary_stats` / `counts_source` 字段 | 类型层拿不到新字段，后续改动继续踩坑 |
| 3 | 上传/预览失败卡片 | 按 `code` + `next_action` 渲染；只有 `execution_summary.proven_no_write === true` 时才可显示“未写入任何内容” | 可能把“未知”渲染成“没写过” |
| 4 | `frontend/src/lib/taskOutcome.ts`（`blocked_concurrent` 无分支，`payload.blocked=true` 命中“任务被阻断”分支） | 新增 `blocked_concurrent` 分支：标题“另一个任务正在运行”，`level`/`toast` 用 `warning`（或 `info`），`needsReview=false`，文案“请等待后刷新” | 实测输出 `{level:"error", toast:"error", title:"任务被阻断 · 待核对", needsReview:true}` |
| 5 | `frontend/src/lib/operationStatus.ts`：`viewOfOperation` 的 `default:` 分支；`operationViewFromStatus('blocked_concurrent')`；`operationModeLabel(...)` | `blocked_concurrent` 单独渲染成“另一个任务正在运行 / 等待后刷新”（`tone` 不要用 `danger`、`needsReview=false`）；`operationViewFromStatus` 补该状态而不是回退 `ready`；`operationModeLabel` 补 4 个新 mode 的中文名 | 实测：`viewOfOperation` → `{key:"error", label:"闪时送下单失败", tone:"danger", needsReview:true}`；`operationViewFromStatus('blocked_concurrent')` → 回退成**“就绪 / 没有正在执行的任务”**（与前者互相矛盾）；`operationModeLabel('wps_preview'…)` 返回原始英文 mode |
| 6 | 恢复面板 | `wps_recovery_status` 增加 `retired_guarded` 计数与 `wps_recovery_retired_guarded` 文案；管理员按钮接 `wps_recovery_resolve`（`[{...}]` 或 `{...}`，必须带 `confirm`=decision、`note`、退场的 `confirm_structure_checked=true`），重复点击处理 `already_retired` | 管理员没有解除阻断的入口 |
| 7 | 全局冲突提示 | 统一文案“另一个任务正在运行，请等待后刷新” | 各处文案不一致 |

`bridge.ts` 的 `OperationMode` / 状态联合类型也要补上
`wps_preview` / `wps_check_copies` / `sss_day_orders` / `wps_recovery_resolve`
与 `blocked_concurrent`（见第 8、9 节）。
