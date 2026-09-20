"""执行 WPS 写入计划：插入/排序/格式化/回读校验，以及失败回滚。"""

from __future__ import annotations

import copy
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Callable

from app.wps.cli import KdocsCli
from app.wps.common import (
    CELL_MARK,
    FILL_GOLD,
    FILL_LUXURY,
    FIRST_DATA_ROW,
    FONT_SIZE_TO_TWIP,
    HEADER_ROW,
    MAX_SCAN_ROW,
    _ALIGN_H,
    _ALIGN_V,
    _argb_to_int,
    _as_int,
    _address_key,
    column_name,
    parse_date_header,
    format_sort_key,
    person_key,
    probe_sort_area,
)
from app.wps.atomicio import AtomicWriteError, FileLock, LockTimeout, operation_lock_path_for
from app.wps.errors import WpsCloudError
from app.wps.journal import (
    DEFAULT_COMPACT_KEEP_OPERATIONS,
    SyncJournal,
    journal_path_for,
    new_operation_id,
)
from app.wps.ledger import SyncLedger
from app.wps.models import LEDGER_SNAPSHOT_ABSENT, Change, SheetPlan
from app.wps.planner import formula_cells_for_new_rows
from app.wps.recovery import classify_journal_sheet, recover_pending_operations

#: apply_plan 整轮跨进程 operation 锁超时；测试可 monkeypatch 到很小。
APPLY_PLAN_LOCK_TIMEOUT = 30.0


def _rollback_inserts(cli: KdocsCli, plan: SheetPlan, worksheet_id: int,
                      inserted: Sequence[tuple[int, int]],
                      emit: Callable[[str], Any]) -> bool:
    """插入成功但后续写入失败时，把插出来的行删掉，避免云端留下烂尾空行。

    从位置最靠后的块开始删（前面的删除不会影响后面的行号）。
    """
    if not inserted:
        return True
    for first_row, count in sorted(inserted, reverse=True):
        try:
            cli.delete_rows(plan.file_id, worksheet_id, row=first_row, count=count)
        except WpsCloudError as exc:
            emit(f"[云同步] {plan.sheet}：回滚插入失败（第 {first_row} 行起 "
                 f"{count} 行可能残留空行，请人工检查）：{exc}")
            return False
    emit(f"[云同步] {plan.sheet}：已回滚 {len(inserted)} 处插入，云端恢复原状")
    return True

def read_person_rows(cli: KdocsCli, plan: SheetPlan, worksheet_id: int, *,
                     last_row: int) -> dict[tuple[str, str], list[int]]:
    """回读「姓名+电话」列，返回 ``{(姓名, 电话): [行号, ...]}``（1-based，按表内顺序）。

    整表排序后人行号全变了，必须靠这个重新定位到每个人的新行号。
    **一个人可能有多行**（一天两单）：第 i 个元素就是他的第 i 个槽位 ——
    排序是稳定的，所以同一个人的多行相对顺序与排序前一致。
    只读 3 列，一次调用。
    """
    name_col = plan.columns.get("name") or 1
    phone_col = plan.columns.get("phone") or max(name_col + 1, 3)
    lo, hi = min(name_col, phone_col), max(name_col, phone_col)
    row_from = FIRST_DATA_ROW if last_row >= FIRST_DATA_ROW else FIRST_DATA_ROW
    grid = cli.read_grid(plan.file_id, worksheet_id,
                         row_from - 1, max(last_row, row_from) - 1, lo - 1, hi - 1)
    index: dict[tuple[str, str], list[int]] = {}
    for (row, col), text in grid.items():
        if col != name_col - 1 or not str(text).strip():
            continue
        phone = grid.get((row, phone_col - 1), "")
        key = person_key(text, phone)
        if row + 1 not in index.setdefault(key, []):
            index[key].append(row + 1)
    for rows in index.values():
        rows.sort()
    return index

def _target_before(change: Change) -> str:
    """这一槽位在写入前的日期格原值（协作者占位 0 也原样保留）。"""
    if change.target_occupied:
        return str(change.target_occupied)
    return CELL_MARK if change.target_ok else ""


def _target_expected(change: Change) -> str:
    if change.target_occupied:
        return str(change.target_occupied)
    return CELL_MARK


def _change_key(change: Change) -> str:
    return f"{change.name}\u0000{change.phone}"


def _ledger_entries(changes: Sequence[Change], *, only_keys: set[str] | None = None,
                    ledger: SyncLedger | None = None,
                    date_key: str = "", file_id: str = "") -> dict[str, dict[str, Any]]:
    """账本条目：每个受影响的人输出**与本地槽位等长**的 slots。

    WPS-C7：绝不能只把 pending 的槽位 append 进去，否则“第 1 槽已同步、第 2 槽新增”
    时会把旧槽位截断，下一次上传把另一个槽位再全额加一遍。未被本次 pending 的槽位
    沿用旧锚点；本地槽位是权威顺序。
    """
    grouped: dict[str, list[Change]] = {}
    for change in changes:
        key = _change_key(change)
        if only_keys is not None and key not in only_keys:
            continue
        grouped.setdefault(key, []).append(change)
    entries: dict[str, dict[str, Any]] = {}
    for key, items in grouped.items():
        name, _separator, phone = key.partition("\u0000")
        old_slots = (ledger.synced_slots(date_key, file_id, name, phone)
                     if ledger is not None else None)
        by_slot = {int(change.slot): change for change in items}
        max_slot = max([0, *by_slot, *range(len(old_slots or []))])
        if max_slot <= 0:
            continue
        slots: list[int] = []
        for position in range(1, max_slot + 1):
            change = by_slot.get(position)
            if change is not None:
                slots.append(int(change.local_meals))
            elif old_slots is not None and position <= len(old_slots):
                slots.append(int(old_slots[position - 1]))
            else:
                slots.append(0)
        total = sum(int(change.total_after) for change in by_slot.values())
        entries[key] = {"slots": slots, "local": sum(slots), "total": total}
    return entries


def _sort_token(index: int) -> str:
    return f"t{index:04d}"


def _intent_for_change(plan: SheetPlan, change: Change, index: int) -> dict[str, Any]:
    columns = plan.columns or {}
    formula_needed = bool(
        (change.kind == "new" or change.fill_formula)
        and columns.get("served") and columns.get("left")
        and columns.get("total") and plan.date_cols)
    return {
        "kind": change.kind,
        "name": change.name,
        "phone": change.phone,
        "phone_key": person_key(change.name, change.phone)[1],
        "slot": int(change.slot),
        "total_before": int(change.total_before),
        "total_after": int(change.total_after),
        "target_before": _target_before(change),
        "target_expected": _target_expected(change),
        "target_occupied": str(change.target_occupied or ""),
        "address": str(change.address or ""),
        "meal_type": str(change.meal_type or ""),
        "meal_kind": str(change.meal_kind or ""),
        "fill_type": bool(change.fill_type),
        "fill_kind": bool(change.fill_kind),
        "fill_formula": bool(change.fill_formula),
        "formula_needed": formula_needed,
        "needs_write": bool(change.needs_write),
        "local_meals": int(change.local_meals),
        "cloud_before_count": len(change.cloud_before_rows or ()),
        "row_hint": int(change.row or 0),
        "pre_row": int(change.pre_row or change.insert_row or 0),
        "sort_token": _sort_token(index),
    }


def _build_journal_record(plan: SheetPlan, worksheet_id: int, marker_enabled: bool,
                          ledger: SyncLedger | None) -> dict[str, Any]:
    """把一个 SheetPlan 变成日志记录；记录里的期望值只来自 plan，不猜云端。"""
    pending_seed = [change for change in plan.changes if change.needs_write]
    affected = {_change_key(change) for change in pending_seed}
    entries = _ledger_entries(plan.changes, only_keys=affected, ledger=ledger,
                              date_key=(plan.target_date.isoformat()
                                        if plan.target_date else ""),
                              file_id=plan.file_id)
    marker_expected = ""
    if marker_enabled and plan.marker_col and pending_seed:
        marker_expected = str(plan.weekday_number)
    return {
        "sheet": plan.sheet,
        "file_id": plan.file_id,
        "target_date": plan.target_date.isoformat() if plan.target_date else "",
        "target_col": int(plan.target_col or 0),
        "target_header": str(plan.target_header or ""),
        "weekday_number": int(plan.weekday_number or 0),
        "marker_enabled": bool(marker_enabled),
        "marker_col": int(plan.marker_col or 0),
        "marker_expected": marker_expected,
        "worksheet_id": int(worksheet_id),
        "columns": {str(key): int(value or 0) for key, value in (plan.columns or {}).items()},
        "date_cols": [int(col) for col in plan.date_cols],
        "sort_enabled": bool(plan.sort_enabled and plan.sort_key_col and plan.row_keys),
        "sort_key_col": int(plan.sort_key_col or 0),
        "sort_range": str(plan.sort_range or ""),
        "last_data_row": int(plan.last_data_row or 0),
        "append_row": int(plan.append_row or 0),
        "baseline_name_rows": [[int(row), str(name), str(phone)]
                               for row, name, phone in plan.baseline_name_rows],
        "intents": [_intent_for_change(plan, change, index)
                    for index, change in enumerate(plan.changes)],
        "ledger_entries": entries,
        # 审计来源：普通 executor 意图只代表本地日志；回读/恢复成功后会更新。
        "cloud_checked": False,
        "evidence": "local_journal",
    }


