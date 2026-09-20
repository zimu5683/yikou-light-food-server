"""意图日志的只读恢复对账：只相信云端实际读值，不自动回滚/不重复追加。

恢复只做三件事：

1. 按日志里的目标表/日期/姓名/电话/槽位重新读云端；
2. 把每个待写槽位分类为 ``verified``（期望值全部成立）、``not_started``
   （原值/行基线完全没动）或 ``uncertain``（部分写、读不到、结构不符）；
3. 只有 ``verified`` 才合并账本锚点；``not_started`` 只标记可重预览；
   ``uncertain`` 阻断后续写入并给人工核对指引。
"""
from __future__ import annotations

import copy
import datetime as _dt
import hashlib
import re
from collections import Counter
from typing import Any, Mapping

from app.wps.cli import KdocsCli
from app.wps.common import (
    CELL_MARK,
    FIRST_DATA_ROW,
    HEADER_ROW,
    MAX_READ_CELLS,
    MAX_SCAN_ROW,
    _address_key,
    _as_int,
    column_name,
    parse_date_header,
    person_key,
)
from app.wps.atomicio import (AtomicWriteError, FileLock, LockTimeout,
                              lock_path_for, operation_lock_path_for)
from app.wps.errors import WpsCloudError
from app.wps.journal import OP_STATUSES, SHEET_STATUSES, SyncJournal, journal_path_for
from app.wps.ledger import SyncLedger


def _uncertain(reason: str, problems: list[str] | None = None) -> dict[str, Any]:
    return {"state": "uncertain", "reason": reason,
            "problems": problems or [], "next_action": "manual_reconcile"}


def _target_ref(file_id: Any, sheet: Any, target_date: Any) -> str:
    """给 A/D 的相关联目标标识；不回传原始 WPS file_id。"""
    raw = f"{file_id}|{sheet}|{target_date}".encode("utf-8")
    return f"wps-target:{hashlib.sha256(raw).hexdigest()[:12]}"


