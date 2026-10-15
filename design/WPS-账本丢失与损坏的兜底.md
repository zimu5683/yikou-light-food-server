# WPS 账本丢失与损坏的兜底（第 14 项 · 待设计）

> **状态：缺口已补（2026-10-15 同日）。** 第 1~6 节保留当时的问题描述，作为设计
> 依据；第 7 节的方向已经落地，落点见第 [7.1](#71-已实现2026-10-15) 节。
> 当前行为以 [README.md](../README.md)、[docs/ARCHITECTURE.md](../docs/ARCHITECTURE.md)、
> [docs/WPS-SYNC-RULES.md](../docs/WPS-SYNC-RULES.md) 与
> [docs/BRIDGE-WPS-CONTRACT-R6.md](../docs/BRIDGE-WPS-CONTRACT-R6.md) §12 为准。

---

## 1. 一句话

云同步的幂等性完全押在本地账本这一个文件上，而它**没有备份、没有重建入口**，
并且在它损坏时连「恢复/退场」入口都打不开。要么补上兜底，要么接受「账本一丢就重复加餐」。

---

## 2. 两个文件是什么

| 文件 | 位置 | 作用 |
| --- | --- | --- |
| `wps_sync_state.json`（账本） | 用户配置目录（`app/wps/ledger.py` 的 `default_state_path()`） | **幂等锚点**：按「目标日期 + 云表 `file_id` + 人 + 槽位」记录「本批这一行**已入账**的累计量」 |
| `<账本>.journal`（意图日志） | 同目录（`journal_path_for()`） | 记录写入意图/未决操作，供崩溃后恢复「上次写到哪一步」 |
| `<journal>.archive` | 同目录 | 退场操作的归档（`app/wps/journal.py`） |

- 账本里一个人的键是 `姓名\u0000电话`（`_entry_key()`）；每个条目除了餐次还带
  槽位/订单号/物理位置（`slots` / `orders` / `positions`），所以补单插入、整表重排之后
  仍能按订单身份对上自己的那一行。
- 账本与 journal 共用一把跨进程锁（`app/wps/atomicio.py` 的 `lock_path_for()`
  把 `x.json` 与 `x.json.journal` 都映射到 `x.json.lock`）。

**核心公式**（`app/wps/planner.py`）：

```
本次增量 = 本地这一行的餐次 − 账本里「本批已同步的餐次」
```

锚点**只增不减**（云端只加不减，本地少写只提示、不自动回退）。预览里每行显示
「总餐次 before→after」与「本批首次同步 +N / 本批本地 a→b」（`planner.py:890-905`）。

---

## 3. 为什么不能简单删掉这个阻断

账本是**唯一**记录「哪一批已经落进云端」的地方。云端只存**总餐次**和日期格，
**不存**「哪一批加了多少」。所以锚点一旦丢失，增量从**任何地方都推算不出来**：

- 猜 0 → 重复加餐（最贵的错误，要人工按云端日期格往回减）；
- 猜「已同步」→ 静默漏加（用户以为送了、实际没加）。

阻断本身是对的。**缺口不是「阻断太严」，而是「阻断之后没有出路」。**

---

## 4. 四种失败形态与现状

### 4.1 账本不可读或损坏 —— 失败关闭，但没有出路

`SyncLedger._load()`：

- 文件不存在 → **静默当作空账本**（见 4.2）；
- `OSError` / `UnicodeDecodeError` → `LedgerCorruptError("账本文件不可读")`；
- JSON 解析失败 → `LedgerCorruptError("账本 JSON 损坏")`；
- 结构校验失败（`_validate_payload()`：根节点/batches/日期层/批次层/people 层/
  条目幂等锚点/slots 与 orders、positions 一致性）→ 同样抛 `LedgerCorruptError`。

所有入口据此**失败关闭、零云端写入**：`app/api/bridge.py` 的
`_wps_context_bundle()` / `_wps_read_plans()` / 执行路径捕获
`LedgerCorruptError` / `JournalError` / `JournalCorruptError`，返回 `local_state_blocked`
（`proven_no_write=True`），文案「本地账本/意图日志不可用：…」
「只读核对；先修复本地日志/账本后重新预览」。

**问题在这里**：恢复/退场入口自己也要先建账本快照（`_wps_recovery_snapshot()`），
所以账本坏时它返回 `wps_recovery_ledger_unreadable`，
`_wps_recovery_resolve_impl()` 直接以
「本地账本/意图日志不可用，拒绝任何恢复动作 / 先修复本地日志/账本」拒绝。
**而界面文案（`frontend/src/components/CloudForm.tsx` 的 `journalIssueOf`）恰恰写着
「先修复（可走恢复/退场入口）」——指向一个此刻打不开的入口。**
这是要修的第一处矛盾：要么让入口可用，要么把文案改成真实可执行的步骤。

### 4.2 账本丢失 —— 静默当空账本，整批重加

`_load()` 对 `FileNotFoundError` 直接 `return`，`self.data` 保持
`{"version":1,"batches":{}}`。**无法区分「全新安装」与「账本被删/丢」**。

后果：每个人取不到锚点（`ledger_prev is None`）→ 预览显示「本批首次同步 +N」→
**整批重新加一遍**。这是已知隐患，[docs/WPS-SYNC-RULES.md](../docs/WPS-SYNC-RULES.md)
§2 末条自己写着：「账本丢了（卸载重装、清应用数据）才可能把同一批再加一次 ——
上传前先点「预览」，核对每行的「本次 +N」即可发现。」

**危险点在于：账本丢失时预览看起来完全正常**（每人都是一条「首次同步 +N」），
只有人工去比云端总餐次才能发现。这是目前唯一的兜底，且完全依赖人。

### 4.3 journal 不可读或损坏 —— 相对轻

锚点在账本里、仍然完好，**增量仍然安全**；丢的只是「上次写到哪一步」的追踪。
`recovery_status()` 把它归类为 `wps_recovery_journal_unreadable`，下一步 `fix_journal`。
journal 有 `<journal>.archive` 可留档。

### 4.4 账本写盘失败（磁盘满/权限）

记 ERROR 日志，并提示「下次上传可能重复加餐，请先预览核对」
（[docs/WPS-SYNC-RULES.md](../docs/WPS-SYNC-RULES.md) §6）。
`tests/test_wps_recovery.py::test_ledger_save_failure_keeps_previous_disk_state`
锁定「失败时不破坏磁盘上已有的旧状态」。

---

## 5. 现在用户看到什么

- 云文档页「刷新状态（只读）」→ 恢复卡片：`本地恢复记录不可读` /
  `本地账本/意图日志损坏：先修复（可走恢复/退场入口），不要删除账本，也不要直接重传。`
  （按 `error_code` 分为版本不受支持 / 持久化失败 / 损坏 / 不可读四种提示）。
- 预览或上传 → `local_state_blocked`，`proven_no_write=true`（明确零写入）。
- `wps_status`：账本读不出来时 `ok=false` 且带 `reason`。
- **没有任何一个入口能告诉用户「现在该怎么办才能继续上传」。**

---

## 6. 现成的先例与缺口

**已有先例可借**

- `config.json` 替换前会留一份 `.bak`（`app/core/config.py`）——账本**没有**这个待遇；
- journal 有 `.archive`，账本**没有**备份/归档；
- 预览里每人都有「总餐次 before→after」「本批本地 a→b」，是现成的人工对账面；
- 写入本身是原子替换 + 跨进程锁（`app/wps/atomicio.py`），「写坏一半」这条已经堵住。

**缺口**

1. 账本**无备份**（无 `.bak`、无归档、无多代快照）；
2. **无「以本地餐次为准重建锚点」的入口**；
3. 账本损坏时**恢复入口不可用**（4.1）；
4. **「缺失」与「全新安装」不可区分**（4.2）；
5. 用户拿到的提示只到「先修复」，没有可执行的修复路径。

---

## 7. 设计要覆盖的点（方向，不是方案）

1. **把「缺失 / 损坏 / 不可读」分开处理**；「缺失但 journal 或历史痕迹还在」
   应当视为**可疑**，而不是当全新安装（这是 4.2 的关键）。
2. **最便宜的加固**：账本每天首次写入前留 `.bak`（可留几代），
   仿 `config.json` 的既有做法。
3. **半自动重建锚点**：给一个「以本地餐次为准重建」的入口 + 显式确认，
   方向永远**宁漏勿重**（漏加可以人工补，重复加要人工按云端日期格往回减）。
4. **让恢复入口在账本损坏时可用**，或者给出一份有文档的安全手工流程，
   并把界面文案改成与之一致（4.1 的矛盾）。
5. **明确「可疑」时的默认动作**：宁可拒绝写入并要求人工核对，
   也不要静默按空账本重加。

---

### 7.1 已实现（2026-10-15）

| 第 7 节的点 | 落点 |
| --- | --- |
| 1. 「缺失 / 丢失 / 损坏 / 不可读」分开 | `Bridge._wps_ledger_state()` 出 `ledger_state`（`ok` / `missing` / `missing_with_history` / `corrupt` / `unreadable`）；`SyncLedger` 缺文件时不再静默当空账本，而是按同目录痕迹区分"全新安装"与"以前同步过" |
| 2. 最便宜的加固 | `app/wps/atomicio.py`：落盘前 `rotate_backups()` 留 `.bak` 三代 + 每日快照（保留 7 天）；损坏/不可读时 `preserve_file()` 把原件留成 `<账本>.corrupt-<内容摘要>`，绝不悄悄丢掉 |
| 2b. 坏账本抢救 | `salvage_payload()` / `salvage_ledger()`（`app/wps/ledger.py`）+ Bridge 的 `wps_ledger_salvage`：逐层丢掉坏的日期/表/人，好的锚点原样留下；判据与严格校验**同源**，写前还有自查。清单写明丢的是**谁** |
| 3. 半自动重建锚点 | `app/wps/rebuild.py` + Bridge 的 `wps_ledger_rebuild`：**两段式**（不带 `confirm` 只返回只读提案；带 `confirm="rebuild"` 才写本地账本，先留档原件）。逐行判定：云端没有这个人 → 留空；目标日期格已有值 → 记已入账；格子空 → 留空 |
| 4. 恢复入口在账本损坏时可用 | `_wps_recovery_snapshot()` 改为 `SyncJournal(journal_path_for(账本路径))` 直接读日志：恢复面板照常显示待处置任务；`retire_guarded` / `keep` 可执行，`cloud_verified` / `cloud_untouched` 明确要求先修好账本 |
| 5. 「可疑」时的默认动作 | **可证明**的丢失（意图日志有 `verified` 锚点、账本里查不到）→ 预览/上传以 `local_ledger_lost` **失败关闭**，一个格子都不写；只有痕迹、证明不了 → 预览里加警告，不阻断 |
| 4（文案） | `frontend/src/lib/wpsFailure.ts`、`frontend/src/components/CloudForm.tsx`、`frontend/src/lib/ledgerRepair.ts` 的文案改为指向真实入口（重建卡片 + 确认弹窗） |

明确**没有**改的：阻断语义（`local_state_blocked` 仍失败关闭）、
[BRIDGE-WPS-CONTRACT-R6](../docs/BRIDGE-WPS-CONTRACT-R6.md) 的既有字段与铁律（只做增量）、
与桌面端刻意不同的结果分类。重建**不写云端**，也**不解除**防重复闸门 —— 闸门（意图
日志）与锚点（账本）是两条独立防线。

**仍然不做**：自动抢救。抢救**永远**要人工确认（逐字输入 `salvage` + 核对说明），
程序不会自己决定丢掉谁的锚点。连 JSON 都解析不了的文件也救不了 —— 那条路走重建。

## 8. 相关代码与测试索引

**代码**

- `app/wps/ledger.py` — `default_state_path()` / `_entry_key()` / `_validate_payload()` /
  `SyncLedger._load()` / `save()` / `_merged_data()` / `empty()` / `rebuild_anchors()`
- `app/wps/rebuild.py` — 锚点重建的判定规则（`build_rebuild()` / `write_rebuild()` /
  `verify_rebuild()` / `format_rebuild()`）
- `app/wps/ledger.py` 的 `_entry_problem()` / `salvage_payload()` / `salvage_ledger()`
  — 坏账本的分层抢救（与严格校验同源）
- `app/wps/atomicio.py` — `rotate_backups()` / `preserve_file()`（备份与留档）
- `app/wps/journal.py` — `journal_path_for()` / `SyncJournal` / `.archive`
- `app/wps/atomicio.py` — `atomic_write_text()` / `FileLock` / `lock_path_for()`
- `app/wps/planner.py` — 取锚点 `synced_slots/synced_orders/synced_positions()`；
  增量与预览文案
- `app/wps/recovery.py` — `recovery_status()` / `_recovery_status_full()` /
  `_journal_for_ledger()` / `_empty_recovery_status()` / `_safe_error_code_from_reason()`
- `app/api/bridge.py` — `_wps_context_bundle()` / `_wps_read_plans()` / 执行路径 /
  `_wps_recovery_snapshot()` / `_wps_recovery_resolve_impl()` / `wps_status`
- `app/core/config.py` — `.bak` 先例
- `frontend/src/components/CloudForm.tsx` — `journalIssueOf` 的用户文案

**测试**

- `tests/test_wps_recovery.py:545` `test_corrupt_ledger_is_rejected_not_treated_as_empty`
  （损坏**不得**当空账本）
- `tests/test_wps_recovery.py:849` `test_ledger_entry_without_anchor_is_rejected`
- `tests/test_wps_recovery.py:992` `test_ledger_save_failure_keeps_previous_disk_state`
- `tests/test_wps_recovery.py:1383` `test_corrupt_ledger_after_old_object_blocks_before_cloud_write`
- `tests/test_wps_ledger_backup.py` — 备份/留档/丢失证据/重建落盘
- `tests/test_wps_ledger_rebuild.py` — 逐行判定规则与指纹
- `tests/test_wps_ledger_salvage.py` — 分层丢弃、清单内容、抢救不变式与写回
- `tests/test_bridge_wps_ledger_repair.py` — Bridge 层：恢复可用性、丢失阻断、重建入口
- `tests/wps_ledger_harness.py` — 上面三个文件共用的合成云端与排单表
- `tests/test_wps_cloud_safety.py:455` `test_apply_plan_without_a_ledger_still_works`
- `tests/test_bridge_wps_result_counts.py:235`
  `test_w6_ledger_corrupt_during_apply_is_blocked_with_unknown_rows`

---

## 9. 明确不在本次范围

- 不改阻断本身（`local_state_blocked` 的失败关闭语义保持）；
- 不改 `docs/BRIDGE-WPS-CONTRACT-R6.md` 的字段契约与「铁律」；
- 不改与桌面端**刻意不同**的结果分类语义；
- 第 7 节的方向已由第 7.1 节落地（2026-10-15）；本文其余部分仍只是当时的记录。