def read_person_row_details(cli: KdocsCli, plan: SheetPlan, worksheet_id: int, *,
                            last_row: int) -> dict[tuple[str, str], list[tuple[int, str]]]:
    """回读姓名/电话/地址，返回 ``{key: [(行号, 地址原文), ...]}``（按行升序）。

    与 :func:`read_person_rows` 的区别是带地址：排序辅助列身份标记不可用时，
    用地址把新增槽位与旧槽位区分开（WPS-C8 的兜底路径）。
    """
    name_col = plan.columns.get("name") or 1
    phone_col = plan.columns.get("phone") or max(name_col + 1, 3)
    addr_col = plan.columns.get("address") or 0
    cols = [name_col, phone_col] + ([addr_col] if addr_col else [])
    lo, hi = min(cols), max(cols)
    row_from = FIRST_DATA_ROW
    row_to = max(last_row, row_from)
    grid = cli.read_grid(plan.file_id, worksheet_id,
                         row_from - 1, row_to - 1, lo - 1, hi - 1)
    result: dict[tuple[str, str], list[tuple[int, str]]] = {}
    for (row, col), text in grid.items():
        if col != name_col - 1 or not str(text).strip():
            continue
        phone = grid.get((row, phone_col - 1), "")
        key = person_key(text, phone)
        address = str(grid.get((row, addr_col - 1), "") or "") if addr_col else ""
        result.setdefault(key, []).append((row + 1, address))
    for rows in result.values():
        rows.sort(key=lambda item: item[0])
    return result


def _assign_change_rows(plan: SheetPlan,
                        details: Mapping[tuple[str, str], Sequence[tuple[int, str]]],
                        token_rows: Mapping[str, int] | None = None
                        ) -> dict[int, int]:
    """把 plan.changes 映射到排序后的实际行；返回 ``{change_index: row}``。

    优先用排序辅助列里的身份 token（最可靠）；缺失时对新增槽位按地址匹配，
    再给老槽位分配剩余行，避免“同人两行地址不同时按行号升序把 slot 弄反”。
    """
    assigned: dict[int, int] = {}
    groups: dict[tuple[str, str], list[tuple[int, Change]]] = {}
    for index, change in enumerate(plan.changes):
        groups.setdefault(person_key(change.name, change.phone), []).append((index, change))

    token_lookup = token_rows or {}
    for key, items in groups.items():
        candidates = [int(row) for row, _address in details.get(key, [])]
        for index, _change in items:
            token = _sort_token(index)
            row = token_lookup.get(token)
            if row is not None and row in candidates:
                assigned[index] = row
                candidates.remove(row)

    for key, items in groups.items():
        candidates = [int(row) for row, _address in details.get(key, [])]
        used = {row for idx, row in assigned.items() if idx in dict(items)}
        free = [row for row in candidates if row not in used]
        if not free:
            continue
        pending = [(index, change) for index, change in items if index not in assigned]
        new_items = sorted((pair for pair in pending if pair[1].kind == "new"),
                           key=lambda pair: (int(pair[1].slot), pair[0]))
        for index, change in new_items:
            wanted = _address_key(change.address)
            chosen = next((row for row in free
                           if wanted and _address_key(
                               next((addr for r, addr in details.get(key, []) if r == row), "")) == wanted),
                          None)
            if chosen is None:
                slot = int(change.slot)
                chosen = free[slot - 1] if 0 < slot <= len(free) else free[0]
            assigned[index] = chosen
            free.remove(chosen)
        rest = sorted((pair for pair in pending if pair[0] not in assigned),
                      key=lambda pair: (int(pair[1].slot), pair[0]))
        for index, change in rest:
            if not free:
                break
            wanted = _address_key(change.address)
            chosen = next((row for row in free
                           if wanted and _address_key(
                               next((addr for r, addr in details.get(key, []) if r == row), "")) == wanted),
                          free[0])
            assigned[index] = chosen
            free.remove(chosen)
    return assigned


def _read_sort_tokens(cli: KdocsCli, plan: SheetPlan, worksheet_id: int) -> dict[str, int]:
    """读排序辅助列里形如 ``0007:t0003`` 的身份 token -> 排序后行号。"""
    token_rows: dict[str, int] = {}
    if not plan.sort_key_col:
        return token_rows
    grid = cli.read_grid(plan.file_id, worksheet_id,
                         FIRST_DATA_ROW - 1, max(plan.last_data_row, FIRST_DATA_ROW) - 1,
                         plan.sort_key_col - 1, plan.sort_key_col - 1)
    for (row, _col), value in grid.items():
        raw = str(value or "").strip()
        if ":" not in raw:
            continue
        token = raw.rsplit(":", 1)[1].strip()
        if token:
            token_rows[token] = row + 1
    return token_rows


def _classify_cloud_now(cli: KdocsCli, journal: SyncJournal, operation_id: str,
                        record_key: str) -> dict[str, Any]:
    """用日志记录重新读云端分类；任何读取/解析异常都按 uncertain。"""
    try:
        op = journal.get_operation(operation_id) or {}
        record = (op.get("sheets") or {}).get(record_key) or {}
        return classify_journal_sheet(cli, record)
    except Exception as exc:  # noqa: BLE001 - 恢复对账失败必须不确定，不能传成成功
        return {"state": "uncertain", "reason": f"recovery_error:{type(exc).__name__}:{exc}",
                "problems": [], "next_action": "manual_reconcile"}


def _commit_ledger_entries(ledger: SyncLedger, date_key: str, file_id: str,
                           entries: Mapping[str, Any]) -> tuple[bool, str]:
    """正常写入成功后的账本提交：快照 + record + save，失败原子回滚内存。"""
    snapshot = copy.deepcopy(ledger.data)
    snapshot_digest = getattr(ledger, "_loaded_digest", None)
    try:
        ledger.record(date_key, file_id, entries)
        ledger.save()
    except BaseException as exc:
        # 磁盘写失败/进程被中断都要恢复内存快照；KeyboardInterrupt 恢复后继续抛。
        ledger.data = snapshot
        if hasattr(ledger, "_loaded_digest"):
            ledger._loaded_digest = snapshot_digest
        if not isinstance(exc, Exception):
            raise
        return False, f"ledger_save_failed:{type(exc).__name__}:{exc}"
    return True, ""


def _journal_set(journal: SyncJournal, operation_id: str, record_key: str,
                 status: str, *, emit: Callable[[str], Any], **fields: Any) -> bool:
    """更新日志状态并落盘；失败返回 False，调用方应立即零写/停止。"""
    try:
        journal.set_sheet_status(operation_id, record_key, status, **fields)
        journal.save()
        return True
    except Exception as exc:  # noqa: BLE001 - 意图不可持久化就不能继续写云端
        emit(f"[云同步] 意图日志保存失败（{exc}）：已停止写入云端，"
             f"请检查磁盘后重试；不要直接重传", "ERROR")
        return False


def _rollback_and_cleanup(cli: KdocsCli, plan: SheetPlan, worksheet_id: int,
                          inserted: Sequence[tuple[int, int]],
                          emit: Callable[[str], Any], *,
                          helpers_written: bool) -> bool:
    """排序前失败的安全回滚：先清辅助键（若已写）再删插入行，并读回确认；返回是否可证明恢复原状。"""
    if helpers_written and plan.sort_key_col:
        rows = sorted({int(row) for row in plan.row_keys})
        if rows:
            try:
                cli.write_cells(plan.file_id, worksheet_id,
                                [{"row": row, "col": plan.sort_key_col, "value": ""}
                                 for row in rows])
            except WpsCloudError as exc:
                emit(f"[云同步] {plan.sheet}：排序键清理失败：{exc}", "WARN")
                return False
    ok = _rollback_inserts(cli, plan, worksheet_id, inserted, emit)
    return bool(ok)


def _safe_failed_after_classify(cli: KdocsCli, journal: SyncJournal, operation_id: str,
                                record_key: str, emit: Callable[[str], Any],
                                fallback: str, *, rollback_ok: bool = True) -> dict[str, Any]:
    """回滚/拒绝后尝试证明“云端完全没动”：只有 not_started 才记 safe failed。

    ``rollback_ok=False`` 表示连删除插入行都没成功：即使姓名行基线碰巧一致，
    也可能残留插入空行（WPS-C9），必须 uncertain。
    """
    classified = _classify_cloud_now(cli, journal, operation_id, record_key)
    if rollback_ok and classified.get("state") == "not_started":
        return {"status": "failed", "reason": fallback, "uncertain": False,
                "next_action": "none", "problems": classified.get("problems") or [],
                "cloud_checked": True, "evidence": "executor_cloud_readback"}
    if classified.get("state") == "verified":
        return {"status": "ok", "reason": fallback, "uncertain": False,
                "next_action": "none", "warning": "写入异常但云端期望值已完整",
                "problems": classified.get("problems") or [],
                "cloud_checked": True, "evidence": "executor_cloud_readback"}
    problems = classified.get("problems") or []
    if not rollback_ok:
        problems = [*problems, "插入行回滚删除失败：云端可能残留空行，严禁自动重试"]
        return {"status": "uncertain", "reason": fallback, "uncertain": True,
                "next_action": "manual_reconcile", "problems": problems,
                "manual_required": "rollback_delete_failed",
                "cloud_checked": True, "evidence": "executor_cloud_readback"}
    return {"status": "uncertain", "reason": fallback, "uncertain": True,
            "next_action": "manual_reconcile", "problems": problems,
            "cloud_checked": True, "evidence": "executor_cloud_readback"}


