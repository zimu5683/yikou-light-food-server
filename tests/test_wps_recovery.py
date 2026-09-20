"""WPS 云同步 P0：意图日志、恢复对账、异常不确定性与零重复写回归。

只使用合成内存网格与临时账本；不联网、不读真实客户数据、不写真实 WPS 云表。
"""
from __future__ import annotations

import datetime as dt
import json
import multiprocessing
import os
from pathlib import Path
from unittest import mock

import pytest

import app.wps.atomicio as atomicio
from app.wps.errors import LedgerCorruptError, WpsCloudError
from app.wps.journal import SyncJournal, journal_path_for
from app.wps.sync import (
    CloudOrder,
    FileLock,
    LockTimeout,
    SyncLedger,
    apply_plan,
    atomic_write_text,
    build_plan,
    recover_pending_operations,
    recovery_status,
    resolve_pending_operation,
)

TARGET = dt.date(2026, 9, 11)


def _fork_ctx():
    """多进程锁回归只在支持 fork 的 POSIX 环境跑；其它平台 skip 而非失败。"""
    try:
        return multiprocessing.get_context("fork")
    except ValueError:
        pytest.skip("当前平台不支持 fork，无法执行跨进程锁回归")


BASE_HEADER = {
    0: "名字", 1: "地址", 2: "电话", 3: "9.10 周四", 4: "9.11 周五",
    5: "类型", 6: "餐种", 7: "总餐次", 8: "已出餐", 9: "剩余餐",
    10: "备注", 12: "9.11 周五",
}


def make_grid(header=None, rows=None):
    grid = {}
    for col, text in (header or BASE_HEADER).items():
        grid[(1, col)] = text
    for idx, row in enumerate(rows or [], start=2):
        for col, text in row.items():
            if text not in (None, ""):
                grid[(idx, col)] = str(text)
    return grid


class GridCli:
    """可控故障注入的内存云端替身（排序/插入/回读/公式都可注入失败）。"""

    def __init__(self, grid, *, fail_write_on_call: int = 0,
                 fail_write_after_cells: int = 0, fail_insert: str = "",
                 fail_sort: str = "", fail_delete: bool = False,
                 fail_formula_reads: int = 0, fail_read_after_write: int = 0,
                 stale_reads_after_write: int = 0, crash_on_write_call: int = 0):
        self.grid = dict(grid)
        self.fail_write_on_call = fail_write_on_call
        self.fail_write_after_cells = fail_write_after_cells
        self.fail_insert = fail_insert
        self.fail_sort = fail_sort
        self.fail_delete = fail_delete
        self.fail_formula_reads = fail_formula_reads
        self.fail_read_after_write = fail_read_after_write
        self.stale_reads_after_write = stale_reads_after_write
        self.crash_on_write_call = crash_on_write_call
        self.write_call = 0
        self.after_write = False
        self._snapshot = None
        self.writes: list[list[dict]] = []
        self.inserts: list[tuple[int, int]] = []
        self.deleted_rows: list[tuple[int, int]] = []
        self.deleted_columns: list[tuple[int, int]] = []
        self.sorts: list[dict] = []

    def sheets_info(self, file_id):
        return [{"sheetId": 1, "sheetName": "Sheet1", "rowTo": 200, "colTo": 50}]

    def _apply(self, cells):
        for cell in cells:
            self.grid[(int(cell["row"]) - 1, int(cell["col"]) - 1)] = str(cell["value"])

    def write_cells(self, file_id, worksheet_id, cells):
        self.write_call += 1
        cells = list(cells)
        self.writes.append(cells)
        if self._snapshot is None:
            self._snapshot = dict(self.grid)
        if self.crash_on_write_call and self.write_call == self.crash_on_write_call:
            self._apply(cells)
            self.after_write = True
            raise KeyboardInterrupt("模拟进程被杀")
        if self.fail_write_on_call and self.write_call == self.fail_write_on_call:
            if self.fail_write_after_cells:
                self._apply(cells[:self.fail_write_after_cells])
                self.after_write = True
            raise WpsCloudError("模拟写入超时/部分批次失败")
        self._apply(cells)
        if self.write_call >= 1:
            self.after_write = True

    def read_grid(self, file_id, worksheet_id, row_from, row_to, col_from, col_to,
                  *, with_format=False):
        if (self.after_write and not with_format and self.fail_read_after_write > 0):
            self.fail_read_after_write -= 1
            raise WpsCloudError("模拟回读接口失败")
        if (self.after_write and not with_format and self.stale_reads_after_write > 0):
            self.stale_reads_after_write -= 1
            source = self._snapshot or {}
            return {key: value for key, value in source.items()
                    if row_from <= key[0] <= row_to and col_from <= key[1] <= col_to}
        return {key: value for key, value in self.grid.items()
                if row_from <= key[0] <= row_to and col_from <= key[1] <= col_to}

    def read_formulas(self, file_id, worksheet_id, row_from, row_to, col_from, col_to):
        if self.fail_formula_reads > 0:
            self.fail_formula_reads -= 1
            raise WpsCloudError("模拟公式接口失败")
        return {key: value for key, value in self.grid.items()
                if row_from <= key[0] <= row_to and col_from <= key[1] <= col_to
                and str(value).startswith("=")}

    def insert_rows(self, file_id, worksheet_id, *, row, count):
        if self.fail_insert == "before":
            raise WpsCloudError("模拟插入接口超时（可能未执行）")
        lo = row - 1
        shifted = {}
        for (r, c), value in self.grid.items():
            shifted[(r if r < lo else r + count, c)] = value
        self.grid = shifted
        self.inserts.append((row, count))
        if self.fail_insert == "after":
            raise WpsCloudError("模拟插入接口超时（可能已执行）")

    def delete_rows(self, file_id, worksheet_id, *, row, count):
        if self.fail_delete:
            raise WpsCloudError("模拟回滚删除失败")
        self.deleted_rows.append((row, count))
        lo, hi = row - 1, row - 1 + count - 1
        shifted = {}
        for (r, c), value in self.grid.items():
            if r < lo:
                shifted[(r, c)] = value
            elif r > hi:
                shifted[(r - count, c)] = value
        self.grid = shifted

    @staticmethod
    def _col_index(letter: str) -> int:
        value = 0
        for char in str(letter).upper():
            value = value * 26 + (ord(char) - 64)
        return value - 1

    def sort_range(self, file_id, worksheet_id, *, range_ref, key, order="asc",
                   header=False, key2=None, order2=None):
        if self.fail_sort == "before":
            raise WpsCloudError("模拟排序接口失败（可能未执行）")
        import re as _re
        match = _re.match(r"^([A-Z]+)(\d+):([A-Z]+)(\d+)$", str(range_ref))
        assert match, f"排序区域格式非法：{range_ref}"
        c0 = self._col_index(match.group(1))
        r0 = int(match.group(2))
        c1 = self._col_index(match.group(3))
        r1 = int(match.group(4))
        first = r0 - 1 + (1 if header else 0)
        last = r1 - 1
        key_col = self._col_index(key)

        def sort_key(row: int):
            raw = str(self.grid.get((row, key_col), "") or "").strip()
            return (1, "") if not raw else (0, raw)

        reverse = str(order).lower() == "desc"
        rows = sorted(range(first, last + 1), key=sort_key, reverse=reverse)
        block = {(r, c): v for (r, c), v in self.grid.items()
                 if first <= r <= last and c0 <= c <= c1}
        for r in range(first, last + 1):
            for c in range(c0, c1 + 1):
                self.grid.pop((r, c), None)
        for offset, src in enumerate(rows):
            for c in range(c0, c1 + 1):
                if (src, c) in block:
                    self.grid[(first + offset, c)] = block[(src, c)]
        self.sorts.append({"range": range_ref, "key": key, "order": order})
        if self.fail_sort == "after":
            raise WpsCloudError("模拟排序接口返回超时（可能已执行）")

    def delete_columns(self, file_id, worksheet_id, *, column, rows):
        self.deleted_columns.append((column, rows))
        col = column - 1
        shifted = {}
        for (r, c), value in self.grid.items():
            if c == col:
                continue
            shifted[(r, c - 1 if c > col else c)] = value
        self.grid = shifted

    def write_format_ops(self, file_id, worksheet_id, ops):
        return None

    def read_cell_format(self, file_id, worksheet_id, row, col):
        return None


class RouterCli:
    """把 ``file_id`` 路由到各自的内存表，用于多表部分失败测试。"""

    def __init__(self, grids):
        self.grids = dict(grids)

    def __getattr__(self, name):
        def call(file_id, *args, **kwargs):
            return getattr(self.grids[file_id], name)(file_id, *args, **kwargs)
        return call


def _orders(*meals, name="张", phone="111", address="小"):
    return [CloudOrder("东湖中餐", name, address, phone, "中餐", "经济", meal)
            for meal in meals]


def _plan(cli, orders, ledger, *, sort=False, address_order=None, target=TARGET):
    return build_plan(
        cli, local_orders={"东湖中餐": orders},
        tables={"东湖中餐": {"file_id": "F1"}},
        target=target, ledger=ledger,
        address_order=address_order or {}, sort_enabled=sort)


def _names(cli):
    return sorted((key[1], key[0]) for key, value in cli.grid.items() if key[1] == 7)


def _new_row_count(cli):
    return sum(1 for key, value in cli.grid.items() if key[1] == 0 and str(value).startswith("新人"))


# ----------------------------------------------------------------------
# 意图日志与零写前置
# ----------------------------------------------------------------------

def test_journal_intent_is_persisted_before_first_cloud_write(tmp_path):
    ledger_path = tmp_path / "state.json"
    journal_path = journal_path_for(ledger_path)

    class InspectCli(GridCli):
        def write_cells(self, file_id, worksheet_id, cells):
            assert journal_path.exists(), "第一笔云端写入前日志必须已落盘"
            payload = json.loads(journal_path.read_text(encoding="utf-8"))
            op = next(iter(payload["operations"].values()))
            assert op["status"] in ("planned", "writing")
            record = next(iter(op["sheets"].values()))
            assert record["status"] in ("planned", "writing")
            intent = record["intents"][0]
            assert intent["name"] == "张" and intent["phone_key"] == "111"
            assert intent["slot"] == 1 and intent["target_expected"] == "1"
            return super().write_cells(file_id, worksheet_id, cells)

    cli = InspectCli(make_grid(rows=[{0: "张", 2: "111", 7: "3"}]))
    ledger = SyncLedger(ledger_path)
    result = apply_plan(cli, _plan(cli, _orders(6), ledger), ledger=ledger,
                        marker_enabled=False)

    assert result["status"] == "ok"
    assert result["operation_id"]
    assert result["journal_path"] == str(journal_path)
    assert result["summary"]["written"] == 1


def test_journal_save_failure_makes_zero_cloud_write(tmp_path):
    cli = GridCli(make_grid(rows=[{0: "张", 2: "111", 7: "3"}]))
    ledger = SyncLedger(tmp_path / "state.json")

    with mock.patch.object(SyncJournal, "save", side_effect=OSError("disk read-only")):
        result = apply_plan(cli, _plan(cli, _orders(6), ledger), ledger=ledger,
                            marker_enabled=False)

    assert result["uncertain"] is True
    assert result["status"] == "uncertain"
    assert result["next_action"] == "fix_journal"
    assert result["sheets"][0]["status"] == "uncertain"
    assert result["sheets"][0]["next_action"] == "fix_journal"
    assert cli.writes == [] and cli.inserts == []
    assert cli.grid[(2, 7)] == "3"


def test_corrupt_journal_blocks_all_writes(tmp_path):
    ledger = SyncLedger(tmp_path / "state.json")
    journal_path = journal_path_for(ledger.path)
    journal_path.write_text("{ broken", encoding="utf-8")
    cli = GridCli(make_grid(rows=[{0: "张", 2: "111", 7: "3"}]))

    result = apply_plan(cli, _plan(cli, _orders(6), ledger), ledger=ledger,
                        marker_enabled=False)

    assert result["uncertain"] is True
    assert result["status"] == "uncertain"
    assert result["next_action"] == "fix_journal"
    assert result["sheets"][0]["status"] == "uncertain"
    assert cli.writes == [] and cli.inserts == []
    assert cli.grid[(2, 7)] == "3"