def _read_region(cli: KdocsCli, file_id: str, worksheet_id: int,
                 row_from: int, row_to: int, col_from: int, col_to: int
                 ) -> dict[tuple[int, int], str]:
    """按单次读取上限分块读取 0-based 闭区间，返回 0-based 键。"""
    if row_to < row_from or col_to < col_from:
        return {}
    width = col_to - col_from + 1
    chunk = max(1, MAX_READ_CELLS // max(1, width), 1)
    grid: dict[tuple[int, int], str] = {}
    start = row_from
    while start <= row_to:
        end = min(row_to, start + chunk - 1)
        part = cli.read_grid(file_id, worksheet_id, start, end, col_from, col_to)
        grid.update(part)
        start = end + 1
    return grid


def _read_formulas(cli: KdocsCli, file_id: str, worksheet_id: int,
                   row_from: int, row_to: int, col_from: int, col_to: int
                   ) -> dict[tuple[int, int], str]:
    if row_to < row_from or col_to < col_from:
        return {}
    return cli.read_formulas(file_id, worksheet_id, row_from, row_to, col_from, col_to)


def _cell(grid: Mapping[tuple[int, int], Any], row: int, col: int) -> str:
    """1-based 行/列取值；统一去空白。"""
    if not row or not col:
        return ""
    return str(grid.get((row - 1, col - 1), "") or "").strip()


def _slot_key(record: Mapping[str, Any]) -> tuple[int, str]:
    return int(record.get("slot", 0)), ""


def _as_int_field(intent: Mapping[str, Any], key: str) -> int:
    try:
        return int(intent.get(key, 0))
    except (TypeError, ValueError):
        return 0


def _expected_formulas(record: Mapping[str, Any], intent: Mapping[str, Any],
                       row: int) -> tuple[str, str] | None:
    if not intent.get("formula_needed") or not row:
        return None
    columns = record.get("columns") or {}
    date_cols = [int(c) for c in (record.get("date_cols") or []) if int(c or 0) > 0]
    served = int(columns.get("served") or 0)
    left = int(columns.get("left") or 0)
    total = int(columns.get("total") or 0)
    if not (date_cols and served and left and total):
        return None
    return (
        f"=SUM({column_name(min(date_cols))}{row}:{column_name(max(date_cols))}{row})",
        f"={column_name(total)}{row}-{column_name(served)}{row}",
    )


def classify_journal_sheet(cli: KdocsCli,
                           record: Mapping[str, Any]) -> dict[str, Any]:
    """只读云端，对一条子表日志分类；任何异常/歧义都归为 uncertain。"""
    raw_status = str(record.get("status") or "")
    if raw_status not in SHEET_STATUSES:
        return _uncertain("unsupported_status")
    try:
        file_id = str(record.get("file_id") or "")
        worksheet_id = int(record.get("worksheet_id") or 0)
        target_col = int(record.get("target_col") or 0)
        columns = record.get("columns") or {}
        intent_list = record.get("intents") or []
        if not isinstance(columns, Mapping) or not isinstance(intent_list, list):
            return _uncertain("journal_invalid")
        if not file_id or not worksheet_id or not target_col:
            return _uncertain("journal_invalid")
        target = _dt.date.fromisoformat(str(record.get("target_date") or ""))
    except (TypeError, ValueError) as exc:
        return _uncertain(f"journal_invalid:{exc}")

    name_col = int(columns.get("name") or 1)
    phone_col = int(columns.get("phone") or (name_col + 1))
    address_col = int(columns.get("address") or 0)
    type_col = int(columns.get("type") or 0)
    kind_col = int(columns.get("kind") or 0)
    total_col = int(columns.get("total") or 0)
    served_col = int(columns.get("served") or 0)
    left_col = int(columns.get("left") or 0)
    marker_col = int(record.get("marker_col") or 0)
    marker_expected = str(record.get("marker_expected") or "")
    sort_key_col = int(record.get("sort_key_col") or 0)

    base_rows = [
        int(record.get("last_data_row") or 0),
        int(record.get("append_row") or 0),
        max([int(item[0]) for item in (record.get("baseline_name_rows") or [])] or [0]),
        max([int(i.get("row_hint") or 0) for i in intent_list] or [0]),
    ]
    row_from = HEADER_ROW - 1
    row_to = max([FIRST_DATA_ROW, *base_rows]) + 10
    row_to = min(row_to, MAX_SCAN_ROW)
    cols = [name_col, phone_col, target_col]
    cols += [c for c in (address_col, type_col, kind_col, total_col, served_col, left_col)
             if c]
    cols += [int(c) for c in (record.get("date_cols") or []) if int(c or 0) > 0]
    if marker_expected and marker_col:
        cols.append(marker_col)
    if sort_key_col:
        cols.append(sort_key_col)
    col_from, col_to = min(cols) - 1, max(cols) - 1

    try:
        grid = _read_region(cli, file_id, worksheet_id, row_from, row_to,
                            col_from, col_to)
        header_text = _cell(grid, HEADER_ROW, target_col)
        parsed = parse_date_header(header_text)
        if parsed != (target.month, target.day):
            return _uncertain(
                "target_date_mismatch",
                [f"第 {target_col} 列表头为「{header_text}」，目标日期为 {target.isoformat()}"])
        formula_needed = any(
            bool(i.get("formula_needed")) for i in intent_list
            if isinstance(i, Mapping))
        formula_grid = _read_formulas(
            cli, file_id, worksheet_id, row_from, row_to, col_from, col_to
        ) if formula_needed else {}
    except WpsCloudError as exc:
        return _uncertain(f"cloud_unreadable:{exc}")
    except Exception as exc:  # noqa: BLE001 - 恢复路径不允许把未知异常当成功
        return _uncertain(f"recovery_error:{type(exc).__name__}:{exc}")

    # 读取云端人员行（姓名+电话做键，保留槽位顺序）。
    rows_by_key: dict[tuple[str, str], list[int]] = {}
    actual_signature: list[tuple[int, str, str]] = []
    address_by_row: dict[int, str] = {}
    for (row0, col0), text in grid.items():
        if row0 < FIRST_DATA_ROW - 1 or col0 != name_col - 1:
            continue
        name = str(text or "").strip()
        if not name:
            continue
        phone = _cell(grid, row0 + 1, phone_col)
        key = person_key(name, phone)
        row = row0 + 1
        rows_by_key.setdefault(key, []).append(row)
        actual_signature.append((row, name, key[1]))
        if address_col:
            address_by_row[row] = _cell(grid, row, address_col)
    for rows in rows_by_key.values():
        rows.sort()
    actual_signature.sort()

    baseline_raw = record.get("baseline_name_rows") or []
    baseline_signature: list[tuple[int, str, str]] = []
    try:
        for item in baseline_raw:
            baseline_signature.append((int(item[0]), str(item[1]), str(item[2])))
    except (TypeError, ValueError, IndexError) as exc:
        return _uncertain(f"journal_invalid_baseline:{exc}")
    baseline_signature.sort()
    baseline_counts = Counter((name, phone) for _row, name, phone in baseline_signature)

    # 意图槽位映射：优先使用排序身份标记（若崩溃时辅助列还在），否则
    # 新增槽位按地址优先匹配、老槽位按剩余行升序，避免 C8 的“新行排前面”
    # 导致 slot 与行号错位。
    mapped_rows: dict[int, int] = {}
    if sort_key_col:
        token_to_row: dict[str, int] = {}
        for (row0, col0), text in grid.items():
            if col0 != sort_key_col - 1:
                continue
            raw = str(text or "")
            if ":" in raw:
                token = raw.rsplit(":", 1)[1].strip()
                if token:
                    token_to_row[token] = row0 + 1
        for index, item in enumerate(intent_list):
            if not isinstance(item, Mapping):
                continue
            token = str(item.get("sort_token") or "")
            if token and token in token_to_row:
                mapped_rows[index] = token_to_row[token]

    for key, candidates in rows_by_key.items():
        pending = [(idx, item) for idx, item in enumerate(intent_list)
                   if isinstance(item, Mapping)
                   and (str(item.get("name") or ""), str(item.get("phone_key") or "")) == key
                   and idx not in mapped_rows]
        if not pending:
            continue
        used = {int(row) for row in mapped_rows.values()}
        free = [row for row in candidates if row not in used]
        # 新增槽位先按地址匹配，避免把新行和旧行按行号硬对成 slot1/slot2。
        for idx, item in sorted(
                (pair for pair in pending if pair[1].get("kind") == "new"),
                key=lambda pair: (int(pair[1].get("slot") or 0), pair[0])):
            wanted = _address_key(item.get("address"))
            chosen = next((row for row in free if wanted and _address_key(address_by_row.get(row)) == wanted),
                          None)
            if chosen is None:
                slot = int(item.get("slot") or 0)
                chosen = free[slot - 1] if 0 < slot <= len(free) else (free[0] if free else None)
            if chosen is None:
                break
            mapped_rows[idx] = chosen
            free.remove(chosen)
        for idx, item in sorted(
                (pair for pair in pending if pair[0] not in mapped_rows),
                key=lambda pair: (int(pair[1].get("slot") or 0), pair[0])):
            if not free:
                break
            wanted = _address_key(item.get("address"))
            chosen = next((row for row in free if wanted and _address_key(address_by_row.get(row)) == wanted),
                          free[0])
            mapped_rows[idx] = chosen
            free.remove(chosen)

    # 结构核对分两套：
    #   verified：基线 + 本次新建槽位；
    #   not_started：必须与基线逐行一致（插入/排序都没痕迹）。
    expected_counts = Counter(baseline_counts)
    for item in intent_list:
        if not isinstance(item, Mapping):
            continue
        key = (str(item.get("name") or ""), str(item.get("phone_key") or ""))
        if item.get("kind") == "new":
            expected_counts[key] += 1
    actual_counts = Counter({key: len(rows) for key, rows in rows_by_key.items()})
    expected_keys = {key for key, count in expected_counts.items() if count}
    verified_problems: list[str] = []
    if set(actual_counts) != expected_keys:
        extra = sorted(key[0] for key in set(actual_counts) - expected_keys)
        missing = sorted(key[0] for key in expected_keys - set(actual_counts))
        if extra:
            verified_problems.append(f"云端多出人员行：{'、'.join(extra[:5])}")
        if missing:
            verified_problems.append(f"云端缺少人员行：{'、'.join(missing[:5])}")
    for key, count in expected_counts.items():
        if count and int(actual_counts.get(key, 0)) != int(count):
            verified_problems.append(
                f"{key[0]}：云端 {actual_counts.get(key, 0)} 行，期望 {count} 行")
    structure_ok = not verified_problems
    baseline_counts_actual = Counter((name, phone) for _row, name, phone in actual_signature)
    baseline_structure_ok = (actual_signature == baseline_signature
                             and baseline_counts_actual == baseline_counts)

    marker_actual = _cell(grid, HEADER_ROW, marker_col) if (marker_expected and marker_col) else ""
    marker_ok = (not marker_expected) or marker_actual == marker_expected
    helper_nonempty = False
    if sort_key_col:
        helper_nonempty = any(
            _cell(grid, row, sort_key_col) for row in range(FIRST_DATA_ROW, row_to + 1)
        )

    expected_matches: list[bool] = []
    original_matches: list[bool] = []
    problems: list[str] = []
    for index, item in enumerate(intent_list):
        if not isinstance(item, Mapping):
            problems.append(f"意图 {index} 结构非法")
            expected_matches.append(False)
            original_matches.append(False)
            continue
        name = str(item.get("name") or "")
        slot = int(item.get("slot") or 0)
        row = mapped_rows.get(index)
        expected_total = _as_int_field(item, "total_after")
        before_total = _as_int_field(item, "total_before")
        expected_target = str(item.get("target_expected") or CELL_MARK)
        before_target = str(item.get("target_before") or "")
        kind = str(item.get("kind") or "")
        actual_total = _as_int(_cell(grid, row or 0, total_col)) if (row and total_col) else None
        actual_target = _cell(grid, row or 0, target_col) if row else ""
        actual_type = _cell(grid, row or 0, type_col) if (row and type_col) else ""
        actual_kind = _cell(grid, row or 0, kind_col) if (row and kind_col) else ""
        actual_address = address_by_row.get(row or 0, "") if row else ""

        want_type = str(item.get("meal_type") or "")
        want_kind = str(item.get("meal_kind") or "")
        need_type = bool(type_col and want_type and (kind == "new" or item.get("fill_type")))
        need_kind = bool(kind_col and want_kind and (kind == "new" or item.get("fill_kind")))
        formulas = _expected_formulas(record, item, row or 0)
        formula_ok = True
        if formulas and row:
            exp_served, exp_left = formulas
            formula_ok = (str(formula_grid.get((row - 1, served_col - 1), "")) == exp_served
                          and str(formula_grid.get((row - 1, left_col - 1), "")) == exp_left)

        exp_match = bool(row)
        if total_col and actual_total != expected_total:
            exp_match = False
        if actual_target != expected_target:
            exp_match = False
        if need_type and actual_type != want_type:
            exp_match = False
        if need_kind and actual_kind != want_kind:
            exp_match = False
        if kind == "new" and address_col and item.get("address"):
            if _address_key(actual_address) != _address_key(item.get("address")):
                exp_match = False
        if not formula_ok:
            exp_match = False
        expected_matches.append(exp_match)

        if kind == "new":
            orig_match = row is None
        else:
            orig_match = bool(row)
            if total_col and actual_total != before_total:
                orig_match = False
            if actual_target != before_target:
                orig_match = False
            if need_type and actual_type != "":
                orig_match = False
            if need_kind and actual_kind != "":
                orig_match = False
            if formulas and row:
                exp_served, exp_left = formulas
                if (str(formula_grid.get((row - 1, served_col - 1), "")) == exp_served
                        and str(formula_grid.get((row - 1, left_col - 1), "")) == exp_left):
                    orig_match = False
        original_matches.append(orig_match)

        if not exp_match:
            problems.append(
                f"{name} 槽位 {slot}：期望 total={expected_total}/mark={expected_target!r}，"
                f"实际 total={actual_total}/mark={actual_target!r}"
                + (f"/类型={actual_type!r}/餐种={actual_kind!r}" if (need_type or need_kind) else "")
            )

    all_expected = bool(intent_list) and all(expected_matches)
    all_original = all(original_matches) and baseline_structure_ok
    if helper_nonempty:
        verified_problems.append("排序辅助列仍有残留内容")
        structure_ok = False
    if verified_problems:
        problems = verified_problems + problems
    if all_expected and structure_ok and marker_ok and not helper_nonempty:
        return {"state": "verified", "reason": "", "problems": [],
                "next_action": "none"}
    if all_original and baseline_structure_ok and not helper_nonempty and not (
            marker_expected and marker_actual == marker_expected):
        return {"state": "not_started", "reason": "云端与基线一致，未发现本操作写入痕迹",
                "problems": [], "next_action": "repreview"}
    if not problems:
        problems.append("云端状态既不是完整期望值，也不是完整原值：无法确认是否写入")
    if marker_expected and not marker_ok:
        problems.append(f"通讯记号应为 {marker_expected}，实际 {marker_actual!r}")
    return _uncertain("partial_or_ambiguous", problems[:20])


def _merge_into_ledger(ledger: SyncLedger, target_date: str, file_id: str,
                       entries: Mapping[str, Any]) -> tuple[bool, str, dict[str, Any]]:
    """在给定账本快照上计算恢复合并结果；不落盘。"""
    to_record: dict[str, Any] = {}
    for storage_key, payload in entries.items():
        if not isinstance(payload, Mapping):
            return False, "journal_ledger_entry_invalid", {}
        name, separator, phone = str(storage_key).partition("\u0000")
        if not separator:
            return False, "journal_ledger_key_invalid", {}
        existing = ledger.synced_slots(target_date, file_id, name, phone)
        wanted = payload.get("slots")
        if existing is None:
            to_record[str(storage_key)] = dict(payload)
            continue
        old_slots = [int(v) for v in existing]
        new_slots = [int(v) for v in wanted] if wanted is not None else []
        if old_slots == new_slots:
            continue          # 已经恢复过，幂等
        if old_slots == new_slots[:len(old_slots)]:
            # 本地新增了槽位：新 slots 以旧 slots 为前缀，补全幂等锚点。
            to_record[str(storage_key)] = dict(payload)
            continue
        if new_slots == old_slots[:len(new_slots)]:
            # 本次云端验证只涉及较少槽位：旧账本已有更长前缀锚点，保持不动。
            continue
        return False, f"ledger_slot_conflict:{name}", {}
    return True, "", to_record


def _merge_ledger(ledger: SyncLedger | None, record: Mapping[str, Any]) -> tuple[bool, str]:
    """恢复证明完整成功后，把日志里的账本锚点安全并入磁盘账本。

    持 :func:`lock_path_for` 数据锁并重新读取磁盘快照；两个进程同时恢复同一
    批次时，后到者看到已存在且相等的 slots 会直接跳过，不会重复确认或覆盖。
    """
    entries = record.get("ledger_entries") or {}
    if not isinstance(entries, Mapping):
        return False, "journal_ledger_entries_invalid"
    if not entries:
        return True, ""
    if ledger is None:
        return False, "ledger_missing"
    target_date = str(record.get("target_date") or "")
    file_id = str(record.get("file_id") or "")
    path = getattr(ledger, "path", None)
    snapshot = copy.deepcopy(ledger.data)
    snapshot_digest = getattr(ledger, "_loaded_digest", None)
    try:
        if path:
            with FileLock(lock_path_for(path), timeout=30.0):
                fresh = SyncLedger(path)
                ok, reason, to_record = _merge_into_ledger(
                    fresh, target_date, file_id, entries)
                if not ok:
                    return False, reason
                if to_record:
                    fresh.record(target_date, file_id, to_record)
                    fresh._save_unlocked()
                ledger.data = fresh.data
                ledger._loaded_digest = fresh._loaded_digest
        else:
            ok, reason, to_record = _merge_into_ledger(
                ledger, target_date, file_id, entries)
            if not ok:
                return False, reason
            if to_record:
                ledger.record(target_date, file_id, to_record)
                ledger.save()
    except BaseException as exc:
        ledger.data = snapshot
        if hasattr(ledger, "_loaded_digest"):
            ledger._loaded_digest = snapshot_digest
        if not isinstance(exc, Exception):
            raise
        return False, f"ledger_merge_failed:{type(exc).__name__}:{exc}"
    return True, ""


def _refresh_journal_snapshot(journal: Any, ledger: SyncLedger | None = None) -> Any:
    """恢复前在同一 operation 锁内重读持久化 journal，避免旧对象漏 pending。

    canonical ``<ledger>.journal`` 是唯一落盘权威：若调用者另传路径或内存 journal，
    把可读到的旧 pending 先并入 canonical，再让对象指向 canonical。
    """
    if not isinstance(journal, SyncJournal):
        return journal
    path = getattr(journal, "path", None)
    if path:
        fresh = SyncJournal(path)
        try:
            old_pending = journal.pending_operations()
        except Exception:  # noqa: BLE001
            old_pending = {}
        operations = fresh.operations()
        for operation_id, operation in old_pending.items():
            if operation_id not in operations and isinstance(operation, dict):
                operations[operation_id] = operation
        journal.data = fresh.data
        if hasattr(journal, "_removed_operations"):
            journal._removed_operations.clear()
    ledger_path = getattr(ledger, "path", None) if ledger is not None else None
    if ledger_path:
        canonical_path = journal_path_for(ledger_path)
        if str(path or "") != str(canonical_path):
            canonical = SyncJournal(canonical_path)
            operations = canonical.operations()
            try:
                provided_pending = journal.pending_operations()
            except Exception:  # noqa: BLE001
                provided_pending = {}
            for operation_id, operation in provided_pending.items():
                if operation_id not in operations and isinstance(operation, dict):
                    operations[operation_id] = operation
            journal.data = canonical.data
            journal.path = canonical.path
            if hasattr(journal, "_removed_operations"):
                journal._removed_operations.clear()
    return journal


def _recovery_lock_target(ledger: SyncLedger | None, journal: Any) -> Any:
    base = getattr(ledger, "path", None) if ledger is not None else None
    if not base:
        base = getattr(journal, "path", None)
    if not base:
        return None
    name = str(base)
    if name.endswith(".journal"):
        name = name[: -len(".journal")]
    return operation_lock_path_for(name)


def _recover_pending_operations_impl(cli: KdocsCli, journal: Any,
                                     ledger: SyncLedger | None = None,
                                     log: Any = None) -> dict[str, Any]:
    """恢复日志中所有未完成操作；调用方已持 operation 锁。"""
    emit = log or (lambda *_args, **_kwargs: None)
    operations: list[dict[str, Any]] = []
    recovered: list[str] = []
    not_started: list[str] = []
    uncertain: list[str] = []
    needs_repreview = False
    for operation_id, op in journal.pending_operations().items():
        sheet_reports: list[dict[str, Any]] = []
        op_uncertain = False
        op_recovered_all = True
        op_not_started = False
        # 未知 / 不支持状态：保留 sheet 原始审计值，只把 op 标记为 uncertain，
        # 不读云端、不分类、不改成 verified/not_started。
        unsupported = []
        checker = getattr(journal, "unsupported_statuses", None)
        if callable(checker):
            try:
                unsupported = list(checker(op))
            except Exception:  # noqa: BLE001
                unsupported = ["<unreadable>"]
        if unsupported:
            op_uncertain = True
            op_recovered_all = False
            op["status"] = "uncertain"
            op["next_action"] = "manual_reconcile"
            op["unsupported_status_preserved"] = True
            op["unsupported_status_values"] = unsupported[:5]
            op["updated_at"] = _dt.datetime.now().isoformat(timespec="microseconds")
            for sheet_key, record in (op.get("sheets") or {}).items():
                if not isinstance(record, dict):
                    continue
                sheet_reports.append({
                    "sheet": record.get("sheet", sheet_key),
                    "file_id": record.get("file_id", ""),
                    "status": "uncertain",
                    "raw_status": str(record.get("status") or ""),
                    "next_action": "manual_reconcile",
                    "unsupported": True,
                })
            emit(f"[云同步恢复] {operation_id}：发现不支持的状态 "
                 f"{'、'.join(unsupported[:3])}，已保留原始审计值并继续阻断",
                 "ERROR")
            try:
                journal.save()
            except Exception as exc:  # noqa: BLE001 - 日志仍不可写则本次恢复结果不可信
                op_uncertain = True
                emit(f"[云同步恢复] 意图日志保存失败：{exc}", "ERROR")
            uncertain.append(operation_id)
            operations.append({
                "operation_id": operation_id,
                "status": "uncertain",
                "next_action": "manual_reconcile",
                "sheets": sheet_reports,
            })
            continue
        for sheet_key, record in (op.get("sheets") or {}).items():
            if not isinstance(record, dict):
                continue
            status = str(record.get("status") or "")
            if status in ("verified", "failed_no_write", "not_started"):
                if status == "not_started":
                    op_not_started = True
                sheet_reports.append({"sheet": record.get("sheet", sheet_key),
                                      "file_id": record.get("file_id", ""),
                                      "status": status,
                                      "next_action": record.get("next_action", "")})
                continue
            # 上次已明确知道“插入/回滚未确认”的，不允许靠只读云端把它降级成
            # not_started：空行残留在姓名读取里是看不见的（WPS-C9）。
            if record.get("manual_required"):
                reason = str(record.get("manual_required"))
                problems = ["上次操作未能确认插入/回滚，必须人工核对云端结构"]
                op_recovered_all = False
                op_uncertain = True
                journal.set_sheet_status(
                    operation_id, sheet_key, "uncertain",
                    reason=reason, next_action="manual_reconcile",
                    problems=problems,
                    cloud_checked=bool(record.get("cloud_checked", False)),
                    evidence=str(record.get("evidence") or "local_journal"))
                sheet_reports.append({"sheet": record.get("sheet", sheet_key),
                                      "file_id": record.get("file_id", ""),
                                      "status": "uncertain", "reason": reason,
                                      "problems": problems,
                                      "cloud_checked": bool(record.get("cloud_checked", False)),
                                      "evidence": str(record.get("evidence") or "local_journal"),
                                      "next_action": "manual_reconcile"})
                emit(f"[云同步恢复] {record.get('sheet', sheet_key)}：上次{reason}，"
                     f"已阻断自动恢复，请人工核对云端", "ERROR")
                continue
            result = classify_journal_sheet(cli, record)
            if result["state"] == "verified":
                ok, reason = _merge_ledger(ledger, record)
                if ok:
                    journal.set_sheet_status(
                        operation_id, sheet_key, "verified", reason="",
                        next_action="none",
                        cloud_checked=True, evidence="journal+cloud_read")
                    sheet_reports.append({"sheet": record.get("sheet", sheet_key),
                                          "file_id": record.get("file_id", ""),
                                          "status": "verified",
                                          "cloud_checked": True,
                                          "evidence": "journal+cloud_read",
                                          "next_action": "none"})
                    emit(f"[云同步恢复] {record.get('sheet', sheet_key)}："
                         f"云端验证完整成功，已补账本")
                else:
                    op_recovered_all = False
                    op_uncertain = True
                    journal.set_sheet_status(
                        operation_id, sheet_key, "ledger_pending",
                        reason=reason, next_action="recover_journal",
                        problems=[reason],
                        cloud_checked=True, evidence="journal+cloud_read")
                    sheet_reports.append({"sheet": record.get("sheet", sheet_key),
                                          "file_id": record.get("file_id", ""),
                                          "status": "ledger_pending",
                                          "reason": reason,
                                          "cloud_checked": True,
                                          "evidence": "journal+cloud_read",
                                          "next_action": "recover_journal"})
                    emit(f"[云同步恢复] {record.get('sheet', sheet_key)}："
                         f"云端成功但账本无法落盘（{reason}）", "ERROR")
            elif result["state"] == "not_started":
                op_not_started = True
                op_recovered_all = False
                journal.set_sheet_status(
                    operation_id, sheet_key, "not_started",
                    reason=result["reason"], next_action="repreview",
                    problems=result.get("problems") or [],
                    cloud_checked=True, evidence="journal+cloud_read")
                sheet_reports.append({"sheet": record.get("sheet", sheet_key),
                                      "file_id": record.get("file_id", ""),
                                      "status": "not_started",
                                      "reason": result["reason"],
                                      "cloud_checked": True,
                                      "evidence": "journal+cloud_read",
                                      "next_action": "repreview"})
                emit(f"[云同步恢复] {record.get('sheet', sheet_key)}："
                     f"确认完全未执行，可重新预览")
            else:
                op_recovered_all = False
                op_uncertain = True
                journal.set_sheet_status(
                    operation_id, sheet_key, "uncertain",
                    reason=result.get("reason", "partial_or_ambiguous"),
                    next_action="manual_reconcile",
                    problems=result.get("problems") or [],
                    cloud_checked=True, evidence="journal+cloud_read")
                sheet_reports.append({"sheet": record.get("sheet", sheet_key),
                                      "file_id": record.get("file_id", ""),
                                      "status": "uncertain",
                                      "reason": result.get("reason", "partial_or_ambiguous"),
                                      "problems": result.get("problems") or [],
                                      "cloud_checked": True,
                                      "evidence": "journal+cloud_read",
                                      "next_action": "manual_reconcile"})
                emit(f"[云同步恢复] {record.get('sheet', sheet_key)}："
                     f"无法证明整体成功/完全未执行，已阻断，请人工核对云端", "ERROR")
        try:
            journal.save()
        except Exception as exc:  # noqa: BLE001 - 日志仍不可写则本次恢复结果不可信
            op_uncertain = True
            op_recovered_all = False
            emit(f"[云同步恢复] 意图日志保存失败：{exc}", "ERROR")
        if op_uncertain:
            uncertain.append(operation_id)
        elif op_not_started:
            not_started.append(operation_id)
            needs_repreview = True
        elif op_recovered_all:
            recovered.append(operation_id)
        operation_out = {
            "operation_id": operation_id,
            "status": "uncertain" if op_uncertain else (
                "not_started" if op_not_started else "verified"),
            "next_action": "manual_reconcile" if op_uncertain else (
                "repreview" if op_not_started else "none"),
            "sheets": sheet_reports,
        }
        operations.append(operation_out)

    if uncertain:
        status, uncertain_flag, next_action = "uncertain", True, "manual_reconcile"
    elif not_started:
        status, uncertain_flag, next_action = "not_started", False, "repreview"
    elif recovered:
        status, uncertain_flag, next_action = "recovered", False, "repreview"
    else:
        status, uncertain_flag, next_action = "none", False, "none"
    return {
        "status": status,
        "uncertain": uncertain_flag,
        "next_action": next_action,
        "operations": operations,
        "recovered_operation_ids": recovered,
        "not_started_operation_ids": not_started,
        "uncertain_operation_ids": uncertain,
        "needs_repreview": bool(recovered or needs_repreview),
    }


def recover_pending_operations(cli: KdocsCli, journal: Any,
                               ledger: SyncLedger | None = None,
                               log: Any = None) -> dict[str, Any]:
    """跨进程串行恢复入口：有落盘目标时先取 operation 锁，再执行只读对账。"""
    lock_target = _recovery_lock_target(ledger, journal)
    if lock_target is None:
        return _recover_pending_operations_impl(cli, journal, ledger=ledger, log=log)
    lock = FileLock(lock_target, timeout=30.0)
    try:
        lock.acquire()
    except LockTimeout as exc:
        return {"status": "uncertain", "uncertain": True,
                "next_action": "wait_for_recovery_lock",
                "reason": f"另一个进程正在恢复/上传，等待锁超时：{exc}",
                "operations": [], "recovered_operation_ids": [],
                "not_started_operation_ids": [], "uncertain_operation_ids": [],
                "needs_repreview": False}
    except (AtomicWriteError, OSError, RuntimeError) as exc:
        return {"status": "uncertain", "uncertain": True,
                "next_action": "fix_journal",
                "reason": f"本地存储不可用，无法取得恢复锁：{exc}",
                "operations": [], "recovered_operation_ids": [],
                "not_started_operation_ids": [], "uncertain_operation_ids": [],
                "needs_repreview": False}
    try:
        try:
            journal = _refresh_journal_snapshot(journal, ledger)
        except Exception as exc:  # noqa: BLE001 - 最新日志不可读则失败关闭
            return {"status": "uncertain", "uncertain": True,
                    "next_action": "fix_journal",
                    "reason": f"恢复前刷新 journal 失败：{exc}",
                    "operations": [], "recovered_operation_ids": [],
                    "not_started_operation_ids": [], "uncertain_operation_ids": [],
                    "needs_repreview": False}
        return _recover_pending_operations_impl(cli, journal, ledger=ledger, log=log)
    finally:
        lock.release()


def _allowed_next_actions(display_status: str, next_action: str) -> list[str]:
    if display_status == "uncertain":
        return ["manual_reconcile"]
    if display_status in ("planned", "writing", "ledger_pending"):
        return ["recover_journal"]
    if display_status in ("failed", "not_started"):
        return ["repreview"]
    if display_status == "verified":
        return []
    return [next_action] if next_action else []


_RECOVERY_STATUS_KEYS = (
    "planned", "writing", "ledger_pending", "uncertain",
    "verified", "failed", "not_started", "retired_guarded",
)


def _empty_recovery_status(reason: str, next_action: str) -> dict[str, Any]:
    return {
        "ok": False,
        "reason": reason,
        "contract_version": 1,
        "source": "local_journal",
        "read_only": True,
        "queried_cloud": False,
        "contains_cloud_checked_records": False,
        "operations": [],
        "pending_operations": [],
        "counts": {key: 0 for key in _RECOVERY_STATUS_KEYS},
        "next_action": next_action,
    }


_SAFE_STATUS_KEYS = (
    "planned", "writing", "ledger_pending", "uncertain",
    "verified", "failed", "not_started", "retired_guarded",
)
_SAFE_STATUS_ORDER = {
    "uncertain": 0, "retired_guarded": 1, "writing": 2, "ledger_pending": 3,
    "planned": 4, "failed": 5, "not_started": 6, "verified": 7,
}
_SAFE_ALLOWED_NEXT = {
    "uncertain": ["manual_reconcile"],
    "writing": ["recover_journal"],
    "ledger_pending": ["recover_journal"],
    "planned": ["recover_journal"],
    "failed": ["repreview"],
    "not_started": ["repreview"],
    "verified": [],
    "retired_guarded": ["manual_reconcile"],
}
_SAFE_STATUS_CODE = {
    "uncertain": "wps_recovery_uncertain",
    "writing": "wps_recovery_writing",
    "ledger_pending": "wps_recovery_ledger_pending",
    "planned": "wps_recovery_planned",
    "failed": "wps_recovery_failed",
    "not_started": "wps_recovery_not_started",
    "verified": "wps_recovery_verified",
    "retired_guarded": "wps_recovery_retired_guarded",
}
_SAFE_EVIDENCE = {
    "local_journal", "journal+cloud_read", "resolve+cloud_read",
    "executor_cloud_readback",
}
_SAFE_OP_ID_RE = re.compile(r"^wps-[0-9a-f]{16}$")
_SAFE_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")
_SAFE_TARGET_REF_RE = re.compile(r"^wps-target:[0-9a-f]{12}$")


def _safe_status(value: Any) -> str:
    raw = "failed" if str(value or "") == "failed_no_write" else str(value or "")
    return raw if raw in _SAFE_STATUS_KEYS else "uncertain"


def _safe_iso(value: Any) -> str:
    text = str(value or "")
    return text if _SAFE_ISO_RE.match(text) else ""


def _safe_operation_id(value: Any) -> str:
    text = str(value or "")
    return text if _SAFE_OP_ID_RE.match(text) else ""


def _safe_operation_ref(value: Any) -> str:
    text = str(value or "")
    return "wps-op:" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def _safe_target_ref(value: Any) -> str:
    text = str(value or "")
    return text if _SAFE_TARGET_REF_RE.match(text) else (
        "wps-target:" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:12])