def _precheck_plan_before_write(cli: KdocsCli, plan: SheetPlan,
                               worksheet_id: int) -> list[str]:
    """云端写入前的最后一次只读复核：身份/旧值/目标格是否仍是计划里看到的那样。

    只读，不发任何写请求。发现变化就返回问题列表；调用方零云端写入并让用户重新预览。
    没有远端 CAS，这一步只能缩小窗口，不能替代平台原子比较交换。
    """
    problems: list[str] = []
    if plan.target_date and plan.target_col:
        wanted = (plan.target_date.month, plan.target_date.day)
        header = cli.read_grid(plan.file_id, worksheet_id, HEADER_ROW - 1, HEADER_ROW - 1,
                               plan.target_col - 1, plan.target_col - 1)
        text = str(header.get((HEADER_ROW - 1, plan.target_col - 1), "") or "")
        if parse_date_header(text) != wanted:
            problems.append(f"目标表头已变化：第 {plan.target_col} 列现在是「{text}」")
    wanted_rows: list[int] = []
    existing = [c for c in plan.changes if c.kind == "existing" and c.needs_write
                and c.cloud_before_rows]
    if existing:
        for change in existing:
            index = int(change.slot) - 1
            if 0 <= index < len(change.cloud_before_rows):
                wanted_rows.append(int(change.cloud_before_rows[index]))
        if wanted_rows:
            lo, hi = min(wanted_rows), max(wanted_rows)
            cols = [plan.columns.get("name") or 1, plan.columns.get("phone") or 3]
            if plan.columns.get("total"):
                cols.append(plan.columns["total"])
            cols.append(plan.target_col)
            grid = cli.read_grid(plan.file_id, worksheet_id, lo - 1, hi - 1,
                                 min(cols) - 1, max(cols) - 1)
            for change in existing:
                index = int(change.slot) - 1
                if not (0 <= index < len(change.cloud_before_rows)):
                    problems.append(f"{change.name}：计划缺少排序前云端行号，无法复核旧值")
                    continue
                row = int(change.cloud_before_rows[index])
                name = str(grid.get((row - 1, (plan.columns.get("name") or 1) - 1), "")).strip()
                phone = str(grid.get((row - 1, (plan.columns.get("phone") or 3) - 1), "")).strip()
                if person_key(name, phone) != person_key(change.name, change.phone):
                    problems.append(f"第 {row} 行的客户已变化（现在为「{name}/{phone}」）")
                    continue
                if plan.columns.get("total"):
                    actual_total = _as_int(grid.get((row - 1, plan.columns["total"] - 1)))
                    if actual_total != int(change.total_before):
                        problems.append(
                            f"{change.name}：总餐次已从 {change.total_before} 变为 {actual_total}")
                actual_target = str(
                    grid.get((row - 1, plan.target_col - 1), "") or "").strip()
                if actual_target != _target_before(change):
                    problems.append(
                        f"{change.name}：目标日期格已变化（原「{_target_before(change)}」"
                        f"现「{actual_target}」）")
    new_changes = [change for change in plan.changes if change.kind == "new"]
    block = plan.insert_blocks[0] if plan.insert_blocks else None
    keys_with_cloud_rows = {person_key(c.name, c.phone) for c in plan.changes
                            if c.cloud_before_rows}
    # 只有“本地新增的客户”才要求云端不存在；同一人的第 2/3 个槽位
    # 是 kind=new 但 key 本来就有一行，不能误判为重复新增。
    new_person_keys = {person_key(c.name, c.phone) for c in new_changes} - keys_with_cloud_rows
    if new_changes:
        # 新增行安全复核必须读“实际云端现有行”，不能只看 plan.append_row-1；
        # 否则协作者在计划之后把同一客户放到更下方，仍会重复追加。
        row_hint = 0
        try:
            infos = cli.sheets_info(plan.file_id)
            if infos:
                row_hint = int(infos[0].get("rowTo") or 0) + 1
        except (WpsCloudError, TypeError, ValueError):
            row_hint = 0
        scan_candidates = [FIRST_DATA_ROW, int(plan.append_row or 0),
                           int(plan.last_data_row or 0), row_hint]
        scan_candidates.extend(int(c.pre_row or c.insert_row or c.row or 0)
                               for c in new_changes)
        scan_candidates.extend(int(row) for row, _name, _phone
                               in (plan.baseline_name_rows or ()))
        scan_to = max(FIRST_DATA_ROW, max(scan_candidates) + 10)
        scan_to = min(scan_to, MAX_SCAN_ROW)
        current = read_person_rows(cli, plan, worksheet_id, last_row=scan_to)

        # W8：计划之后并发出现同名+同电话客户，无论出现在哪一行，都不能再按新增处理。
        for key in new_person_keys:
            if current.get(key):
                problems.append(
                    f"云端已出现客户「{key[0]}/{key[1]}」，不能再按新增行处理")

        # W4：append_only 计划不会再调用 insert_rows，目标行必须仍然只有表头/空白；
        # 否则会直接把协作者刚填的姓名/电话/地址/餐次/日期覆盖掉。
        if block is not None and block.append_only:
            target_rows = sorted({int(c.insert_row or c.pre_row or c.row)
                                  for c in new_changes
                                  if (c.insert_row or c.pre_row or c.row)})
            target_rows = [row for row in target_rows if row >= FIRST_DATA_ROW]
            if target_rows:
                cols: list[int] = []
                for key in ("name", "phone", "address", "type", "kind",
                            "total", "served", "left", "remark"):
                    column = int(plan.columns.get(key) or 0)
                    if column:
                        cols.append(column)
                if plan.target_col:
                    cols.append(int(plan.target_col))
                cols = sorted(set(cols))
                if cols:
                    lo, hi = min(target_rows), max(target_rows)
                    target_grid = cli.read_grid(
                        plan.file_id, worksheet_id, lo - 1, hi - 1,
                        min(cols) - 1, max(cols) - 1)
                    for row in target_rows:
                        for column in cols:
                            text = str(target_grid.get((row - 1, column - 1), "") or "").strip()
                            if text:
                                problems.append(
                                    f"新增行目标第 {row} 行已有协作者内容"
                                    f"（{column_name(column)}={text[:20]}），拒绝覆盖")
                                break

        # 非 append-only 计划仍会先 insert_rows；但若计划插入边界已超出当前有效数据区，
        # 说明表结构/数据被协作者改动，宁可不写也让用户重新预览。
        if block is not None and not block.append_only:
            current_last = max((max(rows) for rows in current.values()),
                               default=FIRST_DATA_ROW - 1)
            if current_last + 1 < int(block.first_row or 0):
                problems.append(
                    f"插入边界已变化：计划插入行 {block.first_row} 超出当前数据区"
                    f"（最后一行 {current_last}），拒绝写入")
    return problems