# ----------------------------------------------------------------------
# 崩溃/部分写/超时/账本失败恢复（WPS-C1/C2/C3/C4/C6）
# ----------------------------------------------------------------------

def test_partial_write_is_uncertain_then_restart_blocks_without_duplicate(tmp_path):
    cli = GridCli(make_grid(rows=[{0: "张", 2: "111", 7: "3"}]),
                  fail_write_on_call=1, fail_write_after_cells=1)
    ledger_path = tmp_path / "state.json"
    ledger = SyncLedger(ledger_path)

    first = apply_plan(cli, _plan(cli, _orders(6), ledger), ledger=ledger,
                       marker_enabled=False)
    assert first["status"] == "uncertain"
    assert first["uncertain"] is True
    assert first["sheets"][0]["next_action"] == "manual_reconcile"

    before = dict(cli.grid)
    cli.fail_write_on_call = 0
    cli.fail_write_after_cells = 0
    ledger2 = SyncLedger(ledger_path)
    second = apply_plan(cli, _plan(cli, _orders(6), ledger2), ledger=ledger2,
                        marker_enabled=False)

    assert second["status"] == "uncertain"
    assert second["uncertain"] is True
    assert cli.grid == before, "恢复对账不确定时不得再写一个格子"
    assert ledger2.synced_slots(TARGET.isoformat(), "F1", "张", "111") is None


def test_formula_read_failure_recovers_and_backfills_ledger(tmp_path):
    cli = GridCli(make_grid(rows=[{0: "张", 2: "111", 7: "3"}]),
                  fail_formula_reads=1)
    ledger_path = tmp_path / "state.json"
    ledger = SyncLedger(ledger_path)

    first = apply_plan(cli, _plan(cli, _orders(6), ledger), ledger=ledger,
                       marker_enabled=False)
    assert first["status"] == "uncertain"
    assert cli.grid[(2, 7)] == "9"

    ledger2 = SyncLedger(ledger_path)
    second = apply_plan(cli, _plan(cli, _orders(6), ledger2), ledger=ledger2,
                        marker_enabled=False)
    assert second["status"] == "recovered"
    assert second["uncertain"] is False
    assert ledger2.synced_slots(TARGET.isoformat(), "F1", "张", "111") == [6]

    ledger3 = SyncLedger(ledger_path)
    before = dict(cli.grid)
    third = apply_plan(cli, _plan(cli, _orders(6), ledger3), ledger=ledger3,
                       marker_enabled=False)
    assert third["status"] == "ok"
    assert cli.grid == before, "恢复补账后再次上传必须零重复写"


def test_stale_verification_read_uncertain_then_cloud_recovery_marks_ok(tmp_path):
    cli = GridCli(make_grid(rows=[{0: "张", 2: "111", 7: "3"}]),
                  stale_reads_after_write=1)
    ledger_path = tmp_path / "state.json"
    ledger = SyncLedger(ledger_path)

    first = apply_plan(cli, _plan(cli, _orders(6), ledger), ledger=ledger,
                       marker_enabled=False)
    assert first["status"] == "uncertain"
    assert cli.grid[(2, 7)] == "9", "实际云端已写入，只是首次回读拿到旧快照"

    ledger2 = SyncLedger(ledger_path)
    second = apply_plan(cli, _plan(cli, _orders(6), ledger2), ledger=ledger2,
                        marker_enabled=False)
    assert second["status"] == "recovered"
    assert ledger2.synced_slots(TARGET.isoformat(), "F1", "张", "111") == [6]
    assert cli.grid[(2, 7)] == "9"


def test_ledger_save_failure_uncertain_and_recovery_backfills(tmp_path):
    cli = GridCli(make_grid(rows=[{0: "张", 2: "111", 7: "3"}]))
    ledger_path = tmp_path / "state.json"
    ledger = SyncLedger(ledger_path)

    def broken_save():
        raise OSError("disk full")

    ledger.save = broken_save  # type: ignore[method-assign]
    first = apply_plan(cli, _plan(cli, _orders(6), ledger), ledger=ledger,
                       marker_enabled=False)

    assert first["status"] == "uncertain"
    assert first["sheets"][0]["status"] == "uncertain"
    assert "ledger_save_failed" in first["sheets"][0]["reason"]

    ledger2 = SyncLedger(ledger_path)
    second = apply_plan(cli, _plan(cli, _orders(6), ledger2), ledger=ledger2,
                        marker_enabled=False)
    assert second["status"] == "recovered"
    assert ledger2.synced_slots(TARGET.isoformat(), "F1", "张", "111") == [6]
    assert cli.grid[(2, 7)] == "9", "整个恢复过程不得再加餐"


def test_crash_after_cloud_write_before_ledger_recovers_without_double(tmp_path):
    cli = GridCli(make_grid(rows=[{0: "张", 2: "111", 7: "3"}]),
                  crash_on_write_call=1)
    ledger_path = tmp_path / "state.json"
    ledger = SyncLedger(ledger_path)
    plan = _plan(cli, _orders(6), ledger)

    with pytest.raises(KeyboardInterrupt):
        apply_plan(cli, plan, ledger=ledger, marker_enabled=False)

    journal_path = journal_path_for(ledger_path)
    payload = json.loads(journal_path.read_text(encoding="utf-8"))
    op = next(iter(payload["operations"].values()))
    assert op["status"] in ("writing", "ledger_pending")
    assert cli.grid[(2, 7)] == "9"

    cli.crash_on_write_call = 0
    ledger2 = SyncLedger(ledger_path)
    recovered = apply_plan(cli, _plan(cli, _orders(6), ledger2), ledger=ledger2,
                           marker_enabled=False)
    assert recovered["status"] == "recovered"
    assert ledger2.synced_slots(TARGET.isoformat(), "F1", "张", "111") == [6]
    assert cli.grid[(2, 7)] == "9"


def test_insert_failure_after_shift_is_manual_blocked_not_retried(tmp_path):
    cli = GridCli(make_grid(rows=[{0: "老客户", 2: "111", 7: "3"}]),
                  fail_insert="after")
    ledger_path = tmp_path / "state.json"
    ledger = SyncLedger(ledger_path)
    orders = [CloudOrder("东湖中餐", "新人", "小", "999", "中餐", "经济", 1)]
    first = apply_plan(cli, _plan(cli, orders, ledger), ledger=ledger,
                       marker_enabled=False)

    assert first["status"] == "uncertain"
    assert first["sheets"][0].get("manual_required") == "insert_result_unknown"

    ledger2 = SyncLedger(ledger_path)
    writes_before = len(cli.writes)
    second = apply_plan(cli, _plan(cli, orders, ledger2), ledger=ledger2,
                        marker_enabled=False)
    assert second["status"] == "uncertain"
    assert second["recovery"]["status"] == "uncertain"
    assert len(cli.writes) == writes_before, "人工核对前不得自动追加"


def test_rollback_delete_failure_manual_blocked(tmp_path):
    cli = GridCli(make_grid(rows=[{0: "老客户", 2: "111", 7: "3"}]),
                  fail_write_on_call=1, fail_delete=True)
    ledger_path = tmp_path / "state.json"
    ledger = SyncLedger(ledger_path)
    orders = [CloudOrder("东湖中餐", "新人", "小", "999", "中餐", "经济", 1)]
    first = apply_plan(cli, _plan(cli, orders, ledger), ledger=ledger,
                       marker_enabled=False)

    assert first["status"] == "uncertain"
    assert first["sheets"][0].get("manual_required") == "rollback_delete_failed"

    ledger2 = SyncLedger(ledger_path)
    writes_before = len(cli.writes)
    second = apply_plan(cli, _plan(cli, orders, ledger2), ledger=ledger2,
                        marker_enabled=False)
    assert second["status"] == "uncertain"
    assert second["uncertain"] is True
    assert len(cli.writes) == writes_before, "回滚失败后绝不能自动重试"


# ----------------------------------------------------------------------
# 多槽位、排序后身份与确定性槽位错位（WPS-C7/C8）
# ----------------------------------------------------------------------

def test_multi_slot_pending_does_not_truncate_ledger(tmp_path):
    cli = GridCli(make_grid(rows=[{0: "柟", 2: "111", 7: "5"}]))
    ledger_path = tmp_path / "state.json"

    expected_slots = ([1], [1, 1], [1, 1], [1, 1])
    for index, (orders, expected) in enumerate(zip((
            _orders(1, name="柟"),
            _orders(1, 1, name="柟"),
            _orders(1, 1, name="柟"),
            _orders(1, 1, name="柟"),
    ), expected_slots)):
        ledger = SyncLedger(ledger_path)
        result = apply_plan(cli, _plan(cli, orders, ledger), ledger=ledger,
                            marker_enabled=False)
        assert result["status"] == "ok", result
        slots = SyncLedger(ledger_path).synced_slots(
            TARGET.isoformat(), "F1", "柟", "111")
        assert slots == expected, f"第 {index + 1} 次上传后 slots={slots}"

    rows_total = sum(int(value) for (row0, col), value in cli.grid.items()
                     if col == 7 and row0 >= 2 and str(value).isdigit())
    assert rows_total == 7, f"云端总餐次应为 5+1+1=7，实际 {rows_total}"


def test_sort_identity_assigns_new_slot_to_new_row_not_old_total(tmp_path):
    cli = GridCli(make_grid(rows=[
        {0: "柟", 1: "D2", 2: "111", 7: "5"},
        {0: "李", 1: "小", 2: "222", 7: "3"},
    ]))
    ledger_path = tmp_path / "state.json"
    address_order = {"东湖中餐": ["小", "D2"]}

    def run(orders):
        ledger = SyncLedger(ledger_path)
        plan = _plan(cli, orders, ledger, sort=True, address_order=address_order)
        result = apply_plan(cli, plan, ledger=ledger, marker_enabled=False)
        assert result["status"] == "ok", result
        return plan, SyncLedger(ledger_path)

    run(_orders(1, name="柟"))
    run(_orders(1, 1, name="柟"))

    nan_rows = sorted((row0 + 1, str(value))
                      for (row0, col), value in cli.grid.items()
                      if col == 0 and str(value) == "柟")
    values = [(name, cli.grid.get((row - 1, 7)), cli.grid.get((row - 1, 4)))
              for row, name in nan_rows]
    assert values == [("柟", "1", "1"), ("柟", "6", "1")], values
    assert SyncLedger(ledger_path).synced_slots(
        TARGET.isoformat(), "F1", "柟", "111") == [1, 1]
    # 老行不能被新行覆盖
    totals = sorted(int(cli.grid.get((row - 1, 7))) for row, name in nan_rows)
    assert totals == [1, 6]


# ----------------------------------------------------------------------
# 账本损坏与迁移
# ----------------------------------------------------------------------