def _safe_target_date(value: Any) -> str:
    text = str(value or "")
    try:
        return _dt.date.fromisoformat(text).isoformat()
    except ValueError:
        return ""


def _safe_evidence(value: Any) -> str:
    text = str(value or "local_journal")
    return text if text in _SAFE_EVIDENCE else "local_journal"


def _worst_status(statuses: set[str]) -> str:
    if not statuses:
        return "uncertain"
    return sorted(statuses, key=lambda item: _SAFE_STATUS_ORDER.get(item, 99))[0]


def _safe_sheet(sheet: Mapping[str, Any]) -> dict[str, Any]:
    status = _safe_status(sheet.get("status") or sheet.get("raw_status"))
    raw_value = str(sheet.get("raw_status") or status or "uncertain")
    raw_status = raw_value if raw_value in {
        "planned", "writing", "ledger_pending", "uncertain",
        "verified", "failed_no_write", "not_started", "retired_guarded",
    } else "uncertain"
    return {
        "target_date": _safe_target_date(sheet.get("target_date")),
        "target_ref": _safe_target_ref(sheet.get("target_ref")),
        "status": status,
        "raw_status": raw_status,
        "error_code": _SAFE_STATUS_CODE.get(status, "wps_recovery_unknown"),
        "allowed_next_actions": list(_SAFE_ALLOWED_NEXT.get(status, ["manual_reconcile"])),
        "manual_required": bool(sheet.get("manual_required", False)),
        "cloud_checked": bool(sheet.get("cloud_checked", False)),
        "evidence": _safe_evidence(sheet.get("evidence")),
    }