def _apply_one_plan(cli: KdocsCli, plan: SheetPlan, worksheet_id: int, *,
                    journal: SyncJournal, operation_id: str, record_key: str,
                    ledger: SyncLedger | None, marker_enabled: bool,
                    emit: Callable[[str], Any]) -> dict[str, Any]:
    """执行单张计划；每个返回值都带 ``uncertain``/``next_action``。

    内部顺序与旧版一致；区别只在：每个可能已写的异常先落到日志+实际回读分类，
    只有能证明云端完整成功才返回 ok，只有能证明完全没执行才返回 safe failed。
    """
    # 第一笔云端写入之前把状态推进到 writing；失败则一个格子都不写。
    if not _journal_set(journal, operation_id, record_key, "writing", emit=emit,
                        next_action="recover_journal"):
        return {"status": "uncertain", "reason": "意图日志不可写，未执行任何云端写入",
                "uncertain": True, "next_action": "fix_journal", "problems": []}

    # 临执行前的只读复核：身份、旧值、目标格任一变化就零写入退回预览。
    # 没有远端 CAS，这一步只能缩小“计划 → 第一次写”之间的协作者改动窗口。
    try:
        pre_problems = _precheck_plan_before_write(cli, plan, worksheet_id)
    except WpsCloudError as exc:
        reason = f"上传前只读复核失败：{exc}"
        emit(f"[云同步] {plan.sheet} {reason}", "ERROR")
        return {"status": "failed", "reason": reason, "uncertain": False,
                "next_action": "repreview", "problems": []}
    if pre_problems:
        reason = "云端在预览/计划后已发生变化，未写入任何格子"
        emit(f"[云同步] {plan.sheet}：{reason}；" + "；".join(pre_problems[:5]),
             "ERROR")
        return {"status": "failed", "reason": reason, "uncertain": False,
                "next_action": "repreview", "problems": pre_problems[:20]}

    inserted: list[tuple[int, int]] = []
    new_changes = [change for change in plan.changes if change.kind == "new"]
    block = plan.insert_blocks[0] if plan.insert_blocks else None

    # 1) 插入新客户行。
    if block and not block.append_only:
        try:
            cli.insert_rows(plan.file_id, worksheet_id,
                            row=block.first_row, count=block.count)
            inserted.append((block.first_row, block.count))
            emit(f"[云同步] {plan.sheet}：已在第 {block.first_row} 行前插入 "
                 f"{block.count} 行（新客户）")
        except WpsCloudError as exc:
            reason = f"插入新行失败：{exc}"
            emit(f"[云同步] {plan.sheet} {reason}")
            classified = _classify_cloud_now(cli, journal, operation_id, record_key)
            if classified.get("state") == "verified":
                return {"status": "ok", "reason": reason, "uncertain": False,
                        "next_action": "none", "problems": classified.get("problems") or [],
                        "warning": "插入接口报错但云端期望值已完整",
                        "cloud_checked": True, "evidence": "executor_cloud_readback"}
            return {"status": "uncertain", "reason": reason, "uncertain": True,
                    "next_action": "manual_reconcile",
                    "problems": classified.get("problems") or [],
                    "manual_required": "insert_result_unknown",
                    "cloud_checked": True, "evidence": "executor_cloud_readback"}

    # 2) 排序前写入新行的整行信息（写在 insert_row，排序后跟着行走）。
    base_cells: list[dict[str, Any]] = []
    for change in new_changes:
        at = change.insert_row or change.pre_row or change.row
        base_cells.append({"row": at, "col": plan.columns["name"], "value": change.name})
        if plan.columns.get("address"):
            base_cells.append({"row": at, "col": plan.columns["address"],
                               "value": change.address})
        base_cells.append({"row": at, "col": plan.columns["phone"], "value": change.phone})
        if plan.columns.get("type") and change.meal_type:
            base_cells.append({"row": at, "col": plan.columns["type"],
                               "value": change.meal_type})
        if plan.columns.get("kind") and change.meal_kind:
            base_cells.append({"row": at, "col": plan.columns["kind"],
                               "value": change.meal_kind})
    if base_cells:
        try:
            cli.write_cells(plan.file_id, worksheet_id, base_cells)
        except WpsCloudError as exc:
            reason = f"新客户行写入失败：{exc}"
            emit(f"[云同步] {plan.sheet} {reason}")
            rollback_ok = _rollback_and_cleanup(cli, plan, worksheet_id, inserted, emit,
                                                helpers_written=False)
            return _safe_failed_after_classify(cli, journal, operation_id, record_key,
                                               emit, reason, rollback_ok=rollback_ok)

    # 3) 格式：读参考格式失败可以跳过（没有发起写）；一旦真正调用格式写接口后
    #    报错/超时，则格式可能已部分生效，按“可能已写未确认”返回 uncertain。
    if new_changes:
        col_lo = plan.columns.get("name") or 1
        col_hi = plan.columns.get("remark") or (col_lo + 12)
        try:
            spec = learn_row_format(cli, plan.file_id, worksheet_id,
                                    col_from=col_lo, col_to=col_hi,
                                    rows=[r for r in plan.format_rows if r],
                                    cached_fills=plan.format_fills)
        except WpsCloudError as exc:
            emit(f"[云同步] {plan.sheet}：读不到参考行格式，跳过格式设置：{exc}", "WARN")
            spec = {}
        if spec:
            econ = [change.insert_row or change.row for change in new_changes
                    if str(change.meal_kind).strip() != "豪华"]
            lux = [change.insert_row or change.row for change in new_changes
                   if str(change.meal_kind).strip() == "豪华"]
            try:
                ops = build_format_ops(
                    spec, econ_rows=econ, lux_rows=lux,
                    col_from=col_lo, col_to=col_hi,
                    plain_to=(plan.columns.get("kind") or plan.columns.get("type") or 0),
                    band_cols=tuple(c for c in (plan.columns.get("total"),
                                                plan.columns.get("served"),
                                                plan.columns.get("left")) if c))
                cli.write_format_ops(plan.file_id, worksheet_id, ops)
            except WpsCloudError as exc:
                reason = f"新客户格式写入失败/未确认：{exc}"
                emit(f"[云同步] {plan.sheet} {reason}", "ERROR")
                return {"status": "uncertain", "reason": reason, "uncertain": True,
                        "next_action": "manual_reconcile", "problems": []}
            emit(f"[云同步] {plan.sheet}：{len(new_changes)} 个新客户已套用表格原有格式")
        else:
            emit(f"[云同步] {plan.sheet}：读不到参考行格式，跳过格式设置")

    # 4) 排序：辅助列里带 token；排序后读 token 精确回填每行的槽位身份。
    token_by_pre_row: dict[int, str] = {}
    for index, change in enumerate(plan.changes):
        pre_row = int(change.pre_row or change.insert_row or 0)
        if pre_row:
            token_by_pre_row[pre_row] = _sort_token(index)
    details: dict[tuple[str, str], list[tuple[int, str]]] = {}
    if plan.sort_enabled and plan.sort_key_col and plan.row_keys:
        safe, found_at = probe_sort_area(
            cli, plan, worksheet_id, col_from=plan.sort_key_col + 1,
            row_to=max(plan.last_data_row, FIRST_DATA_ROW))
        if not safe:
            reason = (f"第 {plan.sort_key_col} 列右侧（{found_at}）还有内容，"
                      "排序区覆盖不到它，已放弃排序以免行与列错位")
            emit(f"[云同步] {plan.sheet}：{reason}", "ERROR")
            rollback_ok = _rollback_and_cleanup(cli, plan, worksheet_id, inserted, emit,
                                                helpers_written=False)
            return _safe_failed_after_classify(cli, journal, operation_id, record_key,
                                               emit, reason, rollback_ok=rollback_ok)

        key_cells: list[dict[str, Any]] = []
        for row, sort_key in sorted(plan.row_keys.items()):
            # 第 2 段写排序前的物理行号：等键时字典序仍等于稳定排序的源顺序；
            # 第 3 段才是槽位身份 token（只有本次变更行有）。
            value = f"{format_sort_key(sort_key)}:{int(row):05d}"
            token = token_by_pre_row.get(int(row))
            if token:
                value = f"{value}:{token}"
            key_cells.append({"row": int(row), "col": plan.sort_key_col, "value": value})
        try:
            cli.write_cells(plan.file_id, worksheet_id, key_cells)
            cli.sort_range(plan.file_id, worksheet_id, range_ref=plan.sort_range,
                           key=column_name(plan.sort_key_col), order="asc", header=False)
        except WpsCloudError as exc:
            # 关键安全判断：排序有没有可能已经生效？用辅助列 token 的当前行号
            # 与排序前预期行号对比；token 还在原位才允许删插入行，否则只报告阻断。
            safe_to_rollback = False
            try:
                current_tokens = _read_sort_tokens(cli, plan, worksheet_id)
                safe_to_rollback = all(
                    current_tokens.get(token) == pre_row
                    for pre_row, token in token_by_pre_row.items())
            except WpsCloudError:
                safe_to_rollback = False
            reason = f"按地址排序失败：{exc}"
            emit(f"[云同步] {plan.sheet} {reason}", "ERROR")
            if safe_to_rollback:
                rollback_ok = _rollback_and_cleanup(cli, plan, worksheet_id, inserted, emit,
                                                    helpers_written=True)
                return _safe_failed_after_classify(cli, journal, operation_id,
                                                   record_key, emit, reason,
                                                   rollback_ok=rollback_ok)
            return {"status": "uncertain", "reason": reason, "uncertain": True,
                    "next_action": "manual_reconcile", "problems": []}

        # 排序已生效：读辅助列 token、删辅助列、回读人员行。
        try:
            token_rows = _read_sort_tokens(cli, plan, worksheet_id)
        except WpsCloudError as exc:
            reason = f"排序后辅助身份列回读失败：{exc}"
            emit(f"[云同步] {plan.sheet} {reason}", "ERROR")
            return {"status": "uncertain", "reason": reason, "uncertain": True,
                    "next_action": "manual_reconcile", "problems": []}
        try:
            cli.delete_columns(plan.file_id, worksheet_id,
                               column=plan.sort_key_col, rows=plan.last_data_row)
        except WpsCloudError as exc:
            emit(f"[云同步] {plan.sheet}：排序辅助列删除失败：{exc}", "WARN")
            try:
                cli.write_cells(plan.file_id, worksheet_id, [
                    {"row": row, "col": plan.sort_key_col, "value": ""}
                    for row in sorted(plan.row_keys)])
            except WpsCloudError as clear_exc:
                reason = f"排序辅助列无法清理：{clear_exc}"
                emit(f"[云同步] {plan.sheet} {reason}", "ERROR")
                return {"status": "uncertain", "reason": reason, "uncertain": True,
                        "next_action": "manual_reconcile", "problems": []}
        try:
            # 只依据实际回读值重建姓名/电话/地址索引；读不到就停止写入。
            # ``read_person_rows`` 保留作为“老回读路径”的健康检查，避免
            # 人员列回读异常被 token 路径掩盖。
            actual_rows = read_person_rows(cli, plan, worksheet_id,
                                           last_row=plan.last_data_row)
            details = read_person_row_details(cli, plan, worksheet_id,
                                              last_row=plan.last_data_row)
        except WpsCloudError as exc:
            reason = f"排序后无法重新定位人员行号（回读失败）：{exc}"
            emit(f"[云同步] {plan.sheet} {reason}", "ERROR")
            return {"status": "uncertain", "reason": reason, "uncertain": True,
                    "next_action": "manual_reconcile", "problems": []}
        assigned = _assign_change_rows(plan, details, token_rows)
        missing: list[str] = []
        for index, change in enumerate(plan.changes):
            row = assigned.get(index)
            key = person_key(change.name, change.phone)
            if row is None or int(row) not in actual_rows.get(key, []):
                missing.append(change.name)
        if missing:
            reason = f"排序后定位不到这些人：{'、'.join(missing[:5])}"
            emit(f"[云同步] {plan.sheet} {reason}", "ERROR")
            return {"status": "uncertain", "reason": reason, "uncertain": True,
                    "next_action": "manual_reconcile", "problems": []}
        drift = 0
        for index, change in enumerate(plan.changes):
            change.row = assigned[index]
            predicted = plan.final_rows.get(person_key(change.name, change.phone), ())
            if len(predicted) >= change.slot and predicted[change.slot - 1] != change.row:
                drift += 1
                if drift <= 3:
                    emit(f"[云同步] {plan.sheet}：{change.name} 第 {change.slot} 行"
                         f"实际排在第 {change.row} 行，与预测不符（按实际 token 写入）", "WARN")
        if drift:
            plan.sort_mismatch = True
            emit(f"[云同步] {plan.sheet}：{drift} 人的实际行号与预测不同"
                 f"（已按 token 实际行号写入）", "WARN")
        safe, found_at = probe_sort_area(
            cli, plan, worksheet_id, col_from=plan.sort_key_col + 1,
            row_to=max(plan.last_data_row, FIRST_DATA_ROW))
        if not safe:
            reason = (f"排序后第 {plan.sort_key_col} 列右侧出现内容（{found_at}），"
                      "说明排序区未覆盖全部列，已停止写入后续数据")
            emit(f"[云同步] {plan.sheet} {reason}", "ERROR")
            return {"status": "uncertain", "reason": reason, "uncertain": True,
                    "next_action": "manual_reconcile", "problems": []}

    # 5) 写日期格 / 总餐次 / 公式 / 通讯记号。
    cells: list[dict[str, Any]] = []
    pending: list[Change] = []
    for change in plan.changes:
        if not change.needs_write:
            continue
        pending.append(change)
        if change.total_after != change.total_before and plan.columns.get("total"):
            cells.append({"row": change.row, "col": plan.columns["total"],
                          "value": change.total_after})
        if change.kind == "existing":
            if change.fill_type and plan.columns.get("type"):
                cells.append({"row": change.row, "col": plan.columns["type"],
                              "value": change.meal_type})
            if change.fill_kind and plan.columns.get("kind"):
                cells.append({"row": change.row, "col": plan.columns["kind"],
                              "value": change.meal_kind})
        if not change.target_ok and not change.target_blocked:
            cells.append({"row": change.row, "col": change.target_col,
                          "value": CELL_MARK})
    formula_cells = formula_cells_for_new_rows(
        plan, [change.row for change in pending if change.kind == "new"])
    formula_cells.extend(formula_cells_for_new_rows(
        plan, [change.row for change in pending
               if change.kind == "existing" and change.fill_formula]))
    cells.extend(formula_cells)
    if marker_enabled and plan.marker_col:
        cells.append({"row": HEADER_ROW, "col": plan.marker_col,
                      "value": str(plan.weekday_number)})
    if not cells:
        return {"status": "noop", "reason": "", "uncertain": False,
                "next_action": "none", "problems": []}

    try:
        cli.write_cells(plan.file_id, worksheet_id, cells)
    except WpsCloudError as exc:
        reason = f"写入失败：{exc}"
        emit(f"[云同步] {plan.sheet} {reason}", "ERROR")
        classified = _classify_cloud_now(cli, journal, operation_id, record_key)
        if classified.get("state") == "verified":
            return {"status": "ok", "reason": reason, "uncertain": False,
                    "next_action": "none", "people": len(pending),
                    "cells": len(cells), "sort_mismatch": bool(plan.sort_mismatch),
                    "warning": "写入异常但云端期望值已完整",
                    "cloud_checked": True, "evidence": "executor_cloud_readback"}
        return {"status": "uncertain", "reason": reason, "uncertain": True,
                "next_action": "manual_reconcile",
                "problems": classified.get("problems") or [],
                "cloud_checked": True, "evidence": "executor_cloud_readback"}

    # 6) 逐格回读校验（数据 + 公式）。
    rows_written = sorted({int(cell["row"]) for cell in cells
                           if int(cell["row"]) >= FIRST_DATA_ROW})
    if rows_written:
        lo, hi = rows_written[0] - 1, rows_written[-1] - 1
        verify_cols = {plan.target_col}
        if plan.columns.get("total"):
            verify_cols.add(plan.columns["total"])
        if plan.columns.get("name"):
            verify_cols.add(plan.columns["name"])
        for key in ("address", "type", "kind"):
            if plan.columns.get(key):
                verify_cols.add(plan.columns[key])
        if formula_cells:
            for formula_col in (plan.columns.get("served"), plan.columns.get("left")):
                if formula_col:
                    verify_cols.add(formula_col)
        c_lo, c_hi = min(verify_cols) - 1, max(verify_cols) - 1
        try:
            back = cli.read_grid(plan.file_id, worksheet_id, lo, hi, c_lo, c_hi)
        except WpsCloudError as exc:
            reason = f"写入已完成，但回读校验失败（网络/接口问题）：{exc}"
            emit(f"[云同步] {plan.sheet} {reason}", "WARN")
            return {"status": "uncertain", "reason": reason, "uncertain": True,
                    "next_action": "manual_reconcile", "problems": []}
        problems: list[str] = []
        formula_back: dict[tuple[int, int], str] = {}
        if formula_cells:
            try:
                formula_back = cli.read_formulas(
                    plan.file_id, worksheet_id, lo, hi, c_lo, c_hi)
            except WpsCloudError as exc:
                reason = f"写入已完成，但公式回读校验失败：{exc}"
                emit(f"[云同步] {plan.sheet} {reason}", "ERROR")
                return {"status": "uncertain", "reason": reason, "uncertain": True,
                        "next_action": "manual_reconcile", "problems": []}
        name_col = plan.columns.get("name") or 0
        for change in pending:
            if name_col:
                got_name = str(back.get((change.row - 1, name_col - 1), "")).strip()
                if got_name != str(change.name).strip():
                    problems.append(f"第 {change.row} 行应为「{change.name}」，"
                                    f"实际「{got_name}」")
            got_mark = str(back.get((change.row - 1, plan.target_col - 1), "")).strip()
            want_mark = change.target_occupied or CELL_MARK
            if got_mark != want_mark:
                problems.append(f"{change.name}: 目标列应为 {want_mark}，实际 {got_mark!r}")
            if plan.columns.get("total"):
                got_total = _as_int(back.get((change.row - 1,
                                              plan.columns["total"] - 1)))
                if got_total != change.total_after:
                    problems.append(
                        f"{change.name}: 总餐次应为 {change.total_after}，实际 {got_total}")
            need_type = bool(plan.columns.get("type")
                             and (change.kind == "new" or change.fill_type)
                             and change.meal_type)
            need_kind = bool(plan.columns.get("kind")
                             and (change.kind == "new" or change.fill_kind)
                             and change.meal_kind)
            if need_type:
                got_type = str(back.get((change.row - 1,
                                         plan.columns["type"] - 1), "")).strip()
                if got_type != str(change.meal_type).strip():
                    problems.append(f"{change.name}: 类型应为「{change.meal_type}」，"
                                    f"实际「{got_type}」")
            if need_kind:
                got_kind = str(back.get((change.row - 1,
                                         plan.columns["kind"] - 1), "")).strip()
                if got_kind != str(change.meal_kind).strip():
                    problems.append(f"{change.name}: 餐种应为「{change.meal_kind}」，"
                                    f"实际「{got_kind}」")
            if (change.kind == "new" and plan.columns.get("address")
                    and str(change.address).strip()):
                got_address = str(back.get((change.row - 1,
                                            plan.columns["address"] - 1), "")).strip()
                if got_address != str(change.address).strip():
                    problems.append(f"{change.name}: 地址应为「{change.address}」，"
                                    f"实际「{got_address}」")
            if ((change.kind == "new" or change.fill_formula)
                    and plan.columns.get("served") and plan.columns.get("left")
                    and plan.columns.get("total") and plan.date_cols):
                expected_served = (f"=SUM({column_name(min(plan.date_cols))}{change.row}:"
                                   f"{column_name(max(plan.date_cols))}{change.row})")
                expected_left = (f"={column_name(plan.columns['total'])}{change.row}-"
                                 f"{column_name(plan.columns['served'])}{change.row}")
                if formula_back.get((change.row - 1,
                                     plan.columns["served"] - 1)) != expected_served:
                    problems.append(f"{change.name}: 已出餐公式未写入或不正确")
                if formula_back.get((change.row - 1,
                                     plan.columns["left"] - 1)) != expected_left:
                    problems.append(f"{change.name}: 剩余餐公式未写入或不正确")
        if problems:
            for item in problems[:10]:
                emit(f"[云同步] {plan.sheet} 校验不一致：{item}")
            return {"status": "uncertain", "reason": "写入后回读校验未通过",
                    "uncertain": True, "next_action": "manual_reconcile",
                    "problems": problems[:20]}
    return {"status": "ok", "reason": "", "uncertain": False,
            "next_action": "none", "cells": len(cells), "people": len(pending),
            "sort_mismatch": bool(plan.sort_mismatch), "problems": []}