def test_corrupt_ledger_is_rejected_not_treated_as_empty(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{ broken", encoding="utf-8")
    with pytest.raises(LedgerCorruptError):
        SyncLedger(path)


def test_legacy_ledger_meals_still_migrates(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({
        "version": 1,
        "batches": {TARGET.isoformat(): {"F1": {
            "synced_at": "2026-09-11T21:00:00",
            "people": {"张\u0000111": {"meals": 6}},
        }}},
    }, ensure_ascii=False), encoding="utf-8")
    cli = GridCli(make_grid(rows=[{
        0: "张", 2: "111", 4: "1", 5: "中餐", 6: "经济", 7: "6",
        8: "=SUM(D3:E3)", 9: "=H3-I3",
    }]))
    ledger = SyncLedger(path)
    plan = _plan(cli, _orders(6), ledger)
    change = plan[0].changes[0]
    assert change.ledger_prev == 6
    assert change.needs_write is False


# ----------------------------------------------------------------------
# API 契约
# ----------------------------------------------------------------------

def test_apply_plan_contract_fields_and_uncertain_not_success(tmp_path):
    cli = GridCli(make_grid(rows=[{0: "张", 2: "111", 7: "3"}]))
    ledger_path = tmp_path / "state.json"
    ledger = SyncLedger(ledger_path)
    result = apply_plan(cli, _plan(cli, _orders(6), ledger), ledger=ledger,
                        marker_enabled=False)

    for key in ("status", "uncertain", "next_action", "operation_id",
                "summary", "journal_path", "written", "failed", "sheets"):
        assert key in result
    assert result["status"] == "ok" and result["uncertain"] is False
    assert result["written"] == 1 and result["failed"] == 0
    assert result["sheets"][0]["status"] == "ok"

    # 人为制造“可能已写但未确认”：必须是 uncertain 而不是 success
    cli2 = GridCli(make_grid(rows=[{0: "张", 2: "111", 7: "3"}]),
                   fail_write_on_call=1, fail_write_after_cells=1)
    ledger2 = SyncLedger(tmp_path / "state2.json")
    result2 = apply_plan(cli2, _plan(cli2, _orders(6), ledger2), ledger=ledger2,
                         marker_enabled=False)
    assert result2["uncertain"] is True
    assert result2["status"] == "uncertain"
    assert result2["failed"] > 0
    assert result2["sheets"][0]["status"] == "uncertain"
    assert result2["sheets"][0]["next_action"] == "manual_reconcile"

def test_recovery_status_and_manual_resolution_route(tmp_path):
    """A/Bridge 可查询的恢复入口：人工确认云端完整后可补账本并解阻。"""
    cli = GridCli(make_grid(rows=[{0: "张", 2: "111", 7: "3"}]))
    ledger_path = tmp_path / "state.json"
    ledger = SyncLedger(ledger_path)

    def broken_save():
        raise OSError("disk full")

    ledger.save = broken_save  # type: ignore[method-assign]
    first = apply_plan(cli, _plan(cli, _orders(6), ledger), ledger=ledger,
                       marker_enabled=False)
    assert first["status"] == "uncertain"

    ledger2 = SyncLedger(ledger_path)
    status = recovery_status(ledger2)
    assert status["ok"] is True
    assert len(status["pending_operations"]) == 1
    operation_id = status["pending_operations"][0]["operation_id"]
    assert status["pending_operations"][0]["sheets"][0]["status"] == "ledger_pending"
    assert status["counts"]["ledger_pending"] == 1

    resolved = resolve_pending_operation(ledger2, operation_id, "cloud_verified",
                                         cli=cli, note="人工核对：云端完整")
    assert resolved["ok"] is True
    assert resolved["status"] == "resolved"
    assert ledger2.synced_slots(TARGET.isoformat(), "F1", "张", "111") == [6]
    after = recovery_status(ledger2)
    assert after["pending_operations"] == []
    assert after["counts"]["verified"] == 1


def test_manual_untouched_resolution_requires_structure_confirmation(tmp_path):
    cli = GridCli(make_grid(rows=[{0: "老客户", 2: "111", 7: "3"}]),
                  fail_write_on_call=1, fail_delete=True)
    ledger_path = tmp_path / "state.json"
    ledger = SyncLedger(ledger_path)
    orders = [CloudOrder("东湖中餐", "新人", "小", "999", "中餐", "经济", 1)]
    first = apply_plan(cli, _plan(cli, orders, ledger), ledger=ledger,
                       marker_enabled=False)
    assert first["sheets"][0].get("manual_required") == "rollback_delete_failed"
    operation_id = recovery_status(ledger)["operations"][0]["operation_id"]

    refused = resolve_pending_operation(ledger, operation_id, "cloud_untouched",
                                        cli=cli)
    assert refused["ok"] is False

    confirmed = resolve_pending_operation(
        ledger, operation_id, "cloud_untouched", cli=cli,
        confirm_structure_checked=True, note="人工检查：云端无残留")
    assert confirmed["ok"] is True
    assert confirmed["next_action"] == "repreview"
    status = recovery_status(ledger)
    assert status["pending_operations"] == []
    assert status["counts"]["not_started"] == 1

def test_crash_after_second_slot_write_merges_ledger_without_conflict(tmp_path):
    """WPS-C7 恢复：账本旧锚点 [1]，新日志期望 [1,1] 时必须补全而非报冲突。"""
    cli = GridCli(make_grid(rows=[{0: "柟", 2: "111", 7: "5"}]))
    ledger_path = tmp_path / "state.json"

    first_ledger = SyncLedger(ledger_path)
    first = apply_plan(cli, _plan(cli, _orders(1, name="柟"), first_ledger),
                       ledger=first_ledger, marker_enabled=False)
    assert first["status"] == "ok"

    cli.write_call = 0
    cli.crash_on_write_call = 2       # 第二轮最终单元格已写，进程在记账前被杀
    second_ledger = SyncLedger(ledger_path)
    with pytest.raises(KeyboardInterrupt):
        apply_plan(cli, _plan(cli, _orders(1, 1, name="柟"), second_ledger),
                   ledger=second_ledger, marker_enabled=False)

    cli.crash_on_write_call = 0
    third_ledger = SyncLedger(ledger_path)
    recovered = apply_plan(cli, _plan(cli, _orders(1, 1, name="柟"), third_ledger),
                           ledger=third_ledger, marker_enabled=False)
    assert recovered["status"] == "recovered"
    assert third_ledger.synced_slots(TARGET.isoformat(), "F1", "柟", "111") == [1, 1]
    totals = sorted(int(v) for (r0, c), v in cli.grid.items()
                    if c == 7 and r0 >= 2 and str(v).isdigit())
    assert totals == [1, 6], totals

def test_stale_old_value_before_execution_blocks_with_zero_writes(tmp_path):
    """临执行前复核旧值：协作者改了 total/target，必须零写入退回重新预览。"""
    cli = GridCli(make_grid(rows=[{0: "张", 2: "111", 7: "3"}]))
    ledger = SyncLedger(tmp_path / "state.json")
    plan = _plan(cli, _orders(6), ledger)

    cli.grid[(2, 7)] = "4"       # 计划后总餐次被协作者改成 4
    before = dict(cli.grid)
    result = apply_plan(cli, plan, ledger=ledger, marker_enabled=False)

    assert result["status"] == "failed"
    assert result["uncertain"] is False
    assert result["next_action"] == "repreview"
    assert cli.grid == before, "旧值变化后一个格子都不能写"
    assert ledger.synced_slots(TARGET.isoformat(), "F1", "张", "111") is None
    assert "总餐次" in "；".join(result["sheets"][0]["problems"])


def test_stale_target_cell_blocked_zero_writes(tmp_path):
    """协作者把目标日期格从 0 改成 1（明确要送）也必须重新预览，不能覆盖。"""
    cli = GridCli(make_grid(rows=[{0: "张", 2: "111", 4: "0", 7: "3"}]))
    ledger = SyncLedger(tmp_path / "state.json")
    plan = _plan(cli, _orders(6), ledger)
    cli.grid[(2, 4)] = "1"
    before = dict(cli.grid)

    result = apply_plan(cli, plan, ledger=ledger, marker_enabled=False)

    assert result["status"] == "failed"
    assert result["next_action"] == "repreview"
    assert cli.grid == before
    assert ledger.synced_slots(TARGET.isoformat(), "F1", "张", "111") is None

def test_target_zero_preserved_through_crash_recovery(tmp_path):
    """协作者写的 0（当天不送）在崩溃恢复后也必须原样保留，不得改成 1。"""
    cli = GridCli(make_grid(rows=[{
        0: "张", 2: "111", 4: "0", 5: "中餐", 6: "经济", 7: "3",
        8: "=SUM(D3:E3)", 9: "=H3-I3",
    }]), crash_on_write_call=1)
    ledger_path = tmp_path / "state.json"
    ledger = SyncLedger(ledger_path)
    plan = _plan(cli, _orders(6), ledger)

    with pytest.raises(KeyboardInterrupt):
        apply_plan(cli, plan, ledger=ledger, marker_enabled=False)

    assert cli.grid[(2, 4)] == "0"
    assert cli.grid[(2, 7)] == "9"

    cli.crash_on_write_call = 0
    ledger2 = SyncLedger(ledger_path)
    recovered = apply_plan(cli, _plan(cli, _orders(6), ledger2), ledger=ledger2,
                           marker_enabled=False)

    assert recovered["status"] == "recovered"
    assert cli.grid[(2, 4)] == "0", "协作者的 0 必须保留"
    assert cli.grid[(2, 7)] == "9"
    assert ledger2.synced_slots(TARGET.isoformat(), "F1", "张", "111") == [6]

def test_multi_sheet_partial_failure_blocks_remaining_sheet(tmp_path):
    """一张表写入结果不确定后，后续表必须阻断，不能继续扩大部分成功。"""
    grid_a = make_grid(rows=[{0: "张", 2: "111", 7: "3"}])
    grid_b = make_grid(rows=[{0: "李", 2: "222", 7: "5"}])
    cli_a = GridCli(grid_a, fail_write_on_call=2, fail_write_after_cells=1)
    cli_b = GridCli(grid_b)
    router = RouterCli({"F1": cli_a, "F2": cli_b})
    ledger = SyncLedger(tmp_path / "state.json")

    plans = build_plan(
        router,
        local_orders={
            "东湖中餐": _orders(6, name="新人A", phone="111"),
            "衣锦中餐": _orders(2, name="李", phone="222"),
        },
        tables={"东湖中餐": {"file_id": "F1"},
                "衣锦中餐": {"file_id": "F2"}},
        target=TARGET, ledger=ledger, address_order={}, sort_enabled=False,
    )
    # F1 新客户 base_cells 是第 1 次写，最终单元格是第 2 次；让其部分写后失败。
    result = apply_plan(router, plans, ledger=ledger, marker_enabled=False)

    assert result["status"] == "uncertain"
    assert result["uncertain"] is True
    assert result["written"] == 0
    a_item = next(item for item in result["sheets"] if item["sheet"] == "东湖中餐")
    b_item = next(item for item in result["sheets"] if item["sheet"] == "衣锦中餐")
    assert a_item["status"] == "uncertain"
    assert b_item["status"] == "blocked"
    assert b_item["next_action"] == "manual_reconcile"
    assert cli_b.writes == [] and cli_b.inserts == [], "后续表一个云端写请求都不能发"
    assert cli_b.grid[(2, 7)] == "5"
    assert ledger.synced_slots(TARGET.isoformat(), "F1", "新人A", "111") is None
    assert ledger.synced_slots(TARGET.isoformat(), "F2", "李", "222") is None

def test_sort_slot_identity_recovery_after_crash(tmp_path):
    """C8 崩溃恢复：新槽位排序到旧行前面，靠实际地址/身份重建而不是行号升序。"""
    cli = GridCli(make_grid(rows=[
        {0: "柟", 1: "D2", 2: "111", 7: "5"},
        {0: "李", 1: "小", 2: "222", 7: "3"},
    ]))
    address_order = {"东湖中餐": ["小", "D2"]}
    ledger_path = tmp_path / "state.json"

    first_ledger = SyncLedger(ledger_path)
    first = apply_plan(
        cli, _plan(cli, _orders(1, name="柟"), first_ledger,
                   sort=True, address_order=address_order),
        ledger=first_ledger, marker_enabled=False)
    assert first["status"] == "ok"

    cli.write_call = 0
    cli.crash_on_write_call = 3         # 第二轮最终单元格已写，进程在记账前被杀
    second_ledger = SyncLedger(ledger_path)
    with pytest.raises(KeyboardInterrupt):
        apply_plan(
            cli, _plan(cli, _orders(1, 1, name="柟"), second_ledger,
                       sort=True, address_order=address_order),
            ledger=second_ledger, marker_enabled=False)

    cli.crash_on_write_call = 0
    third_ledger = SyncLedger(ledger_path)
    recovered = apply_plan(
        cli, _plan(cli, _orders(1, 1, name="柟"), third_ledger,
                   sort=True, address_order=address_order),
        ledger=third_ledger, marker_enabled=False)

    assert recovered["status"] == "recovered"
    assert third_ledger.synced_slots(TARGET.isoformat(), "F1", "柟", "111") == [1, 1]
    totals = sorted(int(v) for (r0, c), v in cli.grid.items()
                    if c == 7 and r0 >= 2 and str(v).isdigit())
    assert totals == [1, 3, 6], totals   # 新 slot1=1, 李=3, 老槽位=6

def test_format_write_failure_is_uncertain_and_blocks_retry(tmp_path):
    """格式写接口报错/超时后可能已部分生效：必须 uncertain，不能按成功继续。"""
    class FormatBoom(GridCli):
        def write_format_ops(self, file_id, worksheet_id, ops):
            raise WpsCloudError("模拟格式写接口超时")

    cli = FormatBoom(make_grid(rows=[{0: "老客户", 2: "111", 7: "3"}]))
    ledger_path = tmp_path / "state.json"
    ledger = SyncLedger(ledger_path)
    orders = [CloudOrder("东湖中餐", "新人", "小", "999", "中餐", "经济", 1)]
    fake_spec = {"alcH": 2, "alcV": 1, "font_name": "",
                 "fill_default": 0xFFFFFFFF, "fills": {}}
    with mock.patch("app.wps.executor.learn_row_format", return_value=fake_spec):
        first = apply_plan(cli, _plan(cli, orders, ledger), ledger=ledger,
                           marker_enabled=False)

    assert first["status"] == "uncertain"
    assert first["uncertain"] is True
    assert first["sheets"][0]["next_action"] == "manual_reconcile"

    ledger2 = SyncLedger(ledger_path)
    writes_before = len(cli.writes)
    second = apply_plan(cli, _plan(cli, orders, ledger2), ledger=ledger2,
                        marker_enabled=False)
    assert second["status"] == "uncertain"
    assert len(cli.writes) == writes_before, "格式未确认后不得自动重试"

def test_ledger_entry_without_anchor_is_rejected(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({
        "version": 1,
        "batches": {TARGET.isoformat(): {"F1": {
            "synced_at": "", "people": {"张\u0000111": {"unexpected": 1}},
        }}},
    }), encoding="utf-8")
    with pytest.raises(LedgerCorruptError):
        SyncLedger(path)

# ----------------------------------------------------------------------
# P1：原子写盘、父目录 fsync、跨进程锁/合并
# ----------------------------------------------------------------------

def _ledger_merge_worker(path: str, prefix: str, count: int) -> None:
    ledger = SyncLedger(path)
    for index in range(count):
        key = f"{prefix}{index}{chr(0)}p{prefix}{index}"
        ledger.merge_entries(TARGET.isoformat(), "F1", {
            key: {"slots": [1], "local": 1, "total": 1},
        })


def _ledger_record_save_worker(path: str, prefix: str, count: int) -> None:
    """模拟旧式/直接调用：每次重新载入账本、record 后 save，不显式用 merge_entries。"""
    key = f"{prefix}direct{chr(0)}p{prefix}direct"
    for _ in range(count):
        ledger = SyncLedger(path)
        ledger.record(TARGET.isoformat(), "F1", {
            key: {"slots": [1], "local": 1, "total": 1},
        })
        ledger.save()


def _journal_save_worker(path: str, operation_id: str) -> None:
    journal = SyncJournal(path)
    journal.create_operation(operation_id, {
        f"sheet:{operation_id}": {
            "sheet": "东湖中餐", "file_id": "F1",
            "status": "writing", "next_action": "recover_journal",
        },
    }, target_date=TARGET.isoformat())
    journal.save()


def _write_pending_journal_worker(journal_path: str, record_path: str,
                                   operation_id: str) -> None:
    record = json.loads(Path(record_path).read_text(encoding="utf-8"))
    journal = SyncJournal(journal_path)
    journal.create_operation(operation_id,
                             {"0:东湖中餐:F1": record},
                             target_date=TARGET.isoformat())
    journal.save()


def _lock_waiter(path: str, queue) -> None:
    try:
        lock = FileLock(path, timeout=0.10)
        lock.acquire()
        try:
            queue.put("acquired")
        finally:
            lock.release()
    except LockTimeout:
        queue.put("timeout")


def _ledger_reader_worker(path: str, queue, rounds: int) -> None:
    try:
        for _ in range(rounds):
            ledger = SyncLedger(path)
            assert isinstance(ledger.data, dict)
        queue.put("ok")
    except Exception as exc:  # pragma: no cover - 失败时把异常回传父进程
        queue.put(f"{type(exc).__name__}: {exc}")


def test_atomic_write_fsyncs_file_and_parent_dir(tmp_path):
    real_fsync = os.fsync
    calls: list[int] = []

    def spy(fd):
        calls.append(int(fd))
        return real_fsync(fd)

    target = tmp_path / "state.json"
    target.write_text("old", encoding="utf-8")
    with mock.patch.object(os, "fsync", side_effect=spy):
        atomic_write_text(target, "new")

    assert target.read_text(encoding="utf-8") == "new"
    assert len(calls) >= 2, "文件 fsync + 父目录 fsync 都不得省略"
    assert list(tmp_path.glob(".*.tmp")) == []


def test_atomic_write_interruption_leaves_old_file_intact(tmp_path):
    target = tmp_path / "state.json"
    target.write_text("old", encoding="utf-8")

    with mock.patch.object(os, "fsync", side_effect=OSError("模拟写盘中断")):
        with pytest.raises(OSError):
            atomic_write_text(target, "new")

    assert target.read_text(encoding="utf-8") == "old"
    assert list(tmp_path.glob(".*.tmp")) == []


def test_atomic_write_parent_dir_fsync_failure_raises(tmp_path):
    real_fsync = os.fsync
    calls = {"count": 0}

    def flaky(fd):
        calls["count"] += 1
        if calls["count"] == 1:
            return real_fsync(fd)
        raise OSError("模拟父目录 fsync 失败")

    target = tmp_path / "state.json"
    with mock.patch.object(os, "fsync", side_effect=flaky):
        with pytest.raises(OSError):
            atomic_write_text(target, "new")

    # replace 已发生，但目录持久化失败；必须向上抛让调用方 failure close。
    assert target.read_text(encoding="utf-8") == "new"
    assert list(tmp_path.glob(".*.tmp")) == []


def test_atomic_write_temp_creation_failure_zero_file_change(tmp_path, monkeypatch):
    target = tmp_path / "state.json"
    target.write_text("old", encoding="utf-8")

    def boom(*_args, **_kwargs):
        raise PermissionError("模拟目录不可写")

    monkeypatch.setattr(atomicio.tempfile, "mkstemp", boom)
    with pytest.raises(PermissionError):
        atomic_write_text(target, "new")

    assert target.read_text(encoding="utf-8") == "old"
    assert list(tmp_path.glob(".*.tmp")) == []


def test_ledger_save_failure_keeps_previous_disk_state(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    baseline = SyncLedger(path)
    baseline.merge_entries(TARGET.isoformat(), "F1", {
        "旧批次" + chr(0) + "111": {"slots": [1], "local": 1, "total": 1},
    })

    ledger = SyncLedger(path)
    ledger.record(TARGET.isoformat(), "F1", {
        "新批次" + chr(0) + "222": {"slots": [1], "local": 1, "total": 1},
    })

    def boom(*_args, **_kwargs):
        raise PermissionError("模拟账本目录不可写")

    monkeypatch.setattr(atomicio.tempfile, "mkstemp", boom)
    with pytest.raises(PermissionError):
        ledger.save()

    reloaded = SyncLedger(path)
    assert reloaded.synced_slots(TARGET.isoformat(), "F1", "旧批次", "111") == [1]
    assert reloaded.synced_slots(TARGET.isoformat(), "F1", "新批次", "222") is None
    assert list(tmp_path.glob(".*.tmp")) == []


def test_cross_process_ledger_merge_does_not_lose_batches(tmp_path):
    path = str(tmp_path / "state.json")
    ctx = _fork_ctx()
    workers = [
        ctx.Process(target=_ledger_merge_worker, args=(path, "A", 12)),
        ctx.Process(target=_ledger_merge_worker, args=(path, "B", 12)),
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(30)
        assert worker.exitcode == 0, f"worker exit={worker.exitcode}"

    ledger = SyncLedger(path)
    people = ledger.data["batches"][TARGET.isoformat()]["F1"]["people"]
    assert len(people) == 24, f"24 个不同批次键都应在磁盘上：{len(people)}"


def test_cross_process_same_slots_are_confirmed_only_once(tmp_path):
    path = str(tmp_path / "state.json")
    ctx = _fork_ctx()
    workers = [
        ctx.Process(target=_ledger_merge_worker, args=(path, "X", 6)),
        ctx.Process(target=_ledger_merge_worker, args=(path, "X", 6)),
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(30)
        assert worker.exitcode == 0

    ledger = SyncLedger(path)
    # 两个进程写的 key 完全相同；最终每个槽位仍只是 [1]，没有被重复确认成 [2]。
    people = ledger.data["batches"][TARGET.isoformat()]["F1"]["people"]
    assert len(people) == 6
    for entry in people.values():
        assert entry.get("slots") == [1], entry


def test_cross_process_journal_save_does_not_lose_operations(tmp_path):
    path = str(tmp_path / "state.journal")
    ctx = _fork_ctx()
    workers = [
        ctx.Process(target=_journal_save_worker, args=(path, "op-a")),
        ctx.Process(target=_journal_save_worker, args=(path, "op-b")),
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(30)
        assert worker.exitcode == 0

    journal = SyncJournal(path)
    assert set(journal.operations()) == {"op-a", "op-b"}


def test_cross_process_file_lock_times_out_while_held(tmp_path):
    lock_path = tmp_path / "state.json.lock"
    lock = FileLock(lock_path, timeout=1.0)
    lock.acquire()
    try:
        ctx = _fork_ctx()
        queue = ctx.Queue()
        worker = ctx.Process(target=_lock_waiter, args=(str(lock_path), queue))
        worker.start()
        assert queue.get(timeout=10) == "timeout"
        worker.join(10)
        assert worker.exitcode == 0
    finally:
        lock.release()


def test_reader_process_never_sees_torn_ledger_during_writes(tmp_path):
    path = str(tmp_path / "state.json")
    ctx = _fork_ctx()
    queue = ctx.Queue()
    writer = ctx.Process(target=_ledger_merge_worker, args=(path, "W", 30))
    reader = ctx.Process(target=_ledger_reader_worker, args=(path, queue, 60))
    writer.start()
    reader.start()
    writer.join(30)
    reader.join(30)
    assert writer.exitcode == 0
    assert reader.exitcode == 0
    result = queue.get(timeout=10)
    assert result == "ok", result

def test_recovery_status_distinguishes_planned_writing_verified_uncertain_failed(tmp_path):
    journal_path = tmp_path / "state.journal"
    journal = SyncJournal(journal_path)
    journal.create_operation("op-states", {
        "planned": {"sheet": "planned", "file_id": "F1", "status": "planned"},
        "writing": {"sheet": "writing", "file_id": "F1", "status": "writing"},
        "verified": {"sheet": "verified", "file_id": "F1", "status": "verified"},
        "uncertain": {"sheet": "uncertain", "file_id": "F1", "status": "uncertain"},
        "failed": {"sheet": "failed", "file_id": "F1", "status": "failed_no_write"},
    })
    for sheet_key, status in (("writing", "writing"), ("verified", "verified"),
                              ("uncertain", "uncertain"),
                              ("failed", "failed_no_write")):
        journal.set_sheet_status("op-states", sheet_key, status)
    journal.save()

    status = recovery_status(SyncLedger(tmp_path / "state.json"), journal=journal)
    assert status["ok"] is True
    assert status["counts"]["planned"] == 1
    assert status["counts"]["writing"] == 1
    assert status["counts"]["verified"] == 1
    assert status["counts"]["uncertain"] == 1
    assert status["counts"]["failed"] == 1
    assert len(status["pending_operations"]) == 1
    pending = status["pending_operations"][0]
    assert pending["pending"] is True
    assert {sheet["status"] for sheet in pending["sheets"]} == {
        "planned", "writing", "verified", "uncertain", "failed"}
    operation = status["operations"][0]
    assert any(sheet["status"] == "verified" and sheet["raw_status"] == "verified"
               for sheet in operation["sheets"])
    assert any(sheet["status"] == "failed" and sheet["raw_status"] == "failed_no_write"
               for sheet in operation["sheets"])
    assert status["next_action"] == "manual_reconcile"

class FileGridCli:
    """从 JSON 文件读云端网格，供跨进程恢复竞争测试共享同一远端状态。"""

    def __init__(self, path: str) -> None:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        self.grid = {tuple(int(part) for part in key.split(",")): str(value)
                     for key, value in raw.items()}

    def read_grid(self, file_id, worksheet_id, row_from, row_to, col_from, col_to,
                  *, with_format=False):
        return {key: value for key, value in self.grid.items()
                if row_from <= key[0] <= row_to and col_from <= key[1] <= col_to}

    def read_formulas(self, file_id, worksheet_id, row_from, row_to, col_from, col_to):
        return {key: value for key, value in self.grid.items()
                if row_from <= key[0] <= row_to and col_from <= key[1] <= col_to
                and str(value).startswith("=")}


def _hold_lock_until_worker(path: str, ready_queue, release_event) -> None:
    lock = FileLock(path, timeout=10.0)
    lock.acquire()
    try:
        ready_queue.put("held")
        release_event.wait(20)
    finally:
        lock.release()


def _recovery_race_worker(journal_path: str, ledger_path: str, cloud_path: str,
                          queue) -> None:
    from app.wps.journal import SyncJournal as _SyncJournal
    from app.wps.sync import SyncLedger as _SyncLedger
    from app.wps.sync import recover_pending_operations as _recover
    try:
        cli = FileGridCli(cloud_path)
        journal = _SyncJournal(journal_path)
        ledger = _SyncLedger(ledger_path)
        report = _recover(cli, journal, ledger=ledger)
        queue.put(report.get("status", "unknown"))
    except Exception as exc:  # pragma: no cover - 失败时回传
        queue.put(f"{type(exc).__name__}: {exc}")


def test_apply_plan_returns_uncertain_while_another_process_holds_lock(tmp_path, monkeypatch):
    import app.wps.executor as executor

    cli = GridCli(make_grid(rows=[{0: "张", 2: "111", 7: "3"}]))
    ledger = SyncLedger(tmp_path / "state.json")
    plan = _plan(cli, _orders(6), ledger)
    lock_path = executor.operation_lock_path_for(ledger.path)

    ctx = _fork_ctx()
    ready = ctx.Queue()
    release = ctx.Event()
    holder = ctx.Process(target=_hold_lock_until_worker, args=(str(lock_path), ready, release))
    holder.start()
    try:
        assert ready.get(timeout=10) == "held"
        monkeypatch.setattr(executor, "APPLY_PLAN_LOCK_TIMEOUT", 0.05)
        writes_before = len(cli.writes)
        result = apply_plan(cli, plan, ledger=ledger, marker_enabled=False)
        assert result["status"] == "uncertain"
        assert result["uncertain"] is True
        assert result["next_action"] == "wait_for_recovery_lock"
        assert result["written"] == 0 and result["failed"] > 0
        assert len(cli.writes) == writes_before
        assert cli.grid[(2, 7)] == "3"
    finally:
        release.set()
        holder.join(10)
        assert holder.exitcode == 0


def test_two_processes_recover_same_batch_without_double_confirmation(tmp_path):
    # 先造“旧计划”和日志记录，再把云端改成完整期望值，模拟写完但未记账。
    old_cli = GridCli(make_grid(rows=[{0: "张", 2: "111", 7: "3"}]))
    ledger_path = tmp_path / "state.json"
    ledger = SyncLedger(ledger_path)
    plan = _plan(old_cli, _orders(6), ledger)
    from app.wps.executor import _build_journal_record
    record = _build_journal_record(plan[0], 1, False, ledger)

    journal_path = tmp_path / "state.journal"
    journal = SyncJournal(journal_path)
    sheet_key = "0:东湖中餐:F1"
    journal.create_operation("op-race", {sheet_key: record})
    journal.set_sheet_status("op-race", sheet_key, "writing")
    journal.save()

    expected = make_grid(rows=[{
        0: "张", 2: "111", 4: "1", 5: "中餐", 6: "经济",
        7: "9", 8: "=SUM(D3:E3)", 9: "=H3-I3",
    }])
    cloud_path = tmp_path / "cloud.json"
    cloud_path.write_text(json.dumps(
        {f"{row},{col}": value for (row, col), value in expected.items()},
        ensure_ascii=False), encoding="utf-8")

    ctx = _fork_ctx()
    queue = ctx.Queue()
    workers = [
        ctx.Process(target=_recovery_race_worker,
                    args=(str(journal_path), str(ledger_path), str(cloud_path), queue)),
        ctx.Process(target=_recovery_race_worker,
                    args=(str(journal_path), str(ledger_path), str(cloud_path), queue)),
    ]
    for worker in workers:
        worker.start()
    statuses = [queue.get(timeout=20) for _ in workers]
    for worker in workers:
        worker.join(20)
        assert worker.exitcode == 0, statuses

    assert all(status in {"recovered", "none"} for status in statuses), statuses
    final = SyncLedger(ledger_path)
    assert final.synced_slots(TARGET.isoformat(), "F1", "张", "111") == [6]
    assert recovery_status(final)["pending_operations"] == []

def test_journal_atomic_write_disk_failure_blocks_all_cloud_writes(tmp_path, monkeypatch):
    cli = GridCli(make_grid(rows=[{0: "张", 2: "111", 7: "3"}]))
    ledger = SyncLedger(tmp_path / "state.json")
    plan = _plan(cli, _orders(6), ledger)

    def boom(_path, _text):
        raise OSError("模拟磁盘写中断/不可写")

    monkeypatch.setattr("app.wps.journal.atomic_write_text", boom)
    result = apply_plan(cli, plan, ledger=ledger, marker_enabled=False)

    assert result["status"] == "uncertain"
    assert result["uncertain"] is True
    assert result["next_action"] == "fix_journal"
    assert cli.writes == [] and cli.inserts == []
    assert cli.grid[(2, 7)] == "3"

def test_cross_process_record_save_merges_instead_of_overwriting(tmp_path):
    """两个进程各自旧快照 record()+save()：后写者必须合并不同批次，不能覆盖。"""
    path = str(tmp_path / "state.json")
    ctx = _fork_ctx()
    workers = [
        ctx.Process(target=_ledger_record_save_worker, args=(path, "C", 1)),
        ctx.Process(target=_ledger_record_save_worker, args=(path, "D", 1)),
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(30)
        assert worker.exitcode == 0

    ledger = SyncLedger(path)
    people = ledger.data["batches"][TARGET.isoformat()]["F1"]["people"]
    assert set(people) == {"Cdirect" + chr(0) + "pCdirect",
                           "Ddirect" + chr(0) + "pDdirect"}

# ----------------------------------------------------------------------
# P1 回派：持锁后重新读取 journal / ledger，旧对象不能绕过 pending
# ----------------------------------------------------------------------

def test_stale_journal_object_after_lock_discovers_child_pending(tmp_path):
    """进程甲先构造旧 journal；乙写入 pending 后；甲持旧对象执行必须被阻断。"""
    cli = GridCli(make_grid(rows=[{0: "张", 2: "111", 7: "3"}]))
    ledger = SyncLedger(tmp_path / "state.json")
    plan = _plan(cli, _orders(6), ledger)
    from app.wps.executor import _build_journal_record
    record = _build_journal_record(plan[0], 1, False, ledger)
    record_path = tmp_path / "pending-record.json"
    record_path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")

    journal_path = tmp_path / "state.journal"
    old_journal = SyncJournal(journal_path)       # 甲：锁外构造的旧对象
    assert old_journal.pending_operations() == {}

    ctx = _fork_ctx()
    writer = ctx.Process(target=_write_pending_journal_worker,
                         args=(str(journal_path), str(record_path), "op-child"))
    writer.start()
    writer.join(20)
    assert writer.exitcode == 0
    assert SyncJournal(journal_path).get_operation("op-child") is not None
    assert old_journal.pending_operations() == {}, "旧对象本身必须仍是旧的"

    writes_before = len(cli.writes)
    grid_before = dict(cli.grid)
    result = apply_plan(cli, plan, ledger=ledger, marker_enabled=False,
                        journal=old_journal)

    assert result["status"] != "ok"
    assert result["written"] == 0
    assert len(cli.writes) == writes_before, "持锁后必须发现子进程 pending，不得再写云端"
    assert cli.grid == grid_before
    # 旧对象已被刷新为磁盘最新状态（recovery 可能把 status 推进为 not_started）。
    assert old_journal.get_operation("op-child") is not None


def test_stale_ledger_object_after_lock_blocks_repreview(tmp_path):
    """旧 ledger 对象在锁外被另一进程推进：必须先回退重新预览，不能按旧增量写。"""
    ledger_path = tmp_path / "state.json"
    baseline = SyncLedger(ledger_path)
    baseline.merge_entries(TARGET.isoformat(), "F1", {
        "基线" + chr(0) + "111": {"slots": [1], "local": 1, "total": 1},
    })
    old_ledger = SyncLedger(ledger_path)
    cli = GridCli(make_grid(rows=[{0: "张", 2: "111", 7: "3"}]))
    plan = _plan(cli, _orders(6), old_ledger)

    ctx = _fork_ctx()
    writer = ctx.Process(target=_ledger_record_save_worker,
                         args=(str(ledger_path), "Z", 1))
    writer.start()
    writer.join(20)
    assert writer.exitcode == 0

    grid_before = dict(cli.grid)
    writes_before = len(cli.writes)
    result = apply_plan(cli, plan, ledger=old_ledger, marker_enabled=False)

    assert result["status"] == "failed"
    assert result["uncertain"] is False
    assert result["next_action"] == "repreview"
    assert result["written"] == 0
    assert len(cli.writes) == writes_before
    assert cli.grid == grid_before


def test_corrupt_journal_after_old_object_blocks_before_cloud_write(tmp_path):
    cli = GridCli(make_grid(rows=[{0: "张", 2: "111", 7: "3"}]))
    ledger = SyncLedger(tmp_path / "state.json")
    plan = _plan(cli, _orders(6), ledger)
    journal_path = tmp_path / "state.journal"
    old_journal = SyncJournal(journal_path)
    journal_path.write_text("{ broken", encoding="utf-8")

    grid_before = dict(cli.grid)
    result = apply_plan(cli, plan, ledger=ledger, marker_enabled=False,
                        journal=old_journal)

    assert result["status"] == "uncertain"
    assert result["uncertain"] is True
    assert result["next_action"] == "fix_journal"
    assert result["written"] == 0
    assert cli.grid == grid_before


def test_corrupt_ledger_after_old_object_blocks_before_cloud_write(tmp_path):
    ledger_path = tmp_path / "state.json"
    old_ledger = SyncLedger(ledger_path)
    cli = GridCli(make_grid(rows=[{0: "张", 2: "111", 7: "3"}]))
    plan = _plan(cli, _orders(6), old_ledger)
    ledger_path.write_text("{ broken", encoding="utf-8")

    grid_before = dict(cli.grid)
    result = apply_plan(cli, plan, ledger=old_ledger, marker_enabled=False)

    assert result["status"] == "uncertain"
    assert result["uncertain"] is True
    assert result["next_action"] == "fix_journal"
    assert result["written"] == 0
    assert cli.grid == grid_before


def test_recovery_status_contract_hides_raw_target_and_paths(tmp_path):
    ledger_path = tmp_path / "state.json"
    journal_path = tmp_path / "state.journal"
    journal = SyncJournal(journal_path)
    journal.create_operation("op-contract", {
        "0:东湖中餐:F1": {
            "sheet": "东湖中餐",
            "file_id": "SENSITIVE_FILE_ID",
            "target_date": TARGET.isoformat(),
            "status": "uncertain",
            "cloud_checked": False,
            "evidence": "local_journal",
            "reason": "回读不一致",
            "next_action": "manual_reconcile",
            "manual_required": "",
            "problems": ["总餐次期望 9 实际 3"],
        },
    }, target_date=TARGET.isoformat())
    journal.save()

    ledger = SyncLedger(ledger_path)
    ledger.save()
    journal_bytes_before = journal_path.read_bytes()
    ledger_bytes_before = ledger_path.read_bytes()

    status = recovery_status(ledger, journal=journal)

    assert journal_path.read_bytes() == journal_bytes_before
    assert ledger_path.read_bytes() == ledger_bytes_before
    assert status["ok"] is True
    assert status["contract_version"] == 1
    assert status["source"] == "local_journal"
    assert status["read_only"] is True
    assert status["queried_cloud"] is False
    text = json.dumps(status, ensure_ascii=False)
    assert "SENSITIVE_FILE_ID" not in text
    assert str(tmp_path) not in text
    assert "journal_path" not in status
    assert len(status["pending_operations"]) == 1
    sheet = status["pending_operations"][0]["sheets"][0]
    assert sheet["target_ref"].startswith("wps-target:")
    assert sheet["status"] == "uncertain"
    assert sheet["error_code"] == "wps_recovery_uncertain"
    assert sheet["allowed_next_actions"] == ["manual_reconcile"]
    assert sheet["cloud_checked"] is False
    assert sheet["evidence"] == "local_journal"
    assert "risk_reason" not in sheet and "problems" not in sheet

def test_journal_read_failure_after_old_object_blocks_before_cloud_write(tmp_path):
    cli = GridCli(make_grid(rows=[{0: "张", 2: "111", 7: "3"}]))
    ledger = SyncLedger(tmp_path / "state.json")
    plan = _plan(cli, _orders(6), ledger)
    journal_path = tmp_path / "state.journal"
    old_journal = SyncJournal(journal_path)
    journal_path.mkdir()          # 读日志时报 IsADirectoryError -> 损坏/不可读

    grid_before = dict(cli.grid)
    result = apply_plan(cli, plan, ledger=ledger, marker_enabled=False,
                        journal=old_journal)

    assert result["status"] == "uncertain"
    assert result["next_action"] == "fix_journal"
    assert result["written"] == 0
    assert cli.grid == grid_before

# ----------------------------------------------------------------------
# verified journal 增长：安全归档/压缩
# ----------------------------------------------------------------------

def _writer_record(sheet: str, status: str, **extra) -> dict:
    record = {
        "sheet": sheet,
        "file_id": "F1",
        "target_date": TARGET.isoformat(),
        "status": status,
        "reason": "",
        "next_action": "",
        "problems": [],
    }
    record.update(extra)
    return record


def test_compact_archives_only_proven_terminal_operations(tmp_path):
    ledger_path = tmp_path / "state.json"
    ledger = SyncLedger(ledger_path)
    person_key = "张" + chr(0) + "111"
    ledger.merge_entries(TARGET.isoformat(), "F1", {
        person_key: {"slots": [6], "local": 6, "total": 9},
    })

    journal_path = tmp_path / "state.journal"
    journal = SyncJournal(journal_path)
    journal.create_operation("op-verified", {
        "s1": _writer_record("东湖中餐", "verified",
                             ledger_entries={person_key: {"slots": [6],
                                                           "local": 6, "total": 9}}),
    })
    journal.create_operation("op-failed", {
        "s2": _writer_record("衣锦中餐", "failed_no_write"),
    })
    journal.create_operation("op-uncertain", {
        "s3": _writer_record("医学院中餐", "uncertain", next_action="manual_reconcile"),
    })
    journal.create_operation("op-writing", {
        "s4": _writer_record("医学院晚餐", "writing", next_action="recover_journal"),
    })
    journal.save()

    result = journal.compact(ledger=ledger, keep_operations=0)

    assert result["removed"] == 2, result
    reloaded = SyncJournal(journal_path)
    assert set(reloaded.operations()) == {"op-uncertain", "op-writing"}
    archive = json.loads((tmp_path / "state.journal.archive").read_text(encoding="utf-8"))
    assert set(archive["operations"]) == {"op-verified", "op-failed"}
    # 归档后 pending/uncertain 恢复记录仍完整
    assert set(reloaded.pending_operations()) == {"op-uncertain", "op-writing"}


def test_compact_keeps_verified_when_ledger_cannot_prove_slots(tmp_path):
    journal_path = tmp_path / "state.journal"
    journal = SyncJournal(journal_path)
    journal.create_operation("op-verified", {
        "s1": _writer_record("东湖中餐", "verified",
                             ledger_entries={"张" + chr(0) + "111": {
                                 "slots": [6], "local": 6, "total": 9}}),
    })
    journal.save()
    empty_ledger = SyncLedger(tmp_path / "empty.json")

    result = journal.compact(ledger=empty_ledger, keep_operations=0)

    assert result["removed"] == 0
    assert "op-verified" in SyncJournal(journal_path).operations()
    assert not (tmp_path / "state.journal.archive").exists()


def test_compact_never_touches_pending_or_uncertain(tmp_path):
    journal_path = tmp_path / "state.journal"
    journal = SyncJournal(journal_path)
    for op_id, status in (
            ("op-uncertain", "uncertain"), ("op-ledger-pending", "ledger_pending"),
            ("op-writing", "writing"), ("op-planned", "planned")):
        sheet = _writer_record("东湖中餐", status,
                               next_action=("manual_reconcile"
                                            if status == "uncertain" else "recover_journal"))
        if status == "ledger_pending":
            sheet["ledger_entries"] = {"张" + chr(0) + "111": {
                "slots": [6], "local": 6, "total": 9}}
        journal.create_operation(op_id, {f"s-{op_id}": sheet})
    journal.save()

    result = journal.compact(ledger=SyncLedger(tmp_path / "state.json"),
                             keep_operations=0)

    assert result["removed"] == 0
    assert set(SyncJournal(journal_path).operations()) == {
        "op-uncertain", "op-ledger-pending", "op-writing", "op-planned"}


def test_compact_corrupt_archive_fails_closed_without_deleting_journal(tmp_path):
    ledger_path = tmp_path / "state.json"
    ledger = SyncLedger(ledger_path)
    person_key = "张" + chr(0) + "111"
    ledger.merge_entries(TARGET.isoformat(), "F1", {
        person_key: {"slots": [6], "local": 6, "total": 9}})
    journal_path = tmp_path / "state.journal"
    journal = SyncJournal(journal_path)
    journal.create_operation("op-verified", {
        "s1": _writer_record("东湖中餐", "verified",
                             ledger_entries={person_key: {"slots": [6],
                                                           "local": 6, "total": 9}}),
    })
    journal.save()
    archive_path = tmp_path / "state.journal.archive"
    archive_path.write_text("{ broken", encoding="utf-8")

    with pytest.raises(Exception):
        journal.compact(ledger=ledger, keep_operations=0)

    assert "op-verified" in SyncJournal(journal_path).operations(),         "归档文件损坏时不能删除原 journal"

def test_successful_apply_auto_compacts_verified_journal(tmp_path, monkeypatch):
    """连续成功上传时，verified terminal operation 归档到冷文件，热 journal 有界。"""
    import app.wps.executor as executor

    monkeypatch.setattr(executor, "DEFAULT_COMPACT_KEEP_OPERATIONS", 1)
    cli = GridCli(make_grid(rows=[{0: "张", 2: "111", 7: "3"}]))
    ledger_path = tmp_path / "state.json"

    for _ in range(4):
        ledger = SyncLedger(ledger_path)
        result = apply_plan(cli, _plan(cli, _orders(6), ledger), ledger=ledger,
                            marker_enabled=False)
        assert result["status"] == "ok", result

    journal_path = tmp_path / "state.json.journal"
    journal = SyncJournal(journal_path)
    assert len(journal.operations()) <= 2, list(journal.operations())
    archive_path = tmp_path / "state.json.journal.archive"
    assert archive_path.exists()
    archive = json.loads(archive_path.read_text(encoding="utf-8"))
    assert archive["operations"], "旧 verified operation 必须进冷归档，不能无审计删除"
    # 幂等锚点仍在 ledger，云端没有重复加餐
    assert cli.grid[(2, 7)] == "9"
    assert SyncLedger(ledger_path).synced_slots(TARGET.isoformat(), "F1", "张", "111") == [6]

def test_apply_plan_cannot_bypass_pending_with_different_journal_path(tmp_path):
    """调用者另传一个 journal 路径也不能绕过 ledger 的 canonical pending。"""
    cli = GridCli(make_grid(rows=[{0: "张", 2: "111", 7: "3"}]))
    ledger_path = tmp_path / "state.json"
    ledger = SyncLedger(ledger_path)
    plan = _plan(cli, _orders(6), ledger)
    from app.wps.executor import _build_journal_record
    record = _build_journal_record(plan[0], 1, False, ledger)
    record_path = tmp_path / "pending-record.json"
    record_path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")

    canonical_journal_path = tmp_path / "state.json.journal"
    other_journal = SyncJournal(tmp_path / "other.journal")
    ctx = _fork_ctx()
    writer = ctx.Process(target=_write_pending_journal_worker,
                         args=(str(canonical_journal_path), str(record_path),
                               "op-canonical"))
    writer.start()
    writer.join(20)
    assert writer.exitcode == 0

    grid_before = dict(cli.grid)
    result = apply_plan(cli, plan, ledger=ledger, marker_enabled=False,
                        journal=other_journal)

    assert result["status"] != "ok"
    assert result["written"] == 0
    assert cli.grid == grid_before

def test_persistent_ledger_adopts_canonical_journal_when_caller_passes_memory_journal(tmp_path):
    """有磁盘 ledger 时，调用者传内存 journal 也不能阻止 operation 落到 canonical 文件。"""
    cli = GridCli(make_grid(rows=[{0: "张", 2: "111", 7: "3"}]))
    ledger = SyncLedger(tmp_path / "state.json")
    plan = _plan(cli, _orders(6), ledger)
    memory_journal = SyncJournal()

    result = apply_plan(cli, plan, ledger=ledger, marker_enabled=False,
                        journal=memory_journal)

    assert result["status"] == "ok"
    canonical = journal_path_for(ledger.path)
    assert canonical.exists(), "有磁盘 ledger 时必须把 operation 落到 canonical journal"
    assert SyncJournal(canonical).operations(), "canonical journal 应有本次 operation"

def test_recover_pending_operations_refreshes_stale_journal_object(tmp_path):
    """直接调用恢复入口时，旧 journal 对象也不能漏掉子进程刚写入的 pending。"""
    cli = GridCli(make_grid(rows=[{0: "张", 2: "111", 7: "3"}]))
    ledger = SyncLedger(tmp_path / "state.json")
    plan = _plan(cli, _orders(6), ledger)
    from app.wps.executor import _build_journal_record
    record = _build_journal_record(plan[0], 1, False, ledger)
    record_path = tmp_path / "pending-record.json"
    record_path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")

    journal_path = tmp_path / "state.json.journal"
    stale_journal = SyncJournal(journal_path)
    assert stale_journal.pending_operations() == {}

    ctx = _fork_ctx()
    writer = ctx.Process(target=_write_pending_journal_worker,
                         args=(str(journal_path), str(record_path), "op-direct"))
    writer.start()
    writer.join(20)
    assert writer.exitcode == 0

    writes_before = len(cli.writes)
    report = recover_pending_operations(cli, stale_journal, ledger=ledger)

    assert report["status"] in ("not_started", "uncertain", "recovered")
    assert len(cli.writes) == writes_before
    assert stale_journal.get_operation("op-direct") is not None
    operation = stale_journal.get_operation("op-direct")
    assert operation["sheets"]["0:东湖中餐:F1"]["status"] == "not_started"

# ----------------------------------------------------------------------
# 对外恢复查询最小披露：字段白名单与敏感标记回归
# ----------------------------------------------------------------------

_SAFE_TOP_KEYS = {
    "ok", "contract_version", "source", "read_only", "queried_cloud",
    "contains_cloud_checked_records", "operations", "pending_operations",
    "counts", "next_action", "error_code",
}
_SAFE_OPERATION_KEYS = {
    "operation_id", "operation_ref", "status", "pending", "cloud_checked",
    "created_at", "updated_at", "target_date", "target_refs", "sheet_count",
    "error_code", "allowed_next_actions", "manual_required", "sheets",
}
_SAFE_SHEET_KEYS = {
    "target_date", "target_ref", "status", "raw_status", "error_code",
    "allowed_next_actions", "manual_required", "cloud_checked", "evidence",
}
_SAFE_MARKERS = [
    "MARKER_TARGET_NAME", "MARKER_FILE_ID", "MARKER_RISK_REASON",
    "MARKER_PROBLEM", "MARKER_NESTED", "MARKER_MANUAL", "MARKER_EVIDENCE",
    "MARKER_CREATED", "MARKER_EXCEPTION", "MARKER_CORRUPT", "MARKER_LEDGER",
    "MARKER_OPERATION", "MARKER_PHONE_13800138000", "MARKER_PERSON_张三",
]


def _assert_safe_recovery_shape(payload: dict) -> None:
    assert set(payload) <= _SAFE_TOP_KEYS, set(payload) - _SAFE_TOP_KEYS
    assert payload["source"] == "local_journal"
    assert payload["read_only"] is True
    assert payload["queried_cloud"] is False
    assert isinstance(payload["counts"], dict)
    assert set(payload["counts"]) <= set(_SAFE_STATUS_KEYS)
    for operation in payload["operations"]:
        assert set(operation) <= _SAFE_OPERATION_KEYS, set(operation) - _SAFE_OPERATION_KEYS
        for sheet in operation["sheets"]:
            assert set(sheet) <= _SAFE_SHEET_KEYS, set(sheet) - _SAFE_SHEET_KEYS
    for operation in payload["pending_operations"]:
        assert set(operation) <= _SAFE_OPERATION_KEYS
    text = json.dumps(payload, ensure_ascii=False)
    for marker in _SAFE_MARKERS:
        assert marker not in text, f"对外响应泄露敏感标记：{marker}"
    assert "risk_reason" not in text
    assert "problems" not in text
    assert "journal_path" not in text


_SAFE_STATUS_KEYS = {
    "planned", "writing", "ledger_pending", "uncertain",
    "verified", "failed", "not_started", "retired_guarded",
}


def test_recovery_status_safe_dto_redacts_nested_sensitive_markers(tmp_path):
    ledger_path = tmp_path / "state.json"
    ledger = SyncLedger(ledger_path)
    ledger.save()
    journal_path = tmp_path / "state.journal"
    journal = SyncJournal(journal_path)
    operation_id = "wps-0123456789abcdef"
    journal.create_operation(operation_id, {
        "sheet-key-MARKER_TARGET_NAME": {
            "sheet": "MARKER_TARGET_NAME",
            "file_id": "MARKER_FILE_ID",
            "target_date": TARGET.isoformat(),
            "status": "uncertain",
            "reason": "MARKER_RISK_REASON MARKER_PERSON_张三 MARKER_PHONE_13800138000",
            "problems": [
                "MARKER_PROBLEM MARKER_PERSON_张三 MARKER_PHONE_13800138000",
                {"nested": "MARKER_NESTED"},
            ],
            "manual_required": "MARKER_MANUAL",
            "evidence": "MARKER_EVIDENCE",
            "created_at": "MARKER_CREATED",
            "cloud_checked": False,
        },
    }, target_date=TARGET.isoformat())
    journal.save()

    journal_before = journal_path.read_bytes()
    ledger_before = ledger_path.read_bytes()
    safe = recovery_status(ledger, journal=journal)

    assert journal_path.read_bytes() == journal_before, "对外查询不得改 journal"
    assert ledger_path.read_bytes() == ledger_before, "对外查询不得改 ledger"
    assert safe["ok"] is True
    assert safe["counts"]["uncertain"] == 1
    assert safe["next_action"] == "manual_reconcile"
    operation = safe["operations"][0]
    assert operation["operation_id"] == operation_id
    assert operation["operation_ref"].startswith("wps-op:")
    assert operation["target_refs"]
    assert operation["target_refs"][0].startswith("wps-target:")
    assert operation["created_at"].startswith("20")
    assert operation["allowed_next_actions"] == ["manual_reconcile"]
    sheet = operation["sheets"][0]
    assert sheet["target_ref"].startswith("wps-target:")
    assert sheet["target_date"] == TARGET.isoformat()
    assert sheet["status"] == "uncertain"
    assert sheet["error_code"] == "wps_recovery_uncertain"
    assert sheet["evidence"] == "local_journal"
    assert sheet["cloud_checked"] is False
    _assert_safe_recovery_shape(safe)

    # 内部审计仍保留完整 journal 数据，不为脱敏破坏恢复证据。
    raw = SyncJournal(journal_path).get_operation(operation_id)
    raw_sheet = raw["sheets"]["sheet-key-MARKER_TARGET_NAME"]
    assert raw_sheet["file_id"] == "MARKER_FILE_ID"
    assert "MARKER_PROBLEM" in str(raw_sheet["problems"])
    assert raw_sheet["reason"] == (
        "MARKER_RISK_REASON MARKER_PERSON_张三 MARKER_PHONE_13800138000")


def test_recovery_status_safe_dto_covers_empty_and_mixed_records(tmp_path):
    ledger = SyncLedger(tmp_path / "state.json")
    ledger.save()
    journal_path = tmp_path / "state.journal"

    empty_journal = SyncJournal(journal_path)
    empty = recovery_status(ledger, journal=empty_journal)
    assert empty["ok"] is True
    assert empty["operations"] == []
    assert empty["pending_operations"] == []
    assert set(empty["counts"]) == _SAFE_STATUS_KEYS
    _assert_safe_recovery_shape(empty)

    journal = SyncJournal(journal_path)
    journal.create_operation("op-mixed", {
        "uncertain": {
            "sheet": "MARKER_TARGET_NAME",
            "file_id": "MARKER_FILE_ID",
            "target_date": TARGET.isoformat(),
            "status": "uncertain",
            "reason": "MARKER_RISK_REASON",
            "problems": ["MARKER_PROBLEM", {"deep": ["MARKER_NESTED"]}],
            "manual_required": "MARKER_MANUAL",
            "evidence": "MARKER_EVIDENCE",
        },
        "verified": {
            "sheet": "MARKER_TARGET_NAME2",
            "file_id": "MARKER_FILE_ID2",
            "target_date": TARGET.isoformat(),
            "status": "verified",
            "reason": "MARKER_RISK_REASON2",
            "problems": [],
            "cloud_checked": True,
            "evidence": "journal+cloud_read",
        },
    }, target_date=TARGET.isoformat())
    journal.save()
    mixed = recovery_status(ledger, journal=journal)
    assert mixed["counts"]["uncertain"] == 1
    assert mixed["counts"]["verified"] == 1
    _assert_safe_recovery_shape(mixed)
    assert "MARKER_TARGET_NAME" not in json.dumps(mixed, ensure_ascii=False)


def test_recovery_status_error_responses_are_redacted_and_read_only(tmp_path):
    ledger = SyncLedger(tmp_path / "state.json")
    ledger.save()
    journal_path = ledger.journal_path
    journal_path.write_text(
        "{ broken MARKER_CORRUPT MARKER_PERSON_张三 MARKER_PHONE_13800138000",
        encoding="utf-8")
    before = journal_path.read_bytes()

    error = recovery_status(ledger)

    assert error["ok"] is False
    assert error["error_code"] == "wps_recovery_journal_unreadable"
    assert "reason" not in error
    assert "journal_path" not in error
    assert journal_path.read_bytes() == before
    _assert_safe_recovery_shape(error)


def test_recovery_status_safe_dto_redacts_runtime_exception(tmp_path):
    class BoomJournal:
        def operations(self):
            raise RuntimeError("MARKER_EXCEPTION MARKER_PERSON_张三")

        def pending_operations(self):
            raise RuntimeError("MARKER_EXCEPTION")

    safe = recovery_status(None, journal=BoomJournal())
    assert safe["ok"] is False
    assert safe["error_code"] == "wps_recovery_internal_error"
    assert "reason" not in safe
    _assert_safe_recovery_shape(safe)


def test_recovery_status_safe_dto_no_raw_keys_even_with_unknown_statuses(tmp_path):
    ledger = SyncLedger(tmp_path / "state.json")
    ledger.save()
    journal = SyncJournal(tmp_path / "state.journal")
    journal.create_operation("op-unknown", {
        "s1": {
            "sheet": "MARKER_TARGET_NAME",
            "file_id": "MARKER_FILE_ID",
            "target_date": TARGET.isoformat(),
            "status": "some_future_status MARKER_NESTED",
            "reason": "MARKER_RISK_REASON",
            "problems": ["MARKER_PROBLEM"],
            "evidence": "MARKER_EVIDENCE",
        },
    })
    journal.save()
    safe = recovery_status(ledger, journal=journal)
    _assert_safe_recovery_shape(safe)
    assert safe["operations"][0]["status"] == "uncertain"
    assert safe["operations"][0]["sheets"][0]["raw_status"] == "uncertain"


def test_ledger_corrupt_message_is_redacted(tmp_path):
    path = tmp_path / "MARKER_LEDGER_state.json"
    path.write_text(
        "{ broken MARKER_CORRUPT MARKER_PERSON_张三 MARKER_PHONE_13800138000",
        encoding="utf-8")
    with pytest.raises(LedgerCorruptError) as excinfo:
        SyncLedger(path)
    message = str(excinfo.value)
    for marker in _SAFE_MARKERS:
        assert marker not in message
    assert str(tmp_path) not in message
    assert "MARKER_LEDGER_state" not in message

# ----------------------------------------------------------------------
# R6 W1/W2/W4/W8 回归（先失败，再修复）
# ----------------------------------------------------------------------

def _one_person_cloud():
    return GridCli(make_grid(rows=[{
        0: "P", 2: "111", 4: "1", 5: "中餐", 6: "经济",
        7: "5", 8: "=SUM(D3)", 9: "=H3-I3", 10: "备注",
    }]))


def _build_two_slot_plan(cli, ledger):
    return build_plan(
        cli,
        local_orders={"东湖中餐": _orders(1, 1, name="P", phone="111")},
        tables={"东湖中餐": {"file_id": "F1"}},
        target=TARGET, ledger=ledger, marker_enabled=True,
        address_order={}, sort_enabled=True,
    )


def test_w1_stale_plan_ledger_snapshot_blocks_cross_process_double_write(tmp_path):
    """R6 W1：计划必须携带构建时账本快照，不能用执行时新加载账本冒名顶替。"""
    ledger_path = tmp_path / "state.json"
    seed = SyncLedger(ledger_path)
    seed.record(TARGET.isoformat(), "F1", {
        "P" + chr(0) + "111": {"slots": [1], "local": 1, "total": 5},
    })
    seed.save()

    # B 先构建计划（账本快照 L0）。
    plans_b = _build_two_slot_plan(_one_person_cloud(), SyncLedger(ledger_path))

    # A 用同样的起点先完成一次成功上传：云写入 + 账本提交。
    cli_a = _one_person_cloud()
    plans_a = _build_two_slot_plan(cli_a, SyncLedger(ledger_path))
    result_a = apply_plan(cli_a, plans_a, ledger=SyncLedger(ledger_path),
                          marker_enabled=True)
    assert result_a["status"] == "ok", result_a

    # B 在 A 完成后用“执行时新加载”的账本执行旧计划。
    cli_b = GridCli(dict(cli_a.grid))
    writes_before = len(cli_b.writes)
    result_b = apply_plan(cli_b, plans_b, ledger=SyncLedger(ledger_path),
                          marker_enabled=True)

    rows = [str(value) for (row, col), value in cli_b.grid.items()
            if col == 0 and str(value).strip() == "P"]
    assert len(rows) == 2, f"B 不得再追加第三行 P：{rows}"
    assert result_b["status"] != "ok"
    assert len(cli_b.writes) == writes_before, "stale plan 必须零云端写入"


def test_w2_unsupported_journal_version_blocks_before_cloud_write(tmp_path):
    """R6 W2：高于本版本支持的 journal version 必须失败关闭。"""
    cli = GridCli(make_grid(rows=[{0: "张", 2: "111", 7: "3"}]))
    ledger = SyncLedger(tmp_path / "state.json")
    plan = _plan(cli, _orders(6), ledger)
    journal_path = ledger.journal_path
    payload = {
        "version": 2,
        "operations": {
            "op-v2": {
                "operation_id": "op-v2",
                "target_date": TARGET.isoformat(),
                "created_at": "2026-09-19T00:00:00.000000",
                "updated_at": "2026-09-19T00:00:00.000000",
                "status": "awaiting_cloud_v2",
                "next_action": "manual_reconcile",
                "sheets": {
                    "s": {
                        "sheet": "东湖中餐", "file_id": "F1",
                        "target_date": TARGET.isoformat(),
                        "status": "awaiting_cloud_v2",
                        "intents": [],
                    },
                },
            },
        },
    }
    journal_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    before = journal_path.read_bytes()
    grid_before = dict(cli.grid)

    result = apply_plan(cli, plan, ledger=ledger, marker_enabled=False)

    assert result["status"] == "uncertain"
    assert result["uncertain"] is True
    assert result["next_action"] == "fix_journal"
    assert result["written"] == 0 and cli.writes == []
    assert cli.grid == grid_before
    assert journal_path.read_bytes() == before, "审计日志不得被改写/删除"


def test_w2_unknown_sheet_status_blocks_and_is_not_rewritten_to_verified(tmp_path):
    """R6 W2：未知 operation/sheet 状态必须继续阻断，不能静默改成 verified。"""
    cli = GridCli(make_grid(rows=[{0: "张", 2: "111", 7: "3"}]))
    ledger = SyncLedger(tmp_path / "state.json")
    plan = _plan(cli, _orders(6), ledger)
    journal_path = ledger.journal_path
    payload = {
        "version": 1,
        "operations": {
            "op-unknown": {
                "operation_id": "op-unknown",
                "target_date": TARGET.isoformat(),
                "created_at": "2026-09-19T00:00:00.000000",
                "updated_at": "2026-09-19T00:00:00.000000",
                "status": "awaiting_cloud_v2",
                "next_action": "manual_reconcile",
                "sheets": {
                    "s": {
                        "sheet": "东湖中餐", "file_id": "F1",
                        "target_date": TARGET.isoformat(),
                        "status": "awaiting_cloud_v2",
                        "intents": [],
                    },
                },
            },
        },
    }
    journal_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    grid_before = dict(cli.grid)

    result = apply_plan(cli, plan, ledger=ledger, marker_enabled=False)

    assert result["status"] != "ok"
    assert result["written"] == 0 and cli.writes == []
    assert cli.grid == grid_before
    reloaded = SyncJournal(journal_path)
    raw = reloaded.get_operation("op-unknown")
    assert raw is not None, "未知状态 operation 不得被删除"
    assert raw["sheets"]["s"]["status"] == "awaiting_cloud_v2",         "未知 sheet 状态必须保留原始审计值，不能被改写成 verified"
    assert raw["status"] != "verified"

    status = recovery_status(ledger, journal=reloaded)
    assert status["next_action"] == "manual_reconcile"
    assert status["counts"]["uncertain"] == 1


def test_w4_append_only_plan_blocks_when_collaborator_rows_occupied(tmp_path):
    """R6 W4：append-only 新行目标位置已有协作者内容时必须拒绝覆盖。"""
    cli = GridCli(make_grid(rows=[]))
    ledger = SyncLedger(tmp_path / "state.json")
    orders = [
        CloudOrder("东湖中餐", "新人A", "addrA", "201", "中餐", "经济", 2, rows=(3,)),
        CloudOrder("东湖中餐", "新人B", "addrB", "202", "中餐", "经济", 3, rows=(4,)),
    ]
    plans = build_plan(
        cli, local_orders={"东湖中餐": orders},
        tables={"东湖中餐": {"file_id": "F1"}},
        target=TARGET, ledger=ledger, marker_enabled=False,
        address_order={}, sort_enabled=False,
    )
    _, block = plans[0], plans[0].insert_blocks[0]
    assert block.append_only is True

    cli.grid[(2, 0)] = "老客户X"
    cli.grid[(2, 2)] = "901"
    cli.grid[(2, 7)] = "4"
    cli.grid[(3, 0)] = "老客户Y"
    cli.grid[(3, 2)] = "902"
    cli.grid[(3, 7)] = "9"
    before = dict(cli.grid)

    result = apply_plan(cli, plans, ledger=ledger, marker_enabled=False)

    assert result["status"] != "ok"
    assert result["written"] == 0 and cli.writes == []
    assert cli.grid == before, "协作者的 X/Y 行必须原样保留"


def test_w8_concurrent_same_new_customer_below_target_rows_is_rejected(tmp_path):
    """R6 W8：新增客户安全检查必须在目标行之外也能发现并发出现的同一客户。"""
    cli = GridCli(make_grid(rows=[]))
    ledger = SyncLedger(tmp_path / "state.json")
    orders = [CloudOrder("东湖中餐", "新人A", "addrA", "201",
                         "中餐", "经济", 1, rows=(3,))]
    plans = build_plan(
        cli, local_orders={"东湖中餐": orders},
        tables={"东湖中餐": {"file_id": "F1"}},
        target=TARGET, ledger=ledger, marker_enabled=False,
        address_order={}, sort_enabled=False,
    )
    # 协作者把同名同电话客户放到目标行（第 3 行）之外的第 10 行。
    cli.grid[(9, 0)] = "新人A"
    cli.grid[(9, 2)] = "201"
    before = dict(cli.grid)

    result = apply_plan(cli, plans, ledger=ledger, marker_enabled=False)

    assert result["status"] != "ok"
    assert result["written"] == 0 and cli.writes == []
    assert cli.grid == before
    assert cli.grid.get((2, 0)) is None, "不能忽略并发客户而把新人A写到第 3 行"

def test_w3_target_date_gone_needs_guarded_retire_and_keeps_same_target_block(tmp_path):
    """R6 W3：日期列变化不能冒充 cloud_untouched；退出旧任务必须留防重闸门。"""
    ledger_path = tmp_path / "state.json"
    ledger = SyncLedger(ledger_path)
    cli = GridCli(make_grid(rows=[{
        0: "张", 2: "111", 4: "1", 5: "中餐", 6: "经济",
        7: "5", 8: "=SUM(D3)", 9: "=H3-I3", 10: "x",
    }]))
    orders = [CloudOrder("东湖中餐", "张", "小", "111", "中餐", "经济", 6,
                         rows=(19,))]
    plans = build_plan(
        cli, local_orders={"东湖中餐": orders},
        tables={"东湖中餐": {"file_id": "F1"}},
        target=TARGET, ledger=ledger, marker_enabled=False,
        address_order={}, sort_enabled=False,
    )

    class Boom(GridCli):
        def write_cells(self, *_args, **_kwargs):
            raise RuntimeError("unexpected in-process failure before cloud write")

    with pytest.raises(RuntimeError):
        apply_plan(Boom(cli.grid), plans, ledger=ledger, marker_enabled=False)

    journal_path = ledger.journal_path
    op_id = next(iter(SyncJournal(journal_path).operations()))
    # 协作者把目标日期列改成别的日期：target_date_mismatch，但无法证明未写。
    cli.grid[(1, 4)] = "9.15 周二"
    same_target_plan = build_plan(
        cli, local_orders={"东湖中餐": orders},
        tables={"东湖中餐": {"file_id": "F1"}},
        target=TARGET, ledger=SyncLedger(ledger_path), marker_enabled=False,
        address_order={}, sort_enabled=False,
    )

    blocked = apply_plan(cli, same_target_plan, ledger=SyncLedger(ledger_path),
                         marker_enabled=False)
    assert blocked["status"] != "ok"
    assert blocked["written"] == 0 and cli.writes == []
    assert SyncJournal(journal_path).get_operation(op_id) is not None

    ledger_reader = SyncLedger(ledger_path)
    for decision in ("cloud_verified", "cloud_untouched"):
        out = resolve_pending_operation(ledger_reader, op_id, decision, cli=cli,
                                        confirm_structure_checked=True)
        assert out["ok"] is False, f"{decision} 不能在日期列不可判定时放行"
        assert SyncJournal(journal_path).get_operation(op_id) is not None

    retired = resolve_pending_operation(
        ledger_reader, op_id, "retire_guarded", cli=cli,
        confirm_structure_checked=True,
        note="人工核对云端后退出旧任务，保留同表同日防重闸门")
    assert retired["ok"] is True
    assert retired["status"] == "retired_guarded"
    assert retired["next_action"] == "manual_reconcile"

    reloaded = SyncJournal(journal_path)
    op = reloaded.get_operation(op_id)
    assert op is not None, "退出旧任务不得删除 journal"
    assert op.get("retired_guarded") is True
    assert op.get("retire_note") == "人工核对云端后退出旧任务，保留同表同日防重闸门"
    assert any(record.get("retired_guarded") is True
               for record in op["sheets"].values())
    assert reloaded.has_guard(TARGET.isoformat(), "F1") is True
    guard_status = recovery_status(SyncLedger(ledger_path))
    assert guard_status["next_action"] == "manual_reconcile"
    assert guard_status["counts"]["retired_guarded"] == 1

    # 同一目标日期/表仍被防重闸门阻断，即使计划已经没有目标列。
    cli_before = dict(cli.grid)
    blocked2 = apply_plan(cli, same_target_plan, ledger=SyncLedger(ledger_path),
                          marker_enabled=False)
    assert blocked2["status"] != "ok"
    assert cli.grid == cli_before and cli.writes == []

    # 其他日期不受旧任务的闸门影响。
    other_date = dt.date(2026, 9, 15)
    cli.grid[(1, 4)] = "9.15 周二"
    other_plan = build_plan(
        cli, local_orders={"东湖中餐": orders},
        tables={"东湖中餐": {"file_id": "F1"}},
        target=other_date, ledger=SyncLedger(ledger_path), marker_enabled=False,
        address_order={}, sort_enabled=False,
    )
    writes_before = len(cli.writes)
    other_result = apply_plan(cli, other_plan, ledger=SyncLedger(ledger_path),
                              marker_enabled=False)
    assert other_result["status"] == "ok"
    assert len(cli.writes) > writes_before, "新日期应能在旧任务退出后正常写入"

def test_w2_unknown_operation_status_alone_blocks_and_query_is_uncertain(tmp_path):
    """只有 op 级状态未知、sheet 看似 verified 时，写闸门和查询仍必须 uncertain。"""
    cli = GridCli(make_grid(rows=[{0: "张", 2: "111", 7: "3"}]))
    ledger = SyncLedger(tmp_path / "state.json")
    plan = _plan(cli, _orders(6), ledger)
    journal_path = ledger.journal_path
    payload = {
        "version": 1,
        "operations": {
            "op-unknown-only": {
                "operation_id": "op-unknown-only",
                "target_date": TARGET.isoformat(),
                "created_at": "2026-09-19T00:00:00.000000",
                "updated_at": "2026-09-19T00:00:00.000000",
                "status": "awaiting_cloud_v2",
                "next_action": "manual_reconcile",
                "sheets": {
                    "s": {
                        "sheet": "东湖中餐", "file_id": "F1",
                        "target_date": TARGET.isoformat(),
                        "status": "verified",
                        "intents": [],
                    },
                },
            },
        },
    }
    journal_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    grid_before = dict(cli.grid)

    result = apply_plan(cli, plan, ledger=ledger, marker_enabled=False)

    assert result["status"] != "ok"
    assert result["written"] == 0 and cli.writes == []
    assert cli.grid == grid_before
    status = recovery_status(ledger)
    assert status["next_action"] == "manual_reconcile"
    assert status["counts"]["uncertain"] == 1

    still_blocked = resolve_pending_operation(
        SyncLedger(ledger.path), "op-unknown-only", "cloud_verified",
        cli=cli, confirm_structure_checked=True)
    assert still_blocked["ok"] is False
    assert still_blocked["reason"] == "unsupported_status_requires_manual"

    retired = resolve_pending_operation(
        SyncLedger(ledger.path), "op-unknown-only", "retire_guarded",
        cli=cli, confirm_structure_checked=True, note="未知状态保留审计后 guarded retire")
    assert retired["ok"] is True and retired["status"] == "retired_guarded"
    reloaded = SyncJournal(journal_path)
    assert reloaded.has_guard(TARGET.isoformat(), "F1") is True
    assert "op-unknown-only" not in reloaded.pending_operations(), \
        "guarded retire 后不应再阻塞其他目标日期"
    raw = reloaded.get_operation("op-unknown-only")
    assert raw["sheets"]["s"]["status"] == "verified", "sheet 原始状态审计值不能被覆盖"