def _safe_operation(operation: Mapping[str, Any]) -> dict[str, Any]:
    sheets = operation.get("sheets") if isinstance(operation.get("sheets"), list) else []
    safe_sheets = [_safe_sheet(sheet) for sheet in sheets if isinstance(sheet, Mapping)]
    statuses = {sheet["status"] for sheet in safe_sheets}
    worst = _worst_status(statuses)
    operation_id = _safe_operation_id(operation.get("operation_id"))
    manual_required = any(sheet["manual_required"] for sheet in safe_sheets)
    target_refs = sorted({sheet["target_ref"] for sheet in safe_sheets if sheet["target_ref"]})
    target_date = _safe_target_date(operation.get("target_date"))
    if not target_date:
        for sheet in safe_sheets:
            if sheet["target_date"]:
                target_date = sheet["target_date"]
                break
    return {
        "operation_id": operation_id,
        "operation_ref": _safe_operation_ref(operation.get("operation_id")),
        "status": worst,
        "pending": bool(operation.get("pending", False)),
        "cloud_checked": bool(operation.get("cloud_checked", False)),
        "created_at": _safe_iso(operation.get("created_at")),
        "updated_at": _safe_iso(operation.get("updated_at")),
        "target_date": target_date,
        "target_refs": target_refs,
        "sheet_count": len(safe_sheets),
        "error_code": _SAFE_STATUS_CODE.get(worst, "wps_recovery_unknown"),
        "allowed_next_actions": list(_SAFE_ALLOWED_NEXT.get(worst, ["manual_reconcile"])),
        "manual_required": manual_required,
        "sheets": safe_sheets,
    }