def _plan_ledger_digests(plans: Sequence[SheetPlan]) -> set[str]:
    """计划构建时携带的 ledger 磁盘快照摘要集合（忽略旧计划 None）。"""
    return {str(plan.ledger_digest) for plan in plans
            if getattr(plan, "ledger_digest", None)}


def _operation_lock_target(ledger: SyncLedger | None,
                          journal: SyncJournal | None) -> Any:
    """整次 apply_plan 使用的跨进程 operation 锁路径；没有落盘目标则返回 None。"""
    base = None
    if ledger is not None:
        base = getattr(ledger, "path", None)
    if not base and journal is not None:
        base = getattr(journal, "path", None)
    if not base:
        return None
    name = str(base)
    if name.endswith(".journal"):
        name = name[: -len(".journal")]
    return operation_lock_path_for(name)


def _refresh_journal_after_lock(journal: SyncJournal | None,
                              ledger: SyncLedger | None = None) -> SyncJournal | None:
    """取得跨进程锁后，持久化 journal 必须以磁盘最新状态为准。

    - ``journal`` 为 None 时由内部实现按 ledger 路径重新构造；
    - 传入 ``SyncJournal`` 且有 path 时重新读盘，避免旧对象漏掉 pending；
    - 若 ledger 还有 canonical ``<ledger>.journal`` 而调用方另传了 journal 路径，
      也会把 canonical 中的 pending 以“只增不减”方式并入，封死换路径绕过；
    - 调用者内存里未落盘的 pending/prewrite 同样只增不减地并入，
      保留测试注入能力，但不能删除/覆盖磁盘上的 pending。
    """
    if journal is None or not isinstance(journal, SyncJournal):
        return journal
    path = getattr(journal, "path", None)
    if path:
        fresh = SyncJournal(path)
        try:
            old_pending = journal.pending_operations()
        except Exception:  # noqa: BLE001 - 旧内存对象不可信时以磁盘为准
            old_pending = {}
        operations = fresh.operations()
        for operation_id, operation in old_pending.items():
            if operation_id not in operations and isinstance(operation, dict):
                operations[operation_id] = operation
        journal.data = fresh.data
        journal._removed_operations.clear()
    # ledger 的 canonical journal 是唯一落盘权威：若调用方另传路径或内存 journal，
    # 普通 pending 先并入 canonical，再让 journal.path 指向 canonical，
    # 避免 operation 分裂到别的文件或完全不落盘。
    ledger_path = getattr(ledger, "path", None) if ledger is not None else None
    if ledger_path:
        canonical_path = journal_path_for(ledger_path)
        if str(path or "") != str(canonical_path):
            canonical = SyncJournal(canonical_path)
            operations = canonical.operations()
            try:
                provided_pending = journal.pending_operations()
            except Exception:  # noqa: BLE001 - 旧对象不可信时以 canonical 为准
                provided_pending = {}
            for operation_id, operation in provided_pending.items():
                if operation_id not in operations and isinstance(operation, dict):
                    operations[operation_id] = operation
            journal.data = canonical.data
            journal.path = canonical.path
            journal._removed_operations.clear()
    return journal