def _safe_recovery_error(error_code: str, next_action: str) -> dict[str, Any]:
    return {
        "ok": False,
        "contract_version": 1,
        "source": "local_journal",
        "read_only": True,
        "queried_cloud": False,
        "contains_cloud_checked_records": False,
        "operations": [],
        "pending_operations": [],
        "counts": {key: 0 for key in _SAFE_STATUS_KEYS},
        "next_action": next_action,
        "error_code": error_code,
    }


def _safe_error_code_from_reason(reason: Any) -> str:
    text = str(reason or "")
    if text == "ledger_missing":
        return "wps_recovery_ledger_missing"
    if text.startswith("journal_unreadable"):
        return "wps_recovery_journal_unreadable"
    if text.startswith("ledger_unreadable"):
        return "wps_recovery_ledger_unreadable"
    return "wps_recovery_error"


def _safe_from_full(full: Mapping[str, Any]) -> dict[str, Any]:
    raw_operations = full.get("operations")
    operations = []
    if isinstance(raw_operations, list):
        operations = [_safe_operation(op) for op in raw_operations
                      if isinstance(op, Mapping)]
    pending = [op for op in operations if op.get("pending")]
    counts = {key: 0 for key in _SAFE_STATUS_KEYS}
    for operation in operations:
        for sheet in operation.get("sheets") or []:
            status = sheet.get("status", "uncertain")
            counts[status] = counts.get(status, 0) + 1
    if counts.get("uncertain"):
        next_action = "manual_reconcile"
    elif counts.get("retired_guarded"):
        next_action = "manual_reconcile"
    elif any(op.get("pending") for op in operations):
        next_action = "recover_journal"
    elif any(op.get("status") in ("failed", "not_started") for op in operations):
        next_action = "repreview"
    else:
        next_action = "none"
    return {
        "ok": True,
        "contract_version": 1,
        "source": "local_journal",
        "read_only": True,
        "queried_cloud": False,
        "contains_cloud_checked_records": bool(
            full.get("contains_cloud_checked_records", False)),
        "operations": operations,
        "pending_operations": pending,
        "counts": counts,
        "next_action": next_action,
        "error_code": "",
    }


def recovery_status(ledger: SyncLedger | None = None, *,
                    journal: SyncJournal | None = None) -> dict[str, Any]:
    """对外安全只读恢复查询；字段白名单构造，不复制 journal 原文。

    与 :func:`_recovery_status_full` 不同，本函数绝不返回 problems、
    risk_reason、异常字符串、客户行、sheet/file_id、路径或原始快照。
    A/D 仅可使用本函数对外；内部审计保留完整 journal。
    """
    try:
        full = _recovery_status_full(ledger, journal=journal)
        if not isinstance(full, Mapping) or not full.get("ok"):
            code = _safe_error_code_from_reason(
                full.get("reason") if isinstance(full, Mapping) else "")
            next_action = "fix_journal" if code != "wps_recovery_ledger_missing" else "none"
            return _safe_recovery_error(code, next_action)
        return _safe_from_full(full)
    except Exception:  # noqa: BLE001 - 对外错误响应也必须脱敏
        return _safe_recovery_error("wps_recovery_internal_error", "fix_journal")


def _recovery_status_full(ledger: SyncLedger | None = None, *,
                         journal: SyncJournal | None = None) -> dict[str, Any]:
    """内部只读完整视图：保留 problems/risk_reason/file_id/sheet 等审计数据。

    该函数只供 app/wps 内部与审计使用，**不得**直接接 HTTP/普通用户；
    对外请用 :func:`recovery_status` 的字段白名单安全 DTO。
    本函数自身不读/写云端、不写账本、不消费预览、不解除阻断。
    """
    if journal is None:
        if ledger is None:
            return _empty_recovery_status("ledger_missing", "none")
        try:
            journal = _journal_for_ledger(ledger)
        except Exception as exc:  # noqa: BLE001 - 只读入口也要把损坏日志变成 JSON
            return _empty_recovery_status(
                f"journal_unreadable:{type(exc).__name__}", "fix_journal")
    operations: list[dict[str, Any]] = []
    pending_ids = set(journal.pending_operations())
    known = set(_RECOVERY_STATUS_KEYS)
    counts: dict[str, int] = {key: 0 for key in known}
    contains_cloud_checked = False
    for operation_id, op in journal.operations().items():
        if not isinstance(op, dict):
            continue
        op_raw_status = str(op.get("status") or "")
        op_unsupported = op_raw_status not in OP_STATUSES
        op_retired = op_raw_status == "retired_guarded"
        op_unsupported_preserved = bool(
            op.get("unsupported_status_preserved", False)) and not op_retired
        sheets: list[dict[str, Any]] = []
        operation_cloud_checked = False
        for sheet_key, record in (op.get("sheets") or {}).items():
            if not isinstance(record, dict):
                continue
            raw = str(record.get("status") or "uncertain")
            display = "failed" if raw == "failed_no_write" else raw
            if op_retired and record.get("retired_guarded"):
                display = "retired_guarded"
            elif (display not in known or op_unsupported
                    or op_unsupported_preserved):
                display = "uncertain"
            counts[display] = counts.get(display, 0) + 1
            cloud_checked = bool(record.get("cloud_checked", False))
            evidence = str(record.get("evidence") or "local_journal")
            if cloud_checked:
                contains_cloud_checked = True
                operation_cloud_checked = True
            next_action = str(record.get("next_action") or "")
            allowed = _allowed_next_actions(display, next_action)
            risk_reason = str(record.get("reason") or "").strip()
            problems = [str(item) for item in (record.get("problems") or [])][:20]
            if not risk_reason and not problems:
                risk_reason = {
                    "uncertain": "结果不确定，需人工核对云端实际值",
                    "failed": "已确认本轮未成功或未执行",
                    "not_started": "确认未执行，可重新预览",
                    "planned": "尚未开始执行",
                    "writing": "写入进行中，需要恢复对账",
                    "ledger_pending": "云端已核对，等待账本锚点落盘",
                    "verified": "已完整成功",
                }.get(display, "无可用的风险说明")
            target_date = str(record.get("target_date") or "")
            sheets.append({
                "sheet": record.get("sheet", sheet_key),
                "target_date": target_date,
                "target_ref": _target_ref(record.get("file_id", ""),
                                         record.get("sheet", sheet_key),
                                         target_date),
                "status": display,
                "raw_status": raw,
                "risk_reason": "；".join([item for item in [risk_reason, *problems] if item])[:500],
                "next_action": next_action,
                "allowed_next_actions": allowed,
                "manual_required": str(record.get("manual_required") or ""),
                "problems": problems,
                "cloud_checked": cloud_checked,
                "evidence": evidence,
            })
        target_date = str(op.get("target_date") or "")
        if not target_date and sheets:
            target_date = str(sheets[0].get("target_date") or "")
        op_display_status = "uncertain" if op_unsupported else op_raw_status
        operations.append({
            "operation_id": operation_id,
            "status": op_display_status,
            "pending": operation_id in pending_ids,
            "cloud_checked": operation_cloud_checked,
            "created_at": op.get("created_at", ""),
            "updated_at": op.get("updated_at", ""),
            "target_date": target_date,
            "sheets": sheets,
        })
    pending_operations = [op for op in operations if op["pending"]]
    if counts.get("uncertain"):
        next_action = "manual_reconcile"
    elif counts.get("retired_guarded"):
        next_action = "manual_reconcile"
    elif pending_operations:
        next_action = "recover_journal"
    else:
        next_action = "none"
    return {
        "ok": True,
        "contract_version": 1,
        "source": "local_journal",
        "read_only": True,
        "queried_cloud": False,
        "contains_cloud_checked_records": contains_cloud_checked,
        "operations": operations,
        "pending_operations": pending_operations,
        "counts": counts,
        "next_action": next_action,
    }