def _refresh_ledger_after_lock(ledger: SyncLedger | None
                              ) -> tuple[SyncLedger | None, str, str | None]:
    """取得跨进程锁后校验持久化 ledger；返回 ``(ledger, stale_reason, disk_digest)``。

    ``disk_digest`` 是锁内重新加载的磁盘快照摘要，调用方必须用它和
    ``SheetPlan.ledger_digest``（构建计划时快照）比较；不能只用执行时传入的
    ledger 对象自己的加载摘要冒名顶替计划快照。
    """
    if ledger is None or not isinstance(ledger, SyncLedger):
        return ledger, "", None
    path = getattr(ledger, "path", None)
    if not path:
        return ledger, "", None
    fresh = SyncLedger(path)
    disk_digest = getattr(fresh, "_loaded_digest", None)
    if getattr(ledger, "_loaded_digest", None) != disk_digest:
        return ledger, ("本地账本在构造计划之后已变化；为避免重复加餐/覆盖批次，"
                        "本次零写入，请重新预览"), disk_digest
    return ledger, "", disk_digest


def _plan_guard_reason(journal: SyncJournal,
                      plans: Sequence[SheetPlan]) -> str:
    """同一目标日期 + 云表是否仍有 retired_guarded 防重复闸门。"""
    checker = getattr(journal, "has_guard", None)
    if not callable(checker):
        return ""
    for plan in plans:
        if not plan.target_date or not plan.file_id:
            continue
        try:
            if checker(plan.target_date.isoformat(), plan.file_id):
                return ("该目标日期/表的旧任务已人工退出但仍保留防重复闸门；"
                        "请先人工核对云端并完成恢复确认，不能直接再次上传")
        except Exception:  # noqa: BLE001 - 查询失败按有闸门处理更安全
            return ("无法确认该目标日期/表是否仍有防重复闸门；"
                    "已拒绝写入，请人工核对后重新预览")
    return ""


def _guarded_result(plans: Sequence[SheetPlan], reason: str, *,
                    journal_path: str = "") -> dict[str, Any]:
    operation_id = new_operation_id()
    count = max(1, len(plans) or 1)
    return {
        "status": "uncertain", "uncertain": True, "next_action": "manual_reconcile",
        "reason": reason, "operation_id": operation_id,
        "summary": {"written": 0, "failed": count, "uncertain": True,
                    "sheets": len(plans), "next_action": "manual_reconcile"},
        "journal_path": journal_path, "recovery": None,
        "written": 0, "failed": count,
        "sheets": [{"sheet": plan.sheet, "status": "uncertain",
                    "reason": reason, "uncertain": True,
                    "next_action": "manual_reconcile", "problems": []} for plan in plans],
    }


def _stale_state_result(plans: Sequence[SheetPlan], reason: str, *,
                        journal_path: str = "") -> dict[str, Any]:
    operation_id = new_operation_id()
    count = max(1, len(plans) or 1)
    return {
        "status": "failed", "uncertain": False, "next_action": "repreview",
        "reason": reason, "operation_id": operation_id,
        "summary": {"written": 0, "failed": count, "uncertain": False,
                    "sheets": len(plans), "next_action": "repreview"},
        "journal_path": journal_path, "recovery": None,
        "written": 0, "failed": count,
        "sheets": [{"sheet": plan.sheet, "status": "failed",
                    "reason": reason, "uncertain": False,
                    "next_action": "repreview", "problems": []} for plan in plans],
    }


def _locked_apply_result(plans: Sequence[SheetPlan], reason: str, *,
                         next_action: str = "wait_for_recovery_lock",
                         journal_path: str = "") -> dict[str, Any]:
    operation_id = new_operation_id()
    count = max(1, len(plans) or 1)
    return {
        "status": "uncertain", "uncertain": True, "next_action": next_action,
        "reason": reason, "operation_id": operation_id,
        "summary": {"written": 0, "failed": count, "uncertain": True,
                    "sheets": len(plans), "next_action": next_action},
        "journal_path": journal_path, "recovery": None,
        "written": 0, "failed": count,
        "sheets": [{"sheet": plan.sheet, "status": "uncertain",
                    "reason": reason, "uncertain": True,
                    "next_action": next_action, "problems": []} for plan in plans],
    }


def apply_plan(cli: KdocsCli, plans: Iterable[SheetPlan], *,
               ledger: SyncLedger | None = None,
               marker_enabled: bool = True,
               log: Callable[[str], Any] | None = None,
               journal: SyncJournal | None = None) -> dict[str, Any]:
    """跨进程串行入口：先取 operation 锁，再调用内部实现。

    无落盘目标（ledger/journal 都是内存对象）时保持旧内存行为，不创建锁文件。
    取锁失败一律零云端写入并返回 ``uncertain``，不得直接重试。
    """
    plan_list = list(plans)
    plan_digests = _plan_ledger_digests(plan_list)
    journal_path = ""
    if journal is not None and getattr(journal, "path", None):
        journal_path = str(journal.path)
    elif ledger is not None:
        journal_path = str(getattr(ledger, "journal_path", "") or "")
    lock_target = _operation_lock_target(ledger, journal)
    if lock_target is None:
        if plan_digests:
            reason = ("计划携带构建时账本快照，但本次没有可校验的持久化账本路径；"
                      "为避免用旧计划重复写入，已拒绝执行，请重新预览")
            emit = log or (lambda *_a, **_k: None)
            emit(f"[云同步] {reason}", "ERROR")
            return _stale_state_result(plan_list, reason, journal_path=journal_path)
        return _apply_plan_impl(cli, plan_list, ledger=ledger,
                                marker_enabled=marker_enabled, log=log,
                                journal=journal)
    lock = FileLock(lock_target, timeout=APPLY_PLAN_LOCK_TIMEOUT)
    try:
        lock.acquire()
    except LockTimeout as exc:
        reason = f"另一个进程正在执行云同步，等待锁超时，已拒绝写入：{exc}"
        emit = log or (lambda *_a, **_k: None)
        emit(f"[云同步] {reason}", "ERROR")
        return _locked_apply_result(plan_list, reason,
                                    next_action="wait_for_recovery_lock",
                                    journal_path=journal_path)
    except (AtomicWriteError, OSError, RuntimeError) as exc:
        reason = f"本地存储不可用，无法取得跨进程锁，已拒绝写入：{exc}"
        emit = log or (lambda *_a, **_k: None)
        emit(f"[云同步] {reason}", "ERROR")
        return _locked_apply_result(plan_list, reason, next_action="fix_journal",
                                    journal_path=journal_path)
    try:
        try:
            journal = _refresh_journal_after_lock(journal, ledger)
        except Exception as exc:  # noqa: BLE001 - 最新日志不可读必须失败关闭
            reason = f"意图日志在锁内刷新失败，已拒绝任何云端写入：{exc}"
            emit = log or (lambda *_a, **_k: None)
            emit(f"[云同步] {reason}", "ERROR")
            return _locked_apply_result(plan_list, reason, next_action="fix_journal",
                                        journal_path=journal_path)
        try:
            ledger, stale_reason, disk_digest = _refresh_ledger_after_lock(ledger)
        except Exception as exc:  # noqa: BLE001 - 最新账本不可读必须失败关闭
            reason = f"本地账本在锁内刷新失败，已拒绝任何云端写入：{exc}"
            emit = log or (lambda *_a, **_k: None)
            emit(f"[云同步] {reason}", "ERROR")
            return _locked_apply_result(plan_list, reason, next_action="fix_journal",
                                        journal_path=journal_path)
        if plan_digests:
            if disk_digest is None:
                if any(digest != LEDGER_SNAPSHOT_ABSENT for digest in plan_digests):
                    stale_reason = ("计划构建时已有账本快照，但锁内磁盘账本不存在；"
                                    "已拒绝写入，请重新预览")
            elif any(digest == LEDGER_SNAPSHOT_ABSENT or digest != disk_digest
                     for digest in plan_digests):
                stale_reason = ("计划构建时的账本快照与当前磁盘账本不一致；"
                                "已拒绝写入，请重新预览")
        if stale_reason:
            emit = log or (lambda *_a, **_k: None)
            emit(f"[云同步] {stale_reason}", "ERROR")
            return _stale_state_result(plan_list, stale_reason,
                                       journal_path=journal_path)
        result = _apply_plan_impl(cli, plan_list, ledger=ledger,
                                  marker_enabled=marker_enabled, log=log,
                                  journal=journal)
        if str(result.get("status") or "") == "ok":
            compact_journal = journal
            if compact_journal is None and ledger is not None:
                ledger_path = getattr(ledger, "path", None)
                if ledger_path:
                    compact_journal = SyncJournal(journal_path_for(ledger_path))
            if compact_journal is not None:
                try:
                    compact_journal.compact(
                        ledger=ledger,
                        keep_operations=DEFAULT_COMPACT_KEEP_OPERATIONS)
                except Exception as exc:  # noqa: BLE001 - 归档失败不影响云端结果
                    emit = log or (lambda *_a, **_k: None)
                    emit(f"[云同步] 意图日志归档跳过（不影响本次结果）：{exc}", "WARN")
        return result
    finally:
        lock.release()