def resolve_pending_operation(
        ledger: SyncLedger,
        operation_id: str,
        decision: str,
        *,
        journal: Any = None,
        cli: Any = None,
        note: str = "",
        confirm_structure_checked: bool = False) -> dict[str, Any]:
    """人工核对后的恢复入口（跨进程串行）。"""
    lock_target = _recovery_lock_target(ledger, journal)
    if lock_target is None:
        return _resolve_pending_operation_impl(
            ledger, operation_id, decision, journal=journal, cli=cli, note=note,
            confirm_structure_checked=confirm_structure_checked)
    lock = FileLock(lock_target, timeout=30.0)
    try:
        lock.acquire()
    except (LockTimeout, AtomicWriteError, OSError, RuntimeError) as exc:
        return {"ok": False, "status": "blocked",
                "reason": f"恢复锁不可用：{exc}",
                "next_action": ("wait_for_recovery_lock"
                                if isinstance(exc, LockTimeout) else "fix_journal"),
                "operations": []}
    try:
        try:
            journal = _refresh_journal_snapshot(journal, ledger)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "status": "blocked",
                    "reason": f"恢复前刷新 journal 失败：{exc}",
                    "next_action": "fix_journal", "operations": []}
        return _resolve_pending_operation_impl(
            ledger, operation_id, decision, journal=journal, cli=cli, note=note,
            confirm_structure_checked=confirm_structure_checked)
    finally:
        lock.release()


def _resolve_pending_operation_impl(
        ledger: SyncLedger,
        operation_id: str,
        decision: str,
        *,
        journal: Any = None,
        cli: Any = None,
        note: str = "",
        confirm_structure_checked: bool = False) -> dict[str, Any]:
    """人工核对后的恢复入口；A 可选调用，默认 ``apply_plan`` 会自动恢复。

    * ``decision="cloud_verified"``：人工确认云端完整成功，但仍会重新只读
      分类；只有每张表都 ``verified`` 才补账本并标记完成。
    * ``decision="cloud_untouched"``：人工确认完全未执行；需分类为
      ``not_started``。若上次是插入/回滚未确认（``manual_required``），
      必须显式 ``confirm_structure_checked=True`` 才允许清障并重预览。
    * ``decision="keep"``：仅写核对备注，保持 uncertain，阻断后续自动写。
    * ``decision="retire_guarded"``（别名 ``retire_manual`` / ``abandon_guarded``）：
      不把不可判定的云端状态当成 cloud_untouched；为旧任务写入审计的
      ``retired_guarded`` 闸门并退出全局 pending，但同一目标日期/云表仍被
      防重复闸门阻断，直到通过实际云端读取证明 verified/not_started 后才清除。
    """
    if journal is None:
        try:
            journal = _journal_for_ledger(ledger)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "status": "blocked",
                    "reason": f"journal_unreadable:{type(exc).__name__}:{exc}"}
    op = journal.get_operation(operation_id)
    if not isinstance(op, dict):
        return {"ok": False, "status": "not_found", "reason": "operation_not_found"}
    if not isinstance(op.get("sheets"), dict):
        return {"ok": False, "status": "invalid", "reason": "operation_invalid"}
    results: list[dict[str, Any]] = []
    if decision in ("retire", "retire_manual", "retire_old_operation",
                    "guarded_retire", "abandon", "abandon_guarded",
                    "manual_retire", "audited_retire"):
        decision = "retire_guarded"
    if decision not in ("cloud_verified", "cloud_untouched", "keep",
                        "retire_guarded"):
        return {"ok": False, "status": "rejected", "reason": "decision_invalid",
                "operations": results}
    if decision == "retire_guarded" and not confirm_structure_checked:
        return {"ok": False, "status": "rejected",
                "reason": "manual_confirmation_required",
                "next_action": "manual_reconcile", "operations": results}
    op_unsupported_preserved = bool(op.get("unsupported_status_preserved", False))
    if op_unsupported_preserved and decision in ("cloud_verified", "cloud_untouched"):
        return {"ok": False, "status": "blocked",
                "reason": "unsupported_status_requires_manual",
                "next_action": "manual_reconcile", "operations": results}
    if op_unsupported_preserved and decision == "keep":
        op["status"] = "uncertain"
        op["next_action"] = "manual_reconcile"
        op["updated_at"] = _dt.datetime.now().isoformat(timespec="microseconds")
        try:
            journal.save()
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "status": "blocked",
                    "reason": f"journal_save_failed:{type(exc).__name__}:{exc}",
                    "next_action": "fix_journal", "operations": []}
        return {"ok": False, "status": "uncertain",
                "reason": "unsupported_status_kept",
                "next_action": "manual_reconcile", "operations": []}
    if op_unsupported_preserved and decision == "retire_guarded":
        now = _dt.datetime.now().isoformat(timespec="microseconds")
        for sheet_key, record in op["sheets"].items():
            if not isinstance(record, dict):
                continue
            record["retired_guarded"] = True
            record["retired_at"] = now
            record["retire_note"] = note
            record["prior_status"] = str(record.get("status") or "")
            record["unsupported_original_status"] = str(
                record.get("unsupported_original_status") or record.get("status") or "")
            results.append({"sheet": record.get("sheet", sheet_key),
                            "status": "retired_guarded",
                            "reason": "unsupported_status_guarded"})
        op["status"] = "retired_guarded"
        op["next_action"] = "manual_reconcile"
        op["retired_guarded"] = True
        op["retired_at"] = now
        op["retire_note"] = note
        op["retire_decision"] = "retire_guarded"
        op["updated_at"] = now
        try:
            journal.save()
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "status": "blocked",
                    "reason": f"journal_save_failed:{type(exc).__name__}:{exc}",
                    "next_action": "fix_journal", "operations": results}
        return {"ok": True, "status": "retired_guarded",
                "next_action": "manual_reconcile",
                "reason": "retired_with_guard", "operations": results}
    for sheet_key, record in op["sheets"].items():
        if not isinstance(record, dict):
            continue
        status = str(record.get("status") or "")
        if decision == "retire_guarded":
            if status in ("verified", "failed_no_write", "not_started"):
                continue
            if status not in SHEET_STATUSES:
                # 未知状态也要保留原值，并加防重复闸门；不能放行、不能删除。
                journal.set_sheet_status(
                    operation_id, sheet_key, "retired_guarded",
                    reason=note or record.get("reason", ""),
                    next_action="manual_reconcile",
                    problems=record.get("problems") or [],
                    retired_guarded=True,
                    retired_at=_dt.datetime.now().isoformat(timespec="microseconds"),
                    retire_note=note,
                    prior_status=status or "unknown",
                    unsupported_original_status=status or "unknown",
                    cloud_checked=bool(record.get("cloud_checked", False)),
                    evidence=str(record.get("evidence") or "local_journal"))
                results.append({"sheet": record.get("sheet", sheet_key),
                                "status": "retired_guarded",
                                "reason": "unsupported_status_guarded"})
                continue
            journal.set_sheet_status(
                operation_id, sheet_key, "retired_guarded",
                reason=note or record.get("reason", ""),
                next_action="manual_reconcile",
                problems=record.get("problems") or [],
                retired_guarded=True,
                retired_at=_dt.datetime.now().isoformat(timespec="microseconds"),
                retire_note=note, prior_status=status or "",
                cloud_checked=bool(record.get("cloud_checked", False)),
                evidence=str(record.get("evidence") or "local_journal"))
            results.append({"sheet": record.get("sheet", sheet_key),
                            "status": "retired_guarded", "reason": "manual_retire"})
            continue
        if status not in SHEET_STATUSES:
            results.append({"sheet": record.get("sheet", sheet_key),
                            "status": "uncertain",
                            "reason": "unsupported_status_requires_manual",
                            "problems": record.get("problems") or []})
            continue
        if status in ("verified", "failed_no_write", "not_started"):
            continue
        if decision == "keep":
            if (record.get("retired_guarded")
                    or status == "retired_guarded"):
                # 已退出的旧任务只更新审计备注，保持 retired_guarded 与原防重闸门，
                # 不能退回 uncertain 而重新进入全局 pending。
                journal.set_sheet_status(
                    operation_id, sheet_key, "retired_guarded",
                    reason=note or record.get("reason", ""),
                    next_action="manual_reconcile",
                    problems=record.get("problems") or [],
                    retired_guarded=True,
                    retired_at=record.get("retired_at")
                    or _dt.datetime.now().isoformat(timespec="microseconds"),
                    retire_note=note or record.get("retire_note", ""),
                    cloud_checked=bool(record.get("cloud_checked", False)),
                    evidence=str(record.get("evidence") or "local_journal"))
                results.append({"sheet": record.get("sheet", sheet_key),
                                "status": "retired_guarded", "reason": "user_keep"})
                continue
            journal.set_sheet_status(operation_id, sheet_key, "uncertain",
                                     reason=note or record.get("reason", ""),
                                     next_action="manual_reconcile",
                                     problems=record.get("problems") or [],
                                     cloud_checked=bool(record.get("cloud_checked", False)),
                                     evidence=str(record.get("evidence") or "local_journal"))
            results.append({"sheet": record.get("sheet", sheet_key),
                            "status": "uncertain", "reason": "user_keep"})
            continue
        classified = classify_journal_sheet(cli, record) if cli is not None else None
        if decision == "cloud_verified":
            if classified is None or classified.get("state") != "verified":
                results.append({"sheet": record.get("sheet", sheet_key),
                                "status": "uncertain",
                                "reason": "cloud_verify_failed" if classified else "cli_required",
                                "problems": (classified or {}).get("problems", [])})
                continue
            ok, reason = _merge_ledger(ledger, record)
            if not ok:
                journal.set_sheet_status(operation_id, sheet_key, "ledger_pending",
                                         reason=reason, next_action="recover_journal",
                                         problems=[reason],
                                         cloud_checked=True,
                                         evidence="resolve+cloud_read")
                results.append({"sheet": record.get("sheet", sheet_key),
                                "status": "ledger_pending", "reason": reason})
                continue
            journal.set_sheet_status(operation_id, sheet_key, "verified",
                                     reason=note, next_action="none",
                                     problems=[],
                                     retired_guarded=False,
                                     retired_at="", retire_note="", prior_status="",
                                     cloud_checked=True,
                                     evidence="resolve+cloud_read")
            results.append({"sheet": record.get("sheet", sheet_key),
                            "status": "verified", "reason": ""})
        else:  # cloud_untouched
            manual = str(record.get("manual_required") or "")
            can_confirm = bool(
                classified is not None and classified.get("state") == "not_started"
                and (not manual or confirm_structure_checked))
            if not can_confirm:
                results.append({"sheet": record.get("sheet", sheet_key),
                                "status": "uncertain",
                                "reason": "cloud_not_untouched" if classified
                                else "cli_required",
                                "problems": (classified or {}).get("problems", [])})
                continue
            journal.set_sheet_status(operation_id, sheet_key, "not_started",
                                     reason=note or "人工核对确认完全未执行",
                                     next_action="repreview", problems=[],
                                     retired_guarded=False,
                                     retired_at="", retire_note="", prior_status="",
                                     cloud_checked=True,
                                     evidence="resolve+cloud_read")
            results.append({"sheet": record.get("sheet", sheet_key),
                            "status": "not_started", "reason": "manual_confirm"})
    if decision == "retire_guarded":
        op["retired_guarded"] = True
        op["retired_at"] = _dt.datetime.now().isoformat(timespec="microseconds")
        op["retire_note"] = note
        op["retire_decision"] = "retire_guarded"
    try:
        journal.save()
    except Exception as exc:  # noqa: BLE001 - 无法落盘则不能声称已解阻
        return {"ok": False, "status": "blocked",
                "reason": f"journal_save_failed:{type(exc).__name__}:{exc}",
                "next_action": "fix_journal", "operations": results}
    if decision == "retire_guarded":
        return {"ok": True, "status": "retired_guarded",
                "next_action": "manual_reconcile",
                "reason": "retired_with_guard",
                "operations": results}
    resolved = not any(item.get("status") in ("uncertain", "ledger_pending")
                       for item in results)
    return {"ok": resolved, "status": "resolved" if resolved else "uncertain",
            "next_action": "repreview" if resolved else "manual_reconcile",
            "operations": results}


def retire_pending_operation(
        ledger: SyncLedger,
        operation_id: str,
        *,
        journal: Any = None,
        note: str = "",
        cli: Any = None) -> dict[str, Any]:
    """有审计地退出旧 pending 任务，同时保留同目标防重复闸门。

    等价于 ``resolve_pending_operation(..., decision="retire_guarded",
    confirm_structure_checked=True)``。不会把不可判定状态当作 cloud_untouched，
    不会删除 journal；同一 ``target_date + file_id`` 的后续写入仍被
    ``SyncJournal.has_guard`` 阻断，其他日期不受影响。
    """
    return resolve_pending_operation(
        ledger, operation_id, "retire_guarded", journal=journal, cli=cli,
        note=note, confirm_structure_checked=True)


#: 兼容命名：某些调用方可能把它叫作 guarded retire。
retire_guarded_operation = retire_pending_operation


def _journal_for_ledger(ledger: SyncLedger) -> Any:
    from app.wps.journal import SyncJournal, journal_path_for
    return SyncJournal(journal_path_for(ledger.path))


__all__ = [
    "classify_journal_sheet",
    "recover_pending_operations",
    "recovery_status",
    "resolve_pending_operation",
    "retire_guarded_operation",
    "retire_pending_operation",
]