def _apply_plan_impl(cli: KdocsCli, plans: Iterable[SheetPlan], *,
                     ledger: SyncLedger | None = None,
                     marker_enabled: bool = True,
                     log: Callable[[str], Any] | None = None,
                     journal: SyncJournal | None = None) -> dict[str, Any]:
    """按计划写入云端；写入前持久化意图，写入后回读校验，成功才更新账本。

    返回值保留旧字段 ``written``/``failed``/``sheets``；新增：
    ``status``、``uncertain``、``next_action``、``operation_id``、``summary``、
    ``journal_path``、``recovery``。任何“可能已写但无法确认”的结果都会
    ``uncertain=true``，每张不确定的表带 ``status="uncertain"`` 与 ``next_action``。
    """
    emit = log or (lambda _msg, _level="INFO": None)
    plan_list = list(plans)
    operation_id = new_operation_id()
    result: dict[str, Any] = {
        "status": "noop", "uncertain": False, "next_action": "", "reason": "",
        "operation_id": operation_id, "summary": {},
        "journal_path": "", "recovery": None,
        "sheets": [], "written": 0, "failed": 0,
    }

    guard_reason = ""
    try:
        if journal is None:
            ledger_path = getattr(ledger, "path", None) if ledger is not None else None
            journal = (SyncJournal(journal_path_for(ledger_path))
                       if ledger_path else SyncJournal())
        result["journal_path"] = str(journal.path or "")
        guard_reason = _plan_guard_reason(journal, plan_list)
        pending_ops = journal.pending_operations()
    except Exception as exc:  # noqa: BLE001 - 日志不可读必须失败关闭
        reason = f"意图日志不可读（{exc}）：已拒绝任何云端写入"
        result.update(status="uncertain", uncertain=True, next_action="fix_journal",
                      reason=reason,
                      failed=max(1, len(plan_list)),
                      sheets=[{"sheet": plan.sheet, "status": "uncertain",
                               "reason": reason, "uncertain": True,
                               "next_action": "fix_journal", "problems": []}
                              for plan in plan_list],
                      summary={"failed": max(1, len(plan_list)), "uncertain": True,
                               "next_action": "fix_journal"})
        emit(f"[云同步] {reason}", "ERROR")
        return result
    if guard_reason:
        emit(f"[云同步] {guard_reason}", "ERROR")
        return _guarded_result(plan_list, guard_reason,
                               journal_path=str(journal.path or ""))
    if pending_ops:
        try:
            report = recover_pending_operations(cli, journal, ledger=ledger, log=emit)
        except Exception as exc:  # noqa: BLE001 - 恢复失败不能当作没有未完成操作
            reason = f"恢复对账异常：{type(exc).__name__}: {exc}"
            result.update(status="uncertain", uncertain=True,
                          next_action="fix_journal", reason=reason,
                          failed=max(1, len(plan_list) or 1),
                          sheets=[{"sheet": plan.sheet, "status": "uncertain",
                                   "reason": reason, "uncertain": True,
                                   "next_action": "fix_journal", "problems": []}
                                  for plan in plan_list],
                          summary={"failed": max(1, len(plan_list) or 1),
                                   "uncertain": True,
                                   "next_action": "fix_journal"})
            emit(f"[云同步] {reason}：已拒绝本次任何云端写入", "ERROR")
            return result
        result["recovery"] = report
        result["operation_id"] = (report["operations"][0]["operation_id"]
                                  if report.get("operations") else operation_id)
        sheet_items: list[dict[str, Any]] = []
        for operation in report.get("operations", []):
            for sheet in operation.get("sheets", []):
                sheet_items.append({
                    "sheet": sheet.get("sheet", ""),
                    "status": sheet.get("status", "uncertain"),
                    "reason": sheet.get("reason", ""),
                    "problems": sheet.get("problems", []),
                    "next_action": sheet.get("next_action", "manual_reconcile"),
                    "uncertain": bool(sheet.get("status") == "uncertain"),
                })
        result["sheets"] = sheet_items
        result["written"] = 0
        result["failed"] = max(1, len(plan_list) or 1)
        result["uncertain"] = bool(report.get("uncertain"))
        result["status"] = str(report.get("status") or "uncertain")
        result["next_action"] = str(report.get("next_action") or "manual_reconcile")
        if report.get("status") == "recovered":
            result["reason"] = "已恢复历史完整操作，本次未写入；请重新预览后再上传"
        elif report.get("status") == "not_started":
            result["reason"] = "历史操作已确认完全未执行；请重新预览后再上传"
        elif report.get("status") == "uncertain":
            result["reason"] = "存在未完成或无法确认的意图日志，已阻断本次写入；请先人工核对云端"
        result["summary"] = {
            "written": 0, "failed": result["failed"],
            "uncertain": result["uncertain"],
            "next_action": result["next_action"],
            "recovered": len(report.get("recovered_operation_ids") or []),
            "not_started": len(report.get("not_started_operation_ids") or []),
        }
        if report.get("status") == "uncertain":
            emit("[云同步] 存在未完成/不确定的意图日志，已阻断本次写入；"
                 "请先按核对指引人工处理", "ERROR")
        else:
            emit(f"[云同步] 已恢复历史操作：{report.get('status')}；"
                 f"本次未写入，请重新预览后再上传", "WARN")
        return result

    pre_items: dict[int, dict[str, Any]] = {}
    runnable: list[tuple[int, SheetPlan, int, str]] = []
    for index, plan in enumerate(plan_list):
        if plan.blocked_reason:
            emit(f"[云同步] {plan.sheet}：{plan.blocked_reason}", "ERROR")
            pre_items[index] = {"sheet": plan.sheet, "status": "stale_batch",
                                "reason": plan.blocked_reason, "uncertain": False,
                                "next_action": "none", "problems": []}
            continue
        if not plan.target_col:
            pre_items[index] = {"sheet": plan.sheet, "status": "skipped",
                                "reason": "未找到目标日期列", "uncertain": False,
                                "next_action": "none", "problems": []}
            continue
        try:
            infos = cli.sheets_info(plan.file_id)
        except WpsCloudError as exc:
            reason = f"云端表信息读取失败：{exc}"
            pre_items[index] = {"sheet": plan.sheet, "status": "failed",
                                "reason": reason, "uncertain": False,
                                "next_action": "repreview", "problems": []}
            emit(f"[云同步] {plan.sheet} {reason}", "ERROR")
            continue
        if not infos:
            pre_items[index] = {"sheet": plan.sheet, "status": "failed",
                                "reason": "云端文件不可读", "uncertain": False,
                                "next_action": "repreview", "problems": []}
            continue
        worksheet_id = int(infos[0].get("sheetId") or 1)
        record_key = f"{index}:{plan.sheet}:{plan.file_id}"
        runnable.append((index, plan, worksheet_id, record_key))

    if runnable:
        records: dict[str, dict[str, Any]] = {}
        for _index, plan, worksheet_id, record_key in runnable:
            records[record_key] = _build_journal_record(plan, worksheet_id,
                                                        marker_enabled, ledger)
        try:
            journal.create_operation(operation_id, records,
                                     target_date=(plan_list[runnable[0][0]].target_date.isoformat()
                                                  if runnable[0][1].target_date else ""))
            journal.save()
        except Exception as exc:  # noqa: BLE001 - 意图无法落盘则零云端写入
            reason = f"写入前无法持久化意图日志：{exc}"
            emit(f"[云同步] {reason}", "ERROR")
            for index, plan, _worksheet_id, _record_key in runnable:
                pre_items[index] = {"sheet": plan.sheet, "status": "uncertain",
                                    "reason": reason, "uncertain": True,
                                    "next_action": "fix_journal", "problems": []}
            result.update(status="uncertain", uncertain=True, next_action="fix_journal")
        else:
            stop_after_uncertain = False
            for index, plan, worksheet_id, record_key in runnable:
                if stop_after_uncertain:
                    pre_items[index] = {
                        "sheet": plan.sheet, "status": "blocked", "uncertain": True,
                        "reason": "前一张表结果不确定，已阻断后续云端写入",
                        "next_action": "manual_reconcile", "problems": []}
                    continue
                item = _apply_one_plan(cli, plan, worksheet_id,
                                       journal=journal, operation_id=operation_id,
                                       record_key=record_key, ledger=ledger,
                                       marker_enabled=marker_enabled, emit=emit)
                if item.get("status") == "ok":
                    record = (journal.get_operation(operation_id) or {}).get("sheets", {}).get(record_key, {})
                    entries = record.get("ledger_entries") or {}
                    target_date = str(record.get("target_date") or "")
                    if entries and ledger is not None:
                        if not _journal_set(journal, operation_id, record_key,
                                            "ledger_pending", emit=emit,
                                            next_action="recover_journal",
                                            reason="", problems=[],
                                            cloud_checked=True,
                                            evidence="executor_cloud_readback"):
                            item = {"status": "uncertain", "uncertain": True,
                                    "next_action": "fix_journal",
                                    "reason": "意图日志在记账前保存失败",
                                    "problems": []}
                        else:
                            committed, reason = _commit_ledger_entries(
                                ledger, target_date, plan.file_id, entries)
                            if not committed:
                                item = {"status": "uncertain", "uncertain": True,
                                        "next_action": "manual_reconcile",
                                        "reason": reason, "problems": [reason]}
                            else:
                                if not _journal_set(journal, operation_id, record_key,
                                                    "verified", emit=emit,
                                                    next_action="none",
                                                    reason="", problems=[],
                                                    cloud_checked=True,
                                                    evidence="executor_cloud_readback"):
                                    item = {"status": "uncertain", "uncertain": True,
                                            "next_action": "fix_journal",
                                            "reason": "账本已保存但意图日志收尾失败",
                                            "problems": []}
                    elif entries and ledger is None and journal.path is not None:
                        _journal_set(journal, operation_id, record_key,
                                     "ledger_pending", emit=emit,
                                     next_action="recover_journal",
                                     reason="ledger_missing", problems=["ledger_missing"],
                                     cloud_checked=True,
                                     evidence="executor_cloud_readback")
                        item = {"status": "uncertain", "uncertain": True,
                                "next_action": "manual_reconcile",
                                "reason": "没有可注入的账本，无法确认幂等锚点",
                                "problems": ["ledger_missing"]}
                    else:
                        _journal_set(journal, operation_id, record_key,
                                     "verified", emit=emit, next_action="none",
                                     reason="", problems=[],
                                     cloud_checked=True,
                                     evidence="executor_cloud_readback")
                elif item.get("status") == "failed":
                    _journal_set(journal, operation_id, record_key,
                                 "failed_no_write", emit=emit, next_action="none",
                                 reason=item.get("reason", ""),
                                 problems=item.get("problems") or [],
                                 cloud_checked=bool(item.get("cloud_checked", False)),
                                 evidence=str(item.get("evidence") or "local_journal"))
                elif item.get("status") == "noop":
                    _journal_set(journal, operation_id, record_key,
                                 "verified", emit=emit, next_action="none",
                                 reason="", problems=[])
                else:
                    manual_required = str(item.get("manual_required") or "")
                    _journal_set(journal, operation_id, record_key, "uncertain",
                                 emit=emit,
                                 next_action=item.get("next_action", "manual_reconcile"),
                                 reason=item.get("reason", "结果不确定"),
                                 problems=item.get("problems") or [],
                                 manual_required=manual_required,
                                 cloud_checked=bool(item.get("cloud_checked", False)),
                                 evidence=str(item.get("evidence") or "local_journal"))
                pre_items[index] = item
                if item.get("uncertain"):
                    stop_after_uncertain = True

    # 输出：按原计划顺序保留旧 sheets 结构，并给出顶层状态。
    result_sheets: list[dict[str, Any]] = []
    written = failed = uncertain_count = 0
    for index, plan in enumerate(plan_list):
        item = pre_items.get(index) or {
            "sheet": plan.sheet, "status": "failed", "reason": "未执行",
            "uncertain": True, "next_action": "manual_reconcile", "problems": []}
        status = str(item.get("status") or "failed")
        item = {**item, "sheet": plan.sheet}
        item.setdefault("next_action", "none")
        item["uncertain"] = bool(item.get("uncertain"))
        if status in ("ok",):
            written += 1
        elif status in ("stale_batch", "failed", "blocked"):
            failed += 1
        elif status == "uncertain":
            failed += 1
            uncertain_count += 1
        if item.get("uncertain") and status not in ("ok",):
            uncertain_count = max(uncertain_count, 1)
        result_sheets.append(item)
    has_uncertain_status = any(item.get("status") == "uncertain" for item in result_sheets)
    has_blocked_status = any(item.get("status") == "blocked" for item in result_sheets)
    any_uncertain = bool(uncertain_count) or any(
        bool(item.get("uncertain")) for item in result_sheets)
    if has_uncertain_status:
        top_status = "uncertain"
        if any(item.get("status") == "uncertain"
               and item.get("next_action") == "fix_journal"
               and not any(other.get("status") == "uncertain"
                           and other.get("next_action") == "manual_reconcile"
                           for other in result_sheets)
               for item in result_sheets):
            next_action = "fix_journal"
        else:
            next_action = "manual_reconcile"
    elif has_blocked_status:
        top_status, next_action = "blocked", (
            "fix_journal" if any(item.get("next_action") == "fix_journal"
                                for item in result_sheets)
            else "manual_reconcile")
    elif any_uncertain:
        top_status, next_action = "uncertain", "manual_reconcile"
    elif failed:
        top_status = "partial" if written else "failed"
        if not written and any(item.get("next_action") == "repreview"
                               for item in result_sheets):
            next_action = "repreview"
        else:
            next_action = ""
    else:
        top_status, next_action = "ok", ""
    if not result.get("reason"):
        for item in result_sheets:
            if item.get("reason") and item.get("status") in ("uncertain", "failed", "blocked"):
                result["reason"] = str(item.get("reason"))
                break
    result.update(sheets=result_sheets, written=written, failed=failed,
                  status=top_status, uncertain=any_uncertain,
                  next_action=next_action,
                  summary={"written": written, "failed": failed,
                           "uncertain": any_uncertain,
                           "sheets": len(result_sheets),
                           "next_action": next_action})
    if any_uncertain:
        emit("[云同步] 存在结果不确定的表：请按日志核对云端实际值，"
             "不要直接重复提交；确认后再重新预览", "ERROR")
    return result

def learn_row_format(cli: "KdocsCli", file_id: str, worksheet_id: int, *,
                     col_from: int, col_to: int,
                     rows: Sequence[int],
                     cached_fills: Mapping[int, str] | None = None) -> dict[str, Any]:
    """从已有数据行里"学"一行模板：字体 + 对齐 + **每列底色**。

    按列照抄底色（而不是"读回目标行原底色"）的原因：接口不返回空单元格，
    目标行的空列读不到颜色，照抄会让那些列变成无填充（看起来像被涂黑）。
    模板行（如东湖中餐第 99 行）的颜色分布是「A~J 白 / K~M 黄 / N 白」，
    照抄后新行的黄带位置与老行完全一致。

    ``rows`` 里第一个能读到内容的行会被采用；全都读不到返回空 dict，
    调用方应跳过格式写入（宁可不设，也不要写错）。
    """
    for row in rows:
        grid = cli.read_grid(file_id, worksheet_id, row - 1, row - 1, col_from - 1, col_to - 1)
        cells = {col + 1: text for (_r, col), text in grid.items()}
        if not cells:
            continue
        # 逐列底色：优先用 build_plan 预先读好的缓存（省接口调用）；
        # 没有缓存时才逐列读 —— 宽表逐列读会消耗大量调用次数。
        fills: dict[int, str] = dict(cached_fills or {})
        if not fills:
            for col in range(col_from, col_to + 1):
                cell = cli.read_cell_format(file_id, worksheet_id, row, col)
                if cell and cell.get("cell_background_color"):
                    fills[col] = str(cell["cell_background_color"])
        spec_cell = cli.read_cell_format(file_id, worksheet_id, row, col_from)
        if not spec_cell:
            continue
        fonts = spec_cell.get("fonts") or {}
        align = spec_cell.get("alignment") or {}
        return {
            "font_name": fonts.get("font_east_asia") or fonts.get("name") or "",
            "font_size": int(fonts.get("size") or 10),
            "font_color": (_argb_to_int(fonts["color"]) if fonts.get("color") else None),
            "alcH": _ALIGN_H.get(str(align.get("horizontal") or ""), 2),
            "alcV": _ALIGN_V.get(str(align.get("vertical") or ""), 1),
            "fill_default": _argb_to_int(fills.get(col_from, "#FFFFFFFF")),
            "fills": fills,
            "sample_row": row,
        }
    return {}

def build_format_ops(spec: Mapping[str, Any], *, econ_rows: Sequence[int],
                     lux_rows: Sequence[int],
                     col_from: int, col_to: int,
                     plain_to: int = 0,
                     band_cols: Sequence[int] = ()) -> list[dict[str, Any]]:
    """把"新客户行 × 模板格式"压成尽量少的格式操作。

    - 经济行：**第 A 列 ~ 「餐种」列整段刷成模板底色**（用户 2026-09-15 要求）。
      原因：日期列与「类型」之间有时夹着一列**空列**，接口不返回空单元格、读不到
      它的底色，逐列照抄就会漏掉它 —— 那一格于是保留插入时从上一行继承的颜色，
      看起来就是"有一格没涂到"。整段刷过去就不会再有漏网的列。
    - 金黄色的三列（总餐次/已出餐/剩余餐）按**列的身份**固定涂金，不依赖模板行
      那几格有没有内容（实测有客户行的总餐次是空的，模板选到它就学不到金色）。
    - 豪华行：**整行金黄**（名字到备注整段，用户 2026-09-12 确认）；
    - 相邻同色列再合并成区间。

    这样 25 个新行只要 ~1 次接口调用（逐行设格式要 25 次，曾把当日额度打满）。
    """
    base_xf: dict[str, Any] = {"alcH": spec.get("alcH", 2), "alcV": spec.get("alcV", 1)}
    if spec.get("font_name"):
        font: dict[str, Any] = {
            "name": spec["font_name"],
            "dyHeight": int(spec.get("font_size", 10)) * FONT_SIZE_TO_TWIP,
        }
        if spec.get("font_color") is not None:
            font["color"] = {"type": 2, "value": int(spec["font_color"])}
        base_xf["font"] = font

    def _xf(color: int) -> dict[str, Any]:
        xf = dict(base_xf)
        xf["fill"] = {"type": 1, "back": {"type": 2, "value": color},
                      "fore": {"type": 255, "value": 0, "tint": 0}}
        return xf

    def _runs(values: Sequence[int]) -> list[tuple[int, int]]:
        runs: list[tuple[int, int]] = []
        for value in sorted(set(values)):
            if runs and value == runs[-1][1] + 1:
                runs[-1] = (runs[-1][0], value)
            else:
                runs.append((value, value))
        return runs

    fills: Mapping[int, str] = spec.get("fills") or {}
    # 注意：fills 里是 "#AARRGGBB" 字符串，而 fill_default 已经是整数 —— 别再转一次，
    # 否则 str(4294967295) 会被当十六进制解析成 0x94967295（离线仿真抓到的真实教训）。
    if fills.get(col_from):
        base_color = _argb_to_int(str(fills[col_from]))
    else:
        base_color = int(spec.get("fill_default") or 0xFFFFFFFF) & 0xFFFFFFFF
    # 「餐种」列及其左边整段用模板底色；它右边只有金黄三列是特殊的。
    plain_end = plain_to if col_from <= plain_to <= col_to else col_from - 1
    band = {int(c) for c in band_cols if col_from <= int(c) <= col_to}

    def color_of(column: int) -> int:
        if column <= plain_end:
            return base_color
        if column in band:
            return FILL_GOLD
        learned = fills.get(column)
        return _argb_to_int(learned) if learned else base_color

    ops: list[dict[str, Any]] = []
    for r0, r1 in _runs(econ_rows):
        col = col_from
        while col <= col_to:
            color = color_of(col)
            col_end = col
            while col_end + 1 <= col_to and color_of(col_end + 1) == color:
                col_end += 1
            ops.append({"opType": "format",
                        "rowFrom": r0 - 1, "rowTo": r1 - 1,
                        "colFrom": col - 1, "colTo": col_end - 1,
                        "xf": _xf(color)})
            col = col_end + 1
    for r0, r1 in _runs(lux_rows):
        ops.append({"opType": "format",
                    "rowFrom": r0 - 1, "rowTo": r1 - 1,
                    "colFrom": col_from - 1, "colTo": col_to - 1,
                    "xf": _xf(FILL_LUXURY)})
    return ops
