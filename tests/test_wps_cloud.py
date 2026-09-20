"""Tests for app.wps.sync —— 云文档同步（全部离线，不联网、不写云端）。

覆盖：目标日期规则、表头解析、人员匹配、总餐次累加语义（云端 + 本次增量）、幂等、
列定位（含协作者写错星期的情况）、写入与回读校验、错误处理。
"""
from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path

import pytest

from app.wps.sync import (
    Change, CloudOrder, LedgerCorruptError, SheetPlan, SyncLedger, WpsCloudError,
    apply_plan, build_plan, find_cli, format_plan, parse_date_header,
    person_key, summarize_plan, target_date_for, weekday_number,
)
from app.wps import sync as wc
from app.wps import cli as wps_cli

# ----------------------------------------------------------------------
# 测试替身
# ----------------------------------------------------------------------


class FakeCli:
    """KdocsCli 的替身：用内存网格模拟一张云端表。"""

    def __init__(self, grid: dict[tuple[int, int], str], *,
                 fail_write: bool = False, corrupt_write: bool = False):
        self.grid = dict(grid)          # (0-based 行, 0-based 列) -> 文本
        self.fail_write = fail_write
        self.corrupt_write = corrupt_write
        self.writes: list[dict] = []
        self.inserts: list[tuple[int, int]] = []
        self.format_ops: list[list[dict]] = []
        self.sorts: list[dict] = []
        self.deleted_columns: list[tuple[int, int]] = []
        self.path = "/fake/kdocs-cli"

    def sheets_info(self, file_id: str):
        return [{"sheetId": 1, "sheetName": "Sheet1", "rowTo": 200, "colTo": 50}]

    def read_grid(self, file_id, worksheet_id, row_from, row_to, col_from, col_to,
                  *, with_format: bool = False):
        hits = {k: v for k, v in self.grid.items()
                if row_from <= k[0] <= row_to and col_from <= k[1] <= col_to}
        if with_format:
            # 与真实接口一致：只返回非空格；fill 模拟为空（模板测试里由 TemplateCli 覆盖）
            return {k: {"text": str(v), "fill": ""} for k, v in hits.items()}
        return hits

    def write_cells(self, file_id, worksheet_id, cells):
        if self.fail_write:
            raise WpsCloudError("模拟写入失败")
        self.writes.append({"file_id": file_id, "cells": list(cells)})
        if self.corrupt_write:
            return                      # 假装成功，但什么都不改（用于验证回读校验）
        for cell in cells:
            self.grid[(int(cell["row"]) - 1, int(cell["col"]) - 1)] = str(cell["value"])

    def read_formulas(self, file_id, worksheet_id, row_from, row_to, col_from, col_to):
        result = {}
        for key, value in self.grid.items():
            if row_from <= key[0] <= row_to and col_from <= key[1] <= col_to and str(value).startswith("="):
                result[key] = str(value)
        return result

    def insert_rows(self, file_id, worksheet_id, *, row, count):
        """在 1-based 行号 row 前插 count 行：0-based ≥ row-1 的内容整体下移。"""
        self.inserts.append((row, count))
        shifted = {}
        for (r, c), v in self.grid.items():
            shifted[(r + count if r >= row - 1 else r, c)] = v
        self.grid = shifted

    def delete_rows(self, file_id, worksheet_id, *, row, count):
        """删除 1-based 行号 row 起 count 行：中间内容移除、下方上移。"""
        lo, hi = row - 1, row - 1 + count - 1
        shifted = {}
        for (r, c), v in self.grid.items():
            if r < lo:
                shifted[(r, c)] = v
            elif r > hi:
                shifted[(r - count, c)] = v
        self.grid = shifted

    def write_format_ops(self, file_id, worksheet_id, ops):
        self.format_ops.append([dict(op) for op in ops])

    @staticmethod
    def _col_index(letter: str) -> int:
        value = 0
        for char in str(letter).upper():
            value = value * 26 + (ord(char) - 64)
        return value - 1

    def sort_range(self, file_id, worksheet_id, *, range_ref, key, order="asc",
                   header=False, key2=None, order2=None):
        """模拟云端原地排序：按辅助列**稳定**排序区域内所有列。

        真实接口承诺"保留格式与公式、等键保持源顺序"，这里至少要把"行整体搬动、
        所有列一起走"这条语义模拟对 —— 单元格错位是最危险的失败模式。
        """
        import re as _re
        self.sorts.append({"range": range_ref, "key": key, "order": order,
                           "header": header})
        match = _re.match(r"^([A-Z]+)(\d+):([A-Z]+)(\d+)$", str(range_ref))
        assert match, f"排序区域格式非法：{range_ref}"
        c0 = self._col_index(match.group(1))
        r0 = int(match.group(2))
        c1 = self._col_index(match.group(3))
        r1 = int(match.group(4))
        first = r0 - 1 + (1 if header else 0)          # 0-based 首行
        last = r1 - 1                                   # 0-based 末行
        key_col = self._col_index(key)

        def sort_key(row: int):
            raw = str(self.grid.get((row, key_col), "") or "").strip()
            # 空键排最后；键是零填充字符串，字典序即数值序
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

    def delete_columns(self, file_id, worksheet_id, *, column, rows):
        """删除一整列（左移）；排序辅助列的收尾清理。"""
        self.deleted_columns.append((column, rows))
        col = column - 1
        shifted = {}
        for (r, c), v in self.grid.items():
            if c == col:
                continue
            shifted[(r, c - 1 if c > col else c)] = v
        self.grid = shifted

    def authenticated(self) -> bool:
        return True

    # ---- 格式接口（默认：读不到参考格式，于是跳过格式设置）----
    def read_cell_format(self, file_id, worksheet_id, row, col):
        return None


class BogusWidthCli(FakeCli):
    """docs: sheetsInfo.colTo 报成网格最右列（16383），真实内容只有十来列。"""

    def sheets_info(self, file_id: str):
        info = dict(super().sheets_info(file_id)[0])
        info["colTo"] = 16383
        return [info]


def make_grid(header: dict[int, str], rows: list[dict[int, str]]) -> dict[tuple[int, int], str]:
    """header/rows 的键都是 0-based 列号；第 1 行标题、第 2 行表头、第 3 行起数据。"""
    grid: dict[tuple[int, int], str] = {}
    for col, text in header.items():
        grid[(1, col)] = text
    for idx, row in enumerate(rows, start=2):
        for col, text in row.items():
            if text not in (None, ""):
                grid[(idx, col)] = str(text)
    return grid


BASE_HEADER = {0: "名字", 1: "地址", 2: "电话", 3: "9.10 周四",
               4: "9.11 周五", 5: "类型", 6: "餐种", 7: "总餐次",
               8: "已出餐", 9: "剩余餐", 10: "备注", 12: "9.11 周五"}


# ----------------------------------------------------------------------
# 目标日期与记号
# ----------------------------------------------------------------------

@pytest.mark.parametrize("hour,expect_day", [
    (18, 11), (19, 11),          # 白天/傍晚：写当天
    (20, 12), (21, 12), (23, 12),  # 晚上：写次日
    (0, 12), (5, 12), (9, 12),   # 凌晨：写次日
    (10, 11), (12, 11),          # 上午 10 点后：写当天
])
def test_target_date_window(hour: int, expect_day: int):
    now = dt.datetime(2026, 9, 11, hour, 0)
    assert target_date_for(now).day == expect_day


def test_target_date_custom_window():
    now = dt.datetime(2026, 9, 11, 19, 0)
    assert target_date_for(now, start_hour=18, end_hour=6).day == 12
    assert target_date_for(now, start_hour=22, end_hour=6).day == 11


@pytest.mark.parametrize("day,expect", [
    (dt.date(2026, 9, 13), 1),   # 周日
    (dt.date(2026, 9, 14), 2),   # 周一
    (dt.date(2026, 9, 18), 6),   # 周五
    (dt.date(2026, 9, 19), 7),   # 周六
])
def test_weekday_number(day: dt.date, expect: int):
    assert weekday_number(day) == expect


# ----------------------------------------------------------------------
# 表头解析
# ----------------------------------------------------------------------

@pytest.mark.parametrize("text,expect", [
    ("9.11 周五", (9, 11)),
    ("9.11周五", (9, 11)),        # 无空格
    ("9.9 周三", (9, 9)),
    ("3.16周一", (3, 16)),        # 协作者漏空格
    ("12.31 周四", (12, 31)),
    ("总餐次", None),
    ("", None),
    (None, None),
    ("13.40", None),              # 非法月日
])
def test_parse_date_header(text, expect):
    assert parse_date_header(text) == expect


def test_find_target_column_ignores_written_weekday():
    """协作者把 9.16 写成「9.16周一」，但 2026-09-16 其实是周三；只认月.日。"""
    header = {0: "名字", 3: "9.16周一", 4: "9.17周二"}
    found = wc.find_target_column(header, dt.date(2026, 9, 16))
    assert found is not None and found[0] == 3


def test_person_key_normalizes_phone():
    assert person_key("张", 19730037965) == ("张", "19730037965")
    assert person_key(" 张 ", "19730037965.0") == ("张", "19730037965")
    assert person_key("张", "") == ("张", "")
    assert person_key(None, None) == ("", "")


# ----------------------------------------------------------------------
# 计划：匹配、总餐次累加（云端 + 本次增量）、幂等
# ----------------------------------------------------------------------

def _plan(cli, orders, target=dt.date(2026, 9, 11), ledger=None, marker=True,
          run_date=None, address_order=None, sort=True):
    tables = {"东湖中餐": {"file_id": "F1"}}
    return build_plan(cli, local_orders={"东湖中餐": orders}, tables=tables,
                      target=target, ledger=ledger, marker_enabled=marker,
                      run_date=run_date,
                      address_order={} if address_order is None else address_order,
                      sort_enabled=sort)


def test_new_customer_is_inserted_below_header():
    """新客户插到第 3 行与第 4 行之间（关闭排序时就是第 4 行）。"""
    cli = FakeCli(make_grid(BASE_HEADER, [
        {0: "Ichi", 2: "111", 7: "3"},
    ]))
    orders = [CloudOrder("东湖中餐", "新人", "小", "222", "中餐", "经济", 6)]
    plans = _plan(cli, orders, sort=False)
    changes = plans[0].changes
    assert len(changes) == 1 and changes[0].kind == "new"
    assert changes[0].total_after == 6 and changes[0].row == 4
    assert [b.position for b in plans[0].insert_blocks] == [4]


def test_existing_customer_total_accumulates():
    """总餐次 = 云端现值 + 本次本地餐次（线上案例：金小雅 5 + 6 = 11）。

    旧写法写的是"本地值的绝对值"，把云端已有的 5 直接覆盖成 6 —— 顾客已经
    付过钱的 5 餐凭空消失，剩余餐从 6 变成 1。
    """
    cli = FakeCli(make_grid(BASE_HEADER, [
        # 真实老行：类型/餐种/已出餐/剩余餐都有值，所以不需要"补全"
        {0: "张", 2: "111", 4: "1", 5: "中餐", 6: "经济", 7: "5", 8: "=SUM(D3)", 9: "=H3-I3"},
    ]))
    orders = [CloudOrder("东湖中餐", "张", "小", "111", "中餐", "经济", 6, rows=(19,))]
    change = _plan(cli, orders)[0].changes[0]
    assert change.kind == "existing"
    assert (change.total_before, change.total_after) == (5, 11)
    assert (change.local_meals, change.ledger_prev) == (6, None)
    assert change.local_rows == (19,)
    assert change.needs_write is True


def test_total_written_when_mismatch():
    cli = FakeCli(make_grid(BASE_HEADER, [
        {0: "张", 2: "111", 7: "3"},        # 云端 3，本地这次又下了 6 餐
    ]))
    orders = [CloudOrder("东湖中餐", "张", "小", "111", "中餐", "经济", 6)]
    change = _plan(cli, orders)[0].changes[0]
    assert change.total_after == 9 and change.total_before == 3
    assert change.needs_write is True


def test_same_batch_uploaded_twice_only_adds_once(tmp_path):
    """幂等：账本记住"本批已同步的本地餐次"，第二次上传一个格子都不写。"""
    led = SyncLedger(tmp_path / "state.json")
    cli = FakeCli(make_grid(BASE_HEADER, [{0: "张", 2: "111", 7: "5"}]))
    orders = [CloudOrder("东湖中餐", "张", "小", "111", "中餐", "经济", 6)]
    first = apply_plan(cli, _plan(cli, orders, ledger=led), ledger=led,
                       marker_enabled=False)
    assert first["failed"] == 0
    assert cli.grid[(2, 7)] == "11", "云端 5 + 本地 6 = 11"
    assert led.synced_local("2026-09-11", "F1", "张", "111") == 6
    assert led.synced_total("2026-09-11", "F1", "张", "111") == 11

    before = dict(cli.grid)
    second = apply_plan(cli, _plan(cli, orders, ledger=led), ledger=led,
                        marker_enabled=False)
    assert cli.grid == before, "同一批重复上传必须零副作用"
    assert second["sheets"][0]["status"] in {"noop", "ok"}


def test_local_meal_growth_only_adds_the_difference(tmp_path):
    """同一批里后补了一单：只补差额（账本 6 → 本地 8 = +2）。"""
    led = SyncLedger(tmp_path / "state.json")
    led.record("2026-09-11", "F1", {"张\u0000111": {"local": 6, "total": 11}})
    cli = FakeCli(make_grid(BASE_HEADER, [{0: "张", 2: "111", 4: "1", 7: "11"}]))
    orders = [CloudOrder("东湖中餐", "张", "小", "111", "中餐", "经济", 8)]
    change = _plan(cli, orders, ledger=led)[0].changes[0]
    assert (change.total_before, change.total_after) == (11, 13)
    assert change.ledger_prev == 6 and change.local_meals == 8


def test_legacy_ledger_entry_counts_as_already_synced(tmp_path):
    """旧版账本（绝对值时代）里的 meals = 当时的本地餐次 → 不重复加。

    线上真实文件：``~/.config/yikou-light-food/wps_sync_state.json`` 里就有这种
    ``{"meals": 6}`` 条目（旧代码把总餐次覆盖成 6 之后记下的）。升级后第一次上传
    必须据此算出 +0，否则今晚这批会被再加一遍。
    """
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"version": 1, "batches": {"2026-09-11": {"F1": {
        "synced_at": "2026-09-11T21:00:00",
        "people": {"张\u0000111": {"meals": 6, "at": "2026-09-11T21:00:00"}}}}}},
        ensure_ascii=False), encoding="utf-8")
    cli = FakeCli(make_grid(BASE_HEADER, [
        {0: "张", 2: "111", 4: "1", 5: "中餐", 6: "经济", 7: "6", 8: "=SUM(D3)", 9: "=H3-I3"},
    ]))
    orders = [CloudOrder("东湖中餐", "张", "小", "111", "中餐", "经济", 6)]
    change = _plan(cli, orders, ledger=SyncLedger(path))[0].changes[0]
    assert change.ledger_prev == 6
    assert change.needs_write is False, "本批已同步过：日期格是 1、总餐次也一致"


def test_local_meal_shrink_never_decreases_the_cloud_total(tmp_path):
    """退单（本地比账本少）不自动回退，只警告，避免误删已付款的餐。"""
    led = SyncLedger(tmp_path / "state.json")
    led.record("2026-09-11", "F1", {"张\u0000111": {"local": 6, "total": 11}})
    cli = FakeCli(make_grid(BASE_HEADER, [{0: "张", 2: "111", 4: "1", 7: "11"}]))
    orders = [CloudOrder("东湖中餐", "张", "小", "111", "中餐", "经济", 1)]
    plan = _plan(cli, orders, ledger=led)[0]
    change = plan.changes[0]
    assert change.total_after == 11, "只加不减"
    assert any("可能退单" in w for w in plan.warnings)


def test_collaborator_zero_cell_is_not_overwritten():
    """协作者在日期格里写了 0（当天不送）：那一格一个字节都不改，总餐次照加。

    线上案例：协作者把 9.19 那格写成 0，旧程序改成 1 → 「闪时送下单」会多送一餐。
    """
    cli = FakeCli(make_grid(BASE_HEADER, [
        {0: "张", 2: "111", 4: "0", 5: "中餐", 6: "经济", 7: "5",
         8: "=SUM(D3)", 9: "=H3-I3"},
    ]))
    orders = [CloudOrder("东湖中餐", "张", "小", "111", "中餐", "经济", 6)]
    plans = _plan(cli, orders)
    change = plans[0].changes[0]
    assert change.target_occupied == "0" and change.target_blocked
    assert change.total_after == 11
    assert any("不改这一格" in w for w in plans[0].warnings)
    assert any("改回 1" in w for w in plans[0].warnings)

    result = apply_plan(cli, plans, ledger=None, marker_enabled=False)

    assert result["failed"] == 0, "保留协作者写的 0 不算校验失败"
    assert cli.grid[(2, 4)] == "0", "协作者写的 0 必须原样保留"
    assert cli.grid[(2, 7)] == "11"
    assert summarize_plan(plans)["skipped"] == 1


def test_blocked_cell_with_nothing_else_to_write_makes_no_call(tmp_path):
    """日期格被协作者占用 + 总餐次已一致 → 一个写入接口都不调。"""
    led = SyncLedger(tmp_path / "state.json")
    led.record("2026-09-11", "F1", {"张\u0000111": {"local": 6, "total": 11}})
    cli = FakeCli(make_grid(BASE_HEADER, [
        {0: "张", 2: "111", 4: "0", 5: "中餐", 6: "经济", 7: "11",
         8: "=SUM(D3)", 9: "=H3-I3"},
    ]))
    orders = [CloudOrder("东湖中餐", "张", "小", "111", "中餐", "经济", 6)]
    plans = _plan(cli, orders, ledger=led)
    assert plans[0].changes[0].needs_write is False
    apply_plan(cli, plans, ledger=led, marker_enabled=False)
    assert cli.writes == []


def test_manual_repair_of_the_cloud_value_is_never_overwritten(tmp_path):
    """线上收尾场景：用户手工把云端金小雅的总餐次改成 11 之后再上传，一个字都不写。

    背景：旧版今晚已经把她写成 6（云端 5 ➜ 6），用户手工改回 11；账本里记着
    本批「本地 6 餐」（新版把旧账本的 ``meals`` 当已同步本地餐次读）。
    增量 = 6 − 6 = 0 → 总餐次不动；日期格协作者写的是 0 → 也不动。
    """
    led = SyncLedger(tmp_path / "state.json")
    led.record("2026-09-11", "F1",
               {"金小雅\u000013175798579": {"local": 6, "total": 6}})
    cli = FakeCli(make_grid(BASE_HEADER, [
        {0: "金小雅", 1: "D2", 2: "13175798579", 4: "0", 5: "中餐", 6: "经济",
         7: "11", 8: "=SUM(D3)", 9: "=H3-I3"},
    ]))
    orders = [CloudOrder("东湖中餐", "金小雅", "D2", "13175798579", "中餐", "经济", 6,
                         row=19, order_no="W17", rows=(19,),
                         weekday_marks=("周五",))]
    plans = _plan(cli, orders, ledger=led)
    change = plans[0].changes[0]
    assert change.ledger_prev == 6 and change.total_after == 11
    assert change.needs_write is False, "既不用改总餐次，也不用改日期格"

    apply_plan(cli, plans, ledger=led, marker_enabled=False)

    assert cli.writes == [], "一个字都不该写"
    assert cli.grid[(2, 4)] == "0", "协作者写的 0 保持原样"
    assert cli.grid[(2, 7)] == "11", "用户手工改的 11 不能被改回去"


def test_local_row_with_blank_meals_is_skipped():
    """本地「餐次」是空的（按 0 计）：既不写日期格（白送一餐），也不动总餐次。"""
    cli = FakeCli(make_grid(BASE_HEADER, [{0: "张", 2: "111", 7: "5"}]))
    orders = [CloudOrder("东湖中餐", "张", "小", "111", "中餐", "经济", 0, rows=(7,))]
    plan = _plan(cli, orders)[0]
    assert plan.changes == []
    assert any("餐次」是空的" in w for w in plan.warnings)


def test_two_orders_in_one_night_become_two_cloud_rows():
    """一晚两单（线上案例：雷丝淇 W25 + W5，各 1 餐）→ 云端两行、各标当天 1。

    用户 2026-09-18 明确："分开写成两行" —— 这样「闪时送下单」
    （按"日期格 == 1"出单、每单固定 1 份）会下两单、这天真的送两餐。
    """
    cli = FakeCli(make_grid(BASE_HEADER, [{0: "雷丝淇", 1: "小", 2: "111", 7: "5"}]))
    orders = [
        CloudOrder("东湖中餐", "雷丝淇", "小", "111", "中餐", "经济", 1,
                   row=4, order_no="W25", rows=(4,), weekday_marks=("周五",)),
        CloudOrder("东湖中餐", "雷丝淇", "小", "111", "中餐", "经济", 1,
                   row=5, order_no="W5", rows=(5,), weekday_marks=("周五",)),
    ]
    plans = _plan(cli, orders, sort=False)
    plan = plans[0]
    slots = sorted(plan.changes, key=lambda c: c.slot)
    assert [c.slot for c in slots] == [1, 2]
    assert slots[0].kind == "existing" and slots[0].total_after == 6, "云端 5 + 第 1 行 1 餐"
    assert slots[1].kind == "new" and slots[1].total_after == 1, "第 2 行是新建的一行"
    assert [c.local_meals for c in slots] == [1, 1]
    assert any("云端写 2 行" in w for w in plan.warnings)

    apply_plan(cli, plans, ledger=None, marker_enabled=False)

    rows = [r for r in range(2, 7) if cli.grid.get((r, 0)) == "雷丝淇"]
    assert len(rows) == 2, "云端要有她两行"
    assert all(cli.grid[(row, 4)] == "1" for row in rows), "两行都标当天"
    assert sorted(int(cli.grid[(row, 7)]) for row in rows) == [1, 6]


def test_one_order_with_two_portions_becomes_two_cloud_rows():
    """一单两份被 runner 写成两行（线上案例：柟 W33 两行）→ 同样云端两行。"""
    cli = FakeCli(make_grid(BASE_HEADER, [{0: "柟", 1: "小", 2: "111", 7: "5"}]))
    orders = [
        CloudOrder("东湖中餐", "柟", "小", "111", "中餐", "经济", 1,
                   row=3, order_no="W33", rows=(3,), weekday_marks=("周五",)),
        CloudOrder("东湖中餐", "柟", "小", "111", "中餐", "经济", 1,
                   row=4, order_no="W33", rows=(4,), weekday_marks=("周五",)),
    ]
    plans = _plan(cli, orders, sort=False)
    slots = sorted(plans[0].changes, key=lambda c: c.slot)
    assert [c.local_meals for c in slots] == [1, 1]
    assert slots[1].kind == "new" and slots[1].total_after == 1

    apply_plan(cli, plans, ledger=None, marker_enabled=False)

    rows = [r for r in range(2, 7) if cli.grid.get((r, 0)) == "柟"]
    assert len(rows) == 2
    assert all(cli.grid[(row, 4)] == "1" for row in rows)


def test_second_cloud_row_of_the_same_person_is_updated_in_place():
    """云端本来就有他两行（上次同步建的第二行）→ 两行各自累加，不再新建。"""
    cli = FakeCli(make_grid(BASE_HEADER, [
        {0: "柟", 1: "小", 2: "111", 4: "1", 5: "中餐", 6: "经济", 7: "5"},
        {0: "柟", 1: "小", 2: "111", 4: "1", 5: "中餐", 6: "经济", 7: "1"},
    ]))
    orders = [
        CloudOrder("东湖中餐", "柟", "小", "111", "中餐", "经济", 1,
                   row=3, rows=(3,), weekday_marks=("周五",)),
        CloudOrder("东湖中餐", "柟", "小", "111", "中餐", "经济", 1,
                   row=4, rows=(4,), weekday_marks=("周五",)),
    ]
    plan = _plan(cli, orders, sort=False)[0]
    slots = sorted(plan.changes, key=lambda c: c.slot)
    assert [c.kind for c in slots] == ["existing", "existing"]
    assert [c.total_after for c in slots] == [6, 2]
    assert plan.insert_blocks == [], "两行都在云端了，不用插行"

    apply_plan(cli, [plan], ledger=None, marker_enabled=False)

    assert cli.grid[(2, 7)] == "6" and cli.grid[(3, 7)] == "2"


def test_two_row_person_is_idempotent_across_uploads(tmp_path):
    """两行的人重复上传：账本按槽位记住已同步餐次 → 一个格子都不写、不重复建行。"""
    led = SyncLedger(tmp_path / "state.json")
    cli = FakeCli(make_grid(BASE_HEADER, [{0: "柟", 1: "小", 2: "111", 7: "5"}]))
    orders = [
        CloudOrder("东湖中餐", "柟", "小", "111", "中餐", "经济", 1,
                   row=3, rows=(3,), weekday_marks=("周五",)),
        CloudOrder("东湖中餐", "柟", "小", "111", "中餐", "经济", 1,
                   row=4, rows=(4,), weekday_marks=("周五",)),
    ]
    apply_plan(cli, _plan(cli, orders, ledger=led, sort=False), ledger=led,
               marker_enabled=False)
    assert led.synced_slots("2026-09-11", "F1", "柟", "111") == [1, 1]
    assert led.synced_local("2026-09-11", "F1", "柟", "111") == 2

    before = dict(cli.grid)
    result = apply_plan(cli, _plan(cli, orders, ledger=led, sort=False), ledger=led,
                        marker_enabled=False)

    assert cli.grid == before, "同一批第二次上传必须零副作用"
    assert result["sheets"][0]["status"] in {"noop", "ok"}


def test_an_extra_order_adds_only_the_second_row(tmp_path):
    """同一批里他又下了一单：第 1 行不动（账本说已同步），只补第 2 行。"""
    led = SyncLedger(tmp_path / "state.json")
    led.record("2026-09-11", "F1",
               {"柟\u0000111": {"local": 1, "slots": [1], "total": 6}})
    cli = FakeCli(make_grid(BASE_HEADER, [
        {0: "柟", 1: "小", 2: "111", 4: "1", 5: "中餐", 6: "经济", 7: "6",
         8: "=SUM(D3:E3)", 9: "=H3-I3"},
    ]))
    orders = [
        CloudOrder("东湖中餐", "柟", "小", "111", "中餐", "经济", 1,
                   row=3, rows=(3,), weekday_marks=("周五",)),
        CloudOrder("东湖中餐", "柟", "小", "111", "中餐", "经济", 1,
                   row=4, rows=(4,), weekday_marks=("周五",)),
    ]
    plan = _plan(cli, orders, ledger=led, sort=False)[0]
    slots = sorted(plan.changes, key=lambda c: c.slot)
    assert slots[0].needs_write is False, "第 1 行本批已同步过"
    assert slots[1].kind == "new" and slots[1].total_after == 1
    assert len(plan.insert_blocks) == 1 and plan.insert_blocks[0].count == 1


def test_two_rows_are_written_after_the_sort():
    """整表重排后，同一个人的第二行也要写在它排序后的**实际**行号上。"""
    cli = FakeCli(make_grid(BASE_HEADER, [
        {0: "张", 1: "b2", 2: "111", 7: "5"},
        {0: "李", 1: "小", 2: "222", 7: "5"},
    ]))
    orders = [
        CloudOrder("东湖中餐", "张", "b2", "111", "中餐", "经济", 1,
                   row=3, rows=(3,), weekday_marks=("周五",)),
        CloudOrder("东湖中餐", "张", "b2", "111", "中餐", "经济", 1,
                   row=4, rows=(4,), weekday_marks=("周五",)),
    ]
    plan = _plan(cli, orders, address_order={"东湖中餐": ["小", "b2"]})[0]

    apply_plan(cli, [plan], ledger=None, marker_enabled=False)

    rows = [r for r in range(2, 7) if cli.grid.get((r, 0)) == "张"]
    assert len(rows) == 2
    assert all(cli.grid[(row, 4)] == "1" for row in rows)
    assert sorted(int(cli.grid[(row, 7)]) for row in rows) == [1, 6]


def test_multiple_rows_with_conflicting_kind_warn():
    """同一人两行的餐种不一致时不猜：按第一行写 + 警告。"""
    cli = FakeCli(make_grid(BASE_HEADER, [{0: "张", 2: "111", 7: "5"}]))
    orders = [
        CloudOrder("东湖中餐", "张", "小", "111", "中餐", "豪华", 1,
                   row=3, rows=(3,)),
        CloudOrder("东湖中餐", "张", "小", "111", "中餐", "经济", 1,
                   row=4, rows=(4,)),
    ]
    plan = _plan(cli, orders)[0]
    assert any("不一致" in w and "餐种" in w for w in plan.warnings)


# ----------------------------------------------------------------------
# 批次日期核对（本地表的星期标记 vs 目标日期）
# ----------------------------------------------------------------------

def test_batch_date_mismatch_refuses_the_whole_sheet():
    """本地表是别的日期的批次：整张表拒绝写入，连云端都不读、记号也不写。"""
    cli = FakeCli(make_grid(BASE_HEADER, [{0: "张", 2: "111", 7: "5"}]))
    reads: list[str] = []
    original = cli.sheets_info
    cli.sheets_info = lambda file_id: reads.append(file_id) or original(file_id)
    orders = [CloudOrder("东湖中餐", "张", "小", "111", "中餐", "经济", 6,
                         weekday_marks=("周四",))]        # 目标 9.11 是周五
    plans = _plan(cli, orders, marker=True)
    plan = plans[0]
    assert plan.blocked_reason and not plan.changes
    assert not plan.target_col and reads == [], "被拒绝的子表一个云端请求都不发"
    assert any("别的日期" in w for w in plan.warnings)

    result = apply_plan(cli, plans, ledger=None, marker_enabled=True)

    assert result["sheets"][0]["status"] == "stale_batch"
    assert result["failed"] == 1
    assert cli.writes == [] and cli.inserts == [] and cli.sorts == []
    assert (1, 13) not in cli.grid, "通讯记号也不能写"


def test_partially_stale_rows_only_warn():
    cli = FakeCli(make_grid(BASE_HEADER, [{0: "张", 2: "111", 7: "5"}]))
    orders = [
        CloudOrder("东湖中餐", "张", "小", "111", "中餐", "经济", 1,
                   weekday_marks=("周五",)),
        CloudOrder("东湖中餐", "李", "小", "222", "中餐", "经济", 1,
                   weekday_marks=("周四",)),
    ]
    plan = _plan(cli, orders)[0]
    assert not plan.blocked_reason
    assert any("可能来自别的批次" in w for w in plan.warnings)


def test_rows_without_weekday_marks_warn_but_are_allowed():
    """人工做的本地表（没有星期标记）：无法核对 → 只警告，照常写入。"""
    cli = FakeCli(make_grid(BASE_HEADER, [{0: "张", 2: "111", 7: "5"}]))
    orders = [CloudOrder("东湖中餐", "张", "小", "111", "中餐", "经济", 6)]
    plan = _plan(cli, orders)[0]
    assert not plan.blocked_reason
    assert any("无法核对批次日期" in w for w in plan.warnings)


def test_match_requires_both_name_and_phone():
    """同名不同电话必须视为两个人（不会写错行）。"""
    cli = FakeCli(make_grid(BASE_HEADER, [
        {0: "李", 2: "111", 7: "5"},
    ]))
    orders = [CloudOrder("东湖中餐", "李", "小", "999", "中餐", "经济", 2)]
    change = _plan(cli, orders)[0].changes[0]
    assert change.kind == "new"


def test_missing_target_column_blocks_write():
    header = dict(BASE_HEADER)
    header.pop(4)                       # 去掉 9.11 列
    header.pop(12)                      # 通讯记号区的同名表头也去掉
    cli = FakeCli(make_grid(header, [{0: "张", 2: "111", 7: "6"}]))
    orders = [CloudOrder("东湖中餐", "张", "小", "111", "中餐", "经济", 6)]
    plan = _plan(cli, orders)[0]
    assert plan.target_col == 0
    assert any("没有 9.11" in w for w in plan.warnings)
    assert plan.changes == []


def test_broken_header_blocks_write():
    """表头被改乱（例如「类型」列表头被覆盖成日期）时必须拒绝写入。

    真实事故：测试副本的第 7 列（类型）表头被写成了「9.12 周六」，
    程序当时照样往那一列写数字 —— 虽然位置凑巧正确，但往看不懂的表里
    写数据本身就不该发生。
    """
    header = dict(BASE_HEADER)
    header[5] = "9.12 周六"          # 覆盖「类型」表头，模拟被改乱
    cli = FakeCli(make_grid(header, [{0: "张", 2: "111", 7: "6"}]))
    orders = [CloudOrder("东湖中餐", "张", "小", "111", "中餐", "经济", 6)]
    plan = _plan(cli, orders)[0]
    assert plan.changes == []
    assert any("表头异常" in w and "类型" in w for w in plan.warnings)


def test_column_lookup_handles_shifted_layout():
    """协作者插列后，工作列整体右移，仍要按表头名找到。"""
    header = {0: "名字", 1: "地址", 2: "电话", 3: "9.10 周四", 4: "9.11 周五",
              5: "9.12 周六", 6: "类型", 7: "餐种", 8: "总餐数", 9: "出餐",
              10: "剩余", 11: "备注"}
    cli = FakeCli(make_grid(header, [{0: "张", 2: "111", 8: "2"}]))
    orders = [CloudOrder("东湖中餐", "张", "小", "111", "中餐", "经济", 6)]
    plan = _plan(cli, orders)[0]
    assert plan.columns["total"] == 9          # 1-based，"总餐数"在第 9 列
    assert plan.target_col == 5                # "9.11 周五"（0-based 4 → 1-based 5）
    assert plan.changes[0].total_after == 8    # 云端 2 + 本地 6


# ----------------------------------------------------------------------
# 写入与校验
# ----------------------------------------------------------------------

def test_apply_plan_writes_and_verifies():
    cli = FakeCli(make_grid(BASE_HEADER, [
        {0: "张", 2: "111", 7: "3"},
    ]))
    orders = [CloudOrder("东湖中餐", "张", "小", "111", "中餐", "经济", 6)]
    plans = _plan(cli, orders)
    result = apply_plan(cli, plans, ledger=None, marker_enabled=False)
    assert result["written"] == 1 and result["failed"] == 0
    # 目标日期列（第 5 列 = index 4）与总餐次（第 8 列 = index 7）都写对了
    assert cli.grid[(2, 4)] == "1"
    assert cli.grid[(2, 7)] == "9", "云端 3 + 本地 6"


def test_apply_plan_detects_silent_write_failure():
    """接口返回成功但内容没变时，回读校验必须发现（曾出现过假阳性）。"""
    cli = FakeCli(make_grid(BASE_HEADER, [
        {0: "张", 2: "111", 7: "3"},
    ]), corrupt_write=True)
    orders = [CloudOrder("东湖中餐", "张", "小", "111", "中餐", "经济", 6)]
    plans = _plan(cli, orders)
    result = apply_plan(cli, plans, ledger=None, marker_enabled=False)
    assert result["failed"] == 1
    assert result["uncertain"] is True
    assert result["sheets"][0]["status"] == "uncertain"
    assert result["sheets"][0]["uncertain"] is True
    assert result["next_action"] == "manual_reconcile"


def test_scan_bounds_respects_read_budget():
    """读取范围必须按"行×列"控制，避免撞 5 万格上限。

    真实事故：`衣锦中餐` 有 121 个日期列，若按 285 行 × 201 列去读
    （= 57285 格）会被接口拒绝，整个预览/上传直接失败。
    """
    # 宽表：列多 -> 行必须被压缩
    row_to, col_to = wc.scan_bounds({"rowTo": 500, "colTo": 199})
    assert (row_to + 1) * (col_to + 1) <= wc.MAX_READ_CELLS
    # 窄表：不受影响
    assert wc.scan_bounds({"rowTo": 103, "colTo": 13}) == (103, 13)
    # 缺字段 / None 不应崩
    assert wc.scan_bounds(None) == (0, 0)


# 模板行的颜色分布（对齐 BASE_HEADER 与真实表）：名字~餐种白、总餐次/已出餐/剩余餐金黄、备注白
TEMPLATE_COLORS = {c: "#FFFFFFFF" for c in range(1, 15)}
for _c in (8, 9, 10):
    TEMPLATE_COLORS[_c] = "#FFFFC000"


class TemplateCli(FakeCli):
    """带"模板行格式"的替身：能学到字体与逐列底色。"""

    def __init__(self, *a, row_colors=None, **kw):
        super().__init__(*a, **kw)
        self.row_colors = dict(row_colors or TEMPLATE_COLORS)

    def read_grid(self, file_id, worksheet_id, row_from, row_to, col_from, col_to,
                  *, with_format: bool = False):
        hits = super().read_grid(file_id, worksheet_id, row_from, row_to,
                                 col_from, col_to)
        if with_format:
            return {k: {"text": str(v),
                        "fill": self.row_colors.get(k[1] + 1, "#FFFFFFFF")}
                    for k, v in hits.items()}
        return hits

    def read_cell_format(self, file_id, worksheet_id, row, col):
        """任何有内容的行都提供格式（与真实接口行为一致）。"""
        if (row - 1, 0) not in self.grid:
            return None
        return {"fonts": {"font_east_asia": "Microsoft YaHei", "size": 10,
                          "color": "#FF000000"},
                "alignment": {"horizontal": "haCenter", "vertical": "vaCenter"},
                "cell_background_color": self.row_colors.get(col, "#FFFFFFFF"),
                "cellText": "参考"}


def test_learn_row_format_copies_font_and_per_column_fills():
    """学模板行：字体/对齐 + **逐列底色**（黄带位置要照抄）。"""
    cli = TemplateCli(make_grid(BASE_HEADER, [{0: "老人", 2: "111", 7: "3"}]))
    spec = wc.learn_row_format(cli, "F1", 1, col_from=1, col_to=14, rows=[3])
    assert spec["font_name"] == "Microsoft YaHei"
    assert spec["font_size"] == 10
    assert spec["alcH"] == 2 and spec["alcV"] == 1
    assert spec["fills"][1] == "#FFFFFFFF"
    assert spec["fills"][8] == "#FFFFC000", "总餐次列必须照抄成黄色"
    assert spec["fills"][9] == "#FFFFC000" and spec["fills"][10] == "#FFFFC000"
    assert spec["fills"][11] == "#FFFFFFFF", "备注列照抄成白色"


def test_learn_row_format_returns_empty_when_no_reference():
    class EmptyCli:
        def read_grid(self, *_a, **_k):
            return {}

        def read_cell_format(self, *_a, **_k):
            return None

    assert wc.learn_row_format(EmptyCli(), "F1", 1, col_from=1, col_to=14, rows=[3]) == {}


def test_new_rows_copy_template_fills():
    """经济餐新行照抄模板底色（黄带位置一致）；豪华餐整行金黄。"""
    # 模板行整行有值（真实接口只返回非空格，底色缓存才覆盖每一列）
    full_row = {0: "老人", 1: "D1", 2: "111", 3: "0", 4: "1", 5: "0", 6: "中餐",
                7: "经济", 8: "3", 9: "1", 10: "2", 11: ""}
    cli = TemplateCli(make_grid(BASE_HEADER, [full_row]))
    orders = [
        CloudOrder("东湖中餐", "经济人", "D2", "13900000001", "中餐", "经济", 1),
        CloudOrder("东湖中餐", "豪华人", "D2", "13900000002", "中餐", "豪华", 1),
    ]
    apply_plan(cli, _plan(cli, orders), ledger=None, marker_enabled=False)
    assert len(cli.format_ops) == 1, "所有格式操作合并后一次调用"
    ops = cli.format_ops[0]
    econ = [op for op in ops if op["rowFrom"] == 3]        # 1-based 第 4 行
    lux = [op for op in ops if op["rowFrom"] == 4]         # 1-based 第 5 行
    assert lux and len(lux) == 1, "豪华餐一整个行区间一个操作"
    assert (lux[0]["colFrom"], lux[0]["colTo"]) == (0, 10), "豪华餐整行（名字到备注列）金黄"
    assert lux[0]["xf"]["fill"]["back"]["value"] == wc.FILL_LUXURY
    white = next(op for op in econ if op["colFrom"] == 0)
    assert white["colTo"] == 6, "第 1~7 列（名字~餐种）合并成一个白底区间"
    assert white["xf"]["fill"]["back"]["value"] == wc._argb_to_int("#FFFFFFFF")
    gold = next(op for op in econ
                if op["xf"]["fill"]["back"]["value"] == wc.FILL_LUXURY)
    assert (gold["colFrom"], gold["colTo"]) == (7, 9), "总餐次~剩余餐 三列金黄"




# 真实表里实测到的写法：日期列与「类型」之间夹着一列空列（正式表 L 列就是它）
GAPPED_HEADER = {0: "名字", 1: "地址", 2: "电话", 3: "9.10 周四", 4: "9.11 周五",
                 # 第 5 列（0-based）= F 列：表头与数据都没有内容
                 6: "类型", 7: "餐种", 8: "总餐次", 9: "已出餐", 10: "剩余餐",
                 11: "备注"}


def test_paint_covers_gap_column_between_dates_and_type():
    """日期与「类型」之间夹着空列时，那一格也必须被涂到。

    用户 2026-09-15 反馈：接口不返回空单元格、读不到它的底色，逐列照抄会漏掉它，
    于是那一格保留了插入时从上一行继承的颜色。现在改成 A~餐种 整段刷。
    """
    cli = TemplateCli(make_grid(GAPPED_HEADER, [
        {0: "老人", 1: "D1", 2: "111", 3: "1", 4: "1",
         6: "中餐", 7: "经济", 8: "3", 9: "1", 10: "2", 11: "备注"},
    ]))
    plans = _plan(cli, [CloudOrder("东湖中餐", "经济人", "D2", "13900000001",
                                   "中餐", "经济", 1)], sort=False)
    plan = plans[0]
    apply_plan(cli, plans, ledger=None, marker_enabled=False)

    ops = [op for op in cli.format_ops[0] if op["rowFrom"] == 3]
    covered = set()
    for op in ops:
        covered.update(range(op["colFrom"] + 1, op["colTo"] + 2))
    want = set(range(1, plan.columns["remark"] + 1))
    assert covered >= want, f"这些列没涂到：{sorted(want - covered)}"

    white = next(op for op in ops if op["colFrom"] == 0)
    assert white["colTo"] + 1 == plan.columns["kind"], "A~餐种 一整段（含空列）刷底色"
    assert white["xf"]["fill"]["back"]["value"] == wc._argb_to_int("#FFFFFFFF")
    gap = next(op for op in ops if op["colFrom"] <= 5 <= op["colTo"])
    assert gap["xf"]["fill"]["back"]["value"] == wc._argb_to_int("#FFFFFFFF"), \
        "空列（第 6 列）不能漏掉"
    gold = next(op for op in ops if op["xf"]["fill"]["back"]["value"] == wc.FILL_LUXURY)
    assert gold["colFrom"] + 1 == plan.columns["total"], "金黄带从总餐次列开始"


# ----------------------------------------------------------------------
# 新客户按地址组插入（排序）
# ----------------------------------------------------------------------

def _grouped_grid():
    return FakeCli(make_grid(BASE_HEADER, [
        {0: "张", 1: "小", 2: "111", 7: "3"},
        {0: "李", 1: "大西", 2: "222", 7: "4"},
        {0: "王", 1: "小", 2: "333", 7: "5"},
    ]))


def test_new_customers_insert_as_single_block_at_row4():
    """所有新客户合成一块，统一插到第 3 行与第 4 行之间。"""
    cli = _grouped_grid()
    orders = [
        CloudOrder("东湖中餐", "新人小", "小", "999", "中餐", "经济", 2),
        CloudOrder("东湖中餐", "新人西", "大西", "888", "中餐", "经济", 1),
    ]
    plan = _plan(cli, orders, sort=False)[0]
    assert [(b.position, b.count, b.first_row) for b in plan.insert_blocks] == [(4, 2, 4)]
    rows = {c.name: c.row for c in plan.changes}
    # plan.changes 只包含本地排单表里的人；张/李/王 不在本地表里，看最终表格
    assert rows == {"新人小": 4, "新人西": 5}
    apply_plan(cli, [plan], ledger=None, marker_enabled=False)
    names = [cli.grid.get((r, 0)) for r in range(2, 8)]
    assert names == ["张", "新人小", "新人西", "李", "王", None], f"实际 {names}"


def test_sort_reorders_whole_table_by_address_order():
    """按列B 的规定顺序重排整张表；清单外的地址排到最后面；组内老行在新行之前。"""
    cli = FakeCli(make_grid(BASE_HEADER, [
        {0: "张", 1: "大西", 2: "111", 7: "3"},
        {0: "李", 1: "小", 2: "222", 7: "3"},
        {0: "王", 1: "学三", 2: "333", 7: "3"},
    ]))
    order = {"东湖中餐": ["小", "大西", "A1"]}
    plan = _plan(cli, [CloudOrder("东湖中餐", "新人", "大西", "444", "中餐", "经济", 1)],
                 address_order=order)[0]
    rows = {c.name: c.row for c in plan.changes}
    assert rows == {"新人": 5}
    assert any("学三" in w and "最后面" in w for w in plan.warnings)
    assert plan.sort_range and plan.sort_key_col

    apply_plan(cli, [plan], ledger=None, marker_enabled=False)
    names = [cli.grid.get((r, 0)) for r in range(2, 6)]
    assert names == ["李", "张", "新人", "王"], f"实际顺序 {names}"
    assert cli.grid[(4, 1)] == "大西", "新人的地址跟着行一起搬到新位置"


def test_empty_order_list_sorts_addresses_naturally():
    """医学院那种「按列B升序」：自然数序 —— 医2号排在医10号之前。"""
    cli = FakeCli(make_grid(BASE_HEADER, [
        {0: "甲", 1: "医10号", 2: "111", 7: "3"},
        {0: "乙", 1: "医2号", 2: "222", 7: "3"},
        {0: "丙", 1: "医1号", 2: "333", 7: "3"},
    ]))
    plan = _plan(cli, [CloudOrder("东湖中餐", "新", "医2号", "444", "中餐", "经济", 1)],
                 address_order={"东湖中餐": []})[0]
    apply_plan(cli, [plan], ledger=None, marker_enabled=False)
    names = [cli.grid.get((r, 0)) for r in range(2, 6)]
    assert names == ["丙", "乙", "新", "甲"], f"实际顺序 {names}"


def test_address_alias_and_case_insensitive_match():
    """本地「小西」按别名进云端「小」组；「B2」忽略大小写匹配，并按清单标准写法落表。"""
    cli = FakeCli(make_grid(BASE_HEADER, [
        {0: "张", 1: "小", 2: "111", 7: "3"},
        {0: "李", 1: "b2", 2: "222", 7: "4"},
    ]))
    orders = [
        CloudOrder("东湖中餐", "黄", "小西", "18377144245", "中餐", "经济", 1),
        CloudOrder("东湖中餐", "陈章依", "B2", "15395721282", "中餐", "豪华", 1),
    ]
    plan = _plan(cli, orders, address_order={"东湖中餐": ["小", "b2"]})[0]
    addrs = {c.name: c.address for c in plan.changes}
    assert addrs == {"黄": "小", "陈章依": "b2"}
    assert not [w for w in plan.warnings if "不在排序清单" in w]
    apply_plan(cli, [plan], ledger=None, marker_enabled=False)
    names = [cli.grid.get((r, 0)) for r in range(2, 6)]
    assert names == ["张", "黄", "李", "陈章依"], f"实际顺序 {names}"
    assert cli.grid[(5, 1)] == "b2", "本地写 B2，云端按清单落成 b2"


def test_unknown_address_goes_to_table_end_with_warning():
    cli = FakeCli(make_grid(BASE_HEADER, [{0: "张", 1: "小", 2: "111", 7: "3"}]))
    orders = [CloudOrder("东湖中餐", "路人", "食堂", "555", "中餐", "经济", 1)]
    plan = _plan(cli, orders, address_order={"东湖中餐": ["小"]})[0]
    assert any("食堂" in w and "最后面" in w for w in plan.warnings)
    apply_plan(cli, [plan], ledger=None, marker_enabled=False)
    assert cli.grid[(2, 0)] == "张" and cli.grid[(3, 0)] == "路人", "清单外的人排在表尾"


def test_helper_column_is_written_then_deleted():
    """排序用辅助列：写在所有内容列右侧，排序后整列删掉（右侧不留垃圾）。"""
    cli = FakeCli(make_grid(BASE_HEADER, [
        {0: "张", 1: "b2", 2: "111", 7: "3"},
        {0: "李", 1: "小", 2: "222", 7: "3"},
    ]))
    plan = _plan(cli, [CloudOrder("东湖中餐", "新", "小", "999", "中餐", "经济", 1)],
                 address_order={"东湖中餐": ["小", "b2"]})[0]
    assert cli.sheets_info("F1")[0]["colTo"] + 1 < plan.sort_key_col, "辅助列在内容列右侧"
    apply_plan(cli, [plan], ledger=None, marker_enabled=False)
    assert cli.sorts and cli.sorts[0]["key"] == wc.column_name(plan.sort_key_col)
    assert cli.sorts[0]["header"] is False, "第 3 行起排序，首行不是表头"
    assert cli.deleted_columns == [(plan.sort_key_col, plan.last_data_row)]
    left = [v for v in cli.grid.values() if re.fullmatch(r"\d{4}", str(v))]
    assert not left, f"辅助列没清干净：{left}"


def test_bogus_used_range_does_not_disable_sorting():
    """接口把 used range 报成 16383 时，排序不能被静默跳过。

    这是“东湖中餐排不了序”的回归测试：旧实现直接拿 colTo 当最后使用列，
    算出辅助列 16385 > MAX_SORT_COL，于是 sort_on 被关掉，新客户留在最上面。
    """
    cli = BogusWidthCli(make_grid(BASE_HEADER, [
        {0: "张", 1: "大西", 2: "111", 7: "3"},
        {0: "李", 1: "小", 2: "222", 7: "3"},
    ]))
    plan = _plan(cli, [CloudOrder("东湖中餐", "新", "小", "999", "中餐", "经济", 1)],
                 address_order={"东湖中餐": ["小", "大西"]})[0]

    assert plan.sort_enabled, f"应当排序；warnings={plan.warnings}"
    assert not [w for w in plan.warnings if "宽度异常" in w]
    assert plan.sort_key_col <= wc.MAX_SCAN_COL + 2, "辅助列必须落在可排序范围内"
    assert plan.sort_key_col > plan.columns["remark"], "辅助列必须在备注右侧"
    assert wc.column_name(plan.sort_key_col) in plan.sort_range

    result = apply_plan(cli, [plan], ledger=None, marker_enabled=False)
    assert result["failed"] == 0, result["sheets"]
    names = [cli.grid.get((r, 0)) for r in range(2, 5)]
    assert names == ["李", "新", "张"], f"实际顺序 {names}"


def test_sort_refuses_when_content_sits_past_the_scan_range():
    """辅助列右边还有内容（在读取范围之外）时拒绝排序，并回滚插入 —— 绝不排错位。"""
    cli = FakeCli(make_grid(BASE_HEADER, [
        {0: "张", 1: "大西", 2: "111", 7: "3"},
        {0: "李", 1: "小", 2: "222", 7: "3"},
    ]))
    # 在第 60 列（辅助列右侧）塞一个“看不见的”内容，模拟读取范围外还有东西
    cli.grid[(2, 59)] = "范围外"
    plan = _plan(cli, [CloudOrder("东湖中餐", "新", "小", "999", "中餐", "经济", 1)],
                 address_order={"东湖中餐": ["小", "大西"]})[0]
    assert plan.sort_enabled

    result = apply_plan(cli, [plan], ledger=None, marker_enabled=False)
    assert result["failed"] == 1
    assert not cli.sorts, "探测不通过时不该真的发排序请求"
    assert "右侧" in result["sheets"][0]["reason"]
    names = [cli.grid.get((r, 0)) for r in range(2, 5)]
    assert names == ["张", "李", None], f"插入的新行必须被回滚，实际 {names}"


def test_probe_skips_the_helper_column_itself():
    """探测从辅助列右边一格开始：辅助列里的排序键不算“右侧还有内容”。"""
    cli = FakeCli(make_grid(BASE_HEADER, [
        {0: "张", 1: "大西", 2: "111", 7: "3"},
    ]))
    plan = _plan(cli, [CloudOrder("东湖中餐", "新", "小", "999", "中餐", "经济", 1)],
                 address_order={"东湖中餐": ["小", "大西"]})[0]
    cli.grid[(2, plan.sort_key_col - 1)] = "0000"      # 假装辅助列已被写过键
    ok, detail = wc.probe_sort_area(cli, plan, 1, col_from=plan.sort_key_col + 1,
                                    row_to=plan.last_data_row)
    assert (ok, detail) == (True, "")


def test_sort_failure_rolls_back_inserted_rows():
    """排序失败时行还没被搬动，可以把插进去的新行整块删掉。"""
    class NoSortCli(FakeCli):
        def sort_range(self, *_a, **_k):
            raise WpsCloudError("模拟排序失败")

    cli = NoSortCli(make_grid(BASE_HEADER, [
        {0: "张", 1: "大西", 2: "111", 7: "3"},
        {0: "王", 1: "小", 2: "222", 7: "3"},
    ]))
    result = apply_plan(cli, _plan(cli, [CloudOrder("东湖中餐", "新人", "小", "999",
                                                    "中餐", "经济", 1)],
                                   address_order={"东湖中餐": ["小", "大西"]}),
                        ledger=None, marker_enabled=False)
    assert result["failed"] == 1
    assert cli.inserts, "确实调用过插入行接口"
    assert [cli.grid.get((r, 0)) for r in range(2, 4)] == ["张", "王"], "回滚后恢复原状"


def test_write_failure_after_sort_does_not_delete_rows():
    """排序成功后写入失败：**不回滚删行**（新行已散落到各地址组，删行会删错人）。"""
    class FailAfterSortCli(FakeCli):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.sorted = False

        def sort_range(self, *args, **kwargs):
            super().sort_range(*args, **kwargs)
            self.sorted = True

        def write_cells(self, file_id, worksheet_id, cells):
            if self.sorted:
                raise WpsCloudError("模拟排序后写入失败")
            return super().write_cells(file_id, worksheet_id, cells)

    cli = FailAfterSortCli(make_grid(BASE_HEADER, [
        {0: "张", 1: "大西", 2: "111", 7: "3"},
    ]))
    result = apply_plan(cli, _plan(cli, [CloudOrder("东湖中餐", "新人", "小", "999",
                                                    "中餐", "经济", 1)],
                                   address_order={"东湖中餐": ["小", "大西"]}),
                        ledger=None, marker_enabled=False)
    assert result["failed"] == 1
    assert cli.sorts, "排序已经发生"
    names = [cli.grid.get((r, 0)) for r in range(2, 5)]
    assert "新人" in names, f"新行不能被删掉（实际 {names}）"


def test_actual_sort_result_wins_over_prediction():
    """云端排序结果与本地预测不一致时，按**实际行号**写入并标记 sort_mismatch。"""
    class ShuffleCli(FakeCli):
        """故意按降序排，模拟"云端排法与本地预测不一致"。"""

        def sort_range(self, file_id, worksheet_id, **kwargs):
            # 故意按**降序**排（与本地的升序预测不同），模拟"云端排法与预测不一致"
            kwargs["order"] = "desc"
            super().sort_range(file_id, worksheet_id, **kwargs)

    cli = ShuffleCli(make_grid(BASE_HEADER, [
        {0: "张", 1: "大西", 2: "111", 7: "3"},
        {0: "王", 1: "小", 2: "222", 7: "3"},
    ]))
    plan = _plan(cli, [CloudOrder("东湖中餐", "新人", "小", "999", "中餐", "经济", 1),
                       CloudOrder("东湖中餐", "王", "小", "222", "中餐", "经济", 3)],
                 address_order={"东湖中餐": ["小", "大西"]})[0]
    result = apply_plan(cli, [plan], ledger=None, marker_enabled=False)
    assert result["written"] == 1
    assert result["sheets"][0]["sort_mismatch"] is True
    for change in plan.changes:
        row = change.row
        assert cli.grid.get((row - 1, 0)) == change.name, \
            f"{change.name} 的写入行号与姓名不符（第 {row} 行）"


def test_existing_customer_written_at_post_sort_row():
    """整表重排后，老客户的总餐次/日期格必须写在他们的**新**行号上。"""
    cli = FakeCli(make_grid(BASE_HEADER, [
        {0: "张", 1: "b2", 2: "111", 7: "3"},
        {0: "李", 1: "小", 2: "222", 7: "3"},
    ]))
    orders = [
        CloudOrder("东湖中餐", "李", "小", "222", "中餐", "经济", 6),      # 老客户，3 → 6
        CloudOrder("东湖中餐", "新人", "小", "999", "中餐", "经济", 1),
    ]
    plan = _plan(cli, orders, address_order={"东湖中餐": ["小", "b2"]})[0]
    rows = {c.name: c.row for c in plan.changes}
    assert rows == {"新人": 4, "李": 3}
    apply_plan(cli, [plan], ledger=None, marker_enabled=False)
    assert cli.grid[(2, 0)] == "李" and cli.grid[(2, 7)] == "9" and cli.grid[(2, 4)] == "1"
    assert cli.grid[(4, 0)] == "张" and (4, 4) not in cli.grid, "张没有日期格，别写错行"


def test_missing_meal_columns_are_completed():
    """半成品行自愈：老客户缺类型/餐种/公式时补齐（上次中断后的补救）。"""
    cli = FakeCli(make_grid(BASE_HEADER, [{0: "张", 1: "小", 2: "111", 7: "6"}]))
    orders = [CloudOrder("东湖中餐", "张", "小", "111", "中餐", "豪华", 6)]
    plan = _plan(cli, orders, sort=False)[0]
    change = plan.changes[0]
    assert change.fill_type and change.fill_kind and change.fill_formula
    apply_plan(cli, [plan], ledger=None, marker_enabled=False)
    assert cli.grid[(2, 5)] == "中餐" and cli.grid[(2, 6)] == "豪华"
    assert cli.grid[(2, 8)] == "=SUM(D3:E3)" and cli.grid[(2, 9)] == "=H3-I3"


def test_sort_disabled_keeps_new_rows_on_top():
    """排序开关关闭：新行留在第 4 行起，不做任何重排（应急模式）。"""
    cli = _grouped_grid()
    orders = [CloudOrder("东湖中餐", "新人", "小", "999", "中餐", "经济", 1)]
    plan = _plan(cli, orders, sort=False)[0]
    assert plan.sort_key_col == 0 and plan.sort_range == "" and not plan.sort_enabled
    apply_plan(cli, [plan], ledger=None, marker_enabled=False)
    names = [cli.grid.get((r, 0)) for r in range(2, 6)]
    assert names == ["张", "新人", "李", "王"], f"实际顺序 {names}"


def test_marker_column_detected_from_existing_weekday_number():
    """协作者的记号位会移动（实测东湖中餐在备注+3 的 Q 列）：
    表头行右侧已有 1~7 数字时，记号写回原位。"""
    header = dict(BASE_HEADER)
    header[16] = "5"                       # 0-based 16 = 第 17 列（Q 列）
    cli = FakeCli(make_grid(header, [{0: "张", 2: "111", 7: "3"}]))
    plan = _plan(cli, [CloudOrder("东湖中餐", "张", "小", "111", "中餐", "经济", 6)])[0]
    assert plan.marker_col == 17
    apply_plan(cli, [plan], ledger=None, marker_enabled=True)
    assert cli.grid[(1, 16)] == "6", "周五运行记 6，且写回协作者原有的记号位"
    assert cli.grid[(1, 12)] == "9.11 周五", "备注+2 的兜底位不该被误写"


def test_marker_value_is_run_day_not_target_day():
    """记号 = 运行日的周几：周五晚跑、目标周六，应记 6（周五）而不是 7。"""
    header = {0: "名字", 1: "地址", 2: "电话", 3: "9.10 周四", 4: "9.11 周五",
              5: "9.12 周六", 6: "类型", 7: "餐种", 8: "总餐次",
              9: "已出餐", 10: "剩余餐", 11: "备注", 16: "5"}
    cli = FakeCli(make_grid(header, [{0: "张", 2: "111", 8: "3"}]))
    orders = [CloudOrder("东湖中餐", "张", "小", "111", "中餐", "经济", 6)]
    apply_plan(cli, _plan(cli, orders, target=dt.date(2026, 9, 12),
                          run_date=dt.date(2026, 9, 11)),
               ledger=None, marker_enabled=True)
    assert cli.grid[(1, 16)] == "6", "写回协作者原记号位（Q 列），值更新为运行日的周几"


def test_date_region_stops_before_first_structural_column():
    """日期列区间 = 电话右侧 ~ 第一个结构列左侧（衣锦中餐有 114 个日期列，同理）。"""
    assert wc.date_region({"phone": 4, "type": 11, "kind": 12, "total": 13,
                           "served": 14, "left": 15, "remark": 16}) == (5, 10)
    wide = {"phone": 4, "type": 118, "kind": 119, "total": 120,
            "served": 121, "left": 122, "remark": 123}
    assert wc.date_region(wide) == (5, 117)
    assert wc.date_region({"phone": 4}) == (5, wc.MAX_SCAN_COL)


def test_collaborator_marker_column_is_not_a_date_column():
    """协作者写在备注右侧的「9.11 周五」是标记，不是日期列。

    真实缺陷：date_cols 原来扫描整行表头，把标记列也算进去，
    新行的「已出餐」公式因此变成 =SUM(D:E:M)（多统计了标记列）。
    """
    cli = FakeCli(make_grid(BASE_HEADER, [{0: "老人", 1: "小", 2: "111", 7: "3"}]))
    plan = _plan(cli, [CloudOrder("东湖中餐", "新人", "小", "222", "中餐", "经济", 1)],
                 sort=False)[0]
    assert plan.date_cols == [4, 5], f"实际 {plan.date_cols}（13 是协作者的标记列）"
    assert any("已忽略日期区间外的日期样式格" in w for w in plan.warnings)
    apply_plan(cli, [plan], ledger=None, marker_enabled=False)
    row = plan.changes[0].row
    assert cli.grid[(row - 1, 8)] == f"=SUM(D{row}:E{row})", "公式不能含标记列"
    assert cli.grid[(1, 12)] == "9.11 周五", "协作者的标记格一个字节都不能动"


def test_target_date_is_not_written_into_marker_column():
    """当天没有真实日期列时，宁可不写，也不能把 1 写进协作者的标记格。"""
    header = dict(BASE_HEADER)
    header.pop(4)                       # 去掉真正的 9.11 列，只留备注右侧的标记
    cli = FakeCli(make_grid(header, [{0: "张", 2: "111", 7: "6"}]))
    plan = _plan(cli, [CloudOrder("东湖中餐", "张", "小", "111", "中餐", "经济", 6)])[0]
    assert plan.target_col == 0
    assert any("没有 9.11" in w for w in plan.warnings)
    apply_plan(cli, [plan], ledger=None, marker_enabled=False)
    assert cli.grid[(1, 12)] == "9.11 周五", "协作者的标记格必须原样不动"


def test_marker_column_guard_avoids_date_like_cell():
    """记号兜底位是备注+3：备注+2 被协作者的日期标记占着。"""
    cli = FakeCli(make_grid(BASE_HEADER, [{0: "张", 2: "111", 7: "3"}]))
    plan = _plan(cli, [CloudOrder("东湖中餐", "张", "小", "111", "中餐", "经济", 6)])[0]
    assert plan.marker_col == 14        # 备注列 11 + 3


def test_apply_plan_skips_marker_in_test_mode_call():
    cli = FakeCli(make_grid(BASE_HEADER, [{0: "张", 2: "111", 7: "3"}]))
    orders = [CloudOrder("东湖中餐", "张", "小", "111", "中餐", "经济", 6)]
    apply_plan(cli, _plan(cli, orders), ledger=None, marker_enabled=False)
    # 第 13 列（index 12）是通讯记号位；BASE_HEADER 里放的是 9.11 表头，
    # 关闭记号时不应被改写成周几数字。
    assert cli.grid[(1, 12)] == "9.11 周五"


def test_insert_failure_rolls_back_inserted_rows():
    """插入成功但写入失败时，必须把插出来的行删掉，云端不能留烂尾空行。"""
    cli = FakeCli(make_grid(BASE_HEADER, [
        {0: "张", 1: "小", 2: "111", 7: "3"},
        {0: "王", 1: "小", 2: "333", 7: "5"},
        {0: "赵末", 1: "学3", 2: "444", 7: "6"},   # 表尾行，保证「小」组不在表尾
    ]))
    cli.fail_write = True
    orders = [CloudOrder("东湖中餐", "新人小", "小", "999", "中餐", "经济", 2)]
    plan = _plan(cli, orders)[0]
    result = apply_plan(cli, [plan], ledger=None, marker_enabled=False)
    assert result["failed"] == 1
    assert cli.inserts, "确实调用过插入行接口"
    assert cli.grid[(3, 0)] == "王" and cli.grid[(4, 0)] == "赵末", "回滚后云端恢复原状"
    assert (5, 0) not in cli.grid, "插入的空行已被删除"


def test_write_cells_batches_under_api_limit():
    """单次 update-range-data 有 100 项上限，写入必须自动分批。

    真实事故：东湖中餐一次追加 25 人 ≈ 175 格，接口返回
    ``400001 rangeData length 175 exceeds limit 100``，整张表写入失败。
    """
    calls: list[list[dict]] = []

    class BatchCli(FakeCli):
        """只覆盖底层接口，保留真实的分批逻辑（在 KdocsCli.write_cells 里）。"""

        def _run(self, *args, params=None):
            if args[:2] == ("sheet", "get-sheets-info"):
                return {"detail": {"sheetsInfo": [{"sheetId": 1}]}}
            batch = list(params["rangeData"])
            assert len(batch) <= 100, f"单批 {len(batch)} 项超过接口上限 100"
            calls.append(batch)
            # 把写入反映到网格里，供回读校验使用
            for item in batch:
                self.grid[(item["rowFrom"], item["colFrom"])] = str(item["formula"])
            return {"detail": {}}

        def write_cells(self, file_id, worksheet_id, cells):
            # 走真实实现（父类），从而验证分批
            wc.KdocsCli.write_cells(self, file_id, worksheet_id, cells)

        def read_cell_format(self, file_id, worksheet_id, row, col):
            return {"fonts": {"font_east_asia": "Microsoft YaHei", "size": 10,
                              "color": "#FF000000"},
                    "alignment": {"horizontal": "haCenter", "vertical": "vaCenter"},
                    "cellText": "参考"}
        # write_format_ops 沿用 FakeCli 的记录实现

    cli = BatchCli(make_grid(BASE_HEADER, [{0: "老人", 2: "111", 7: "3"}]))
    # 25 个新客户 × 9 格（基础字段、日期格、两条公式）= 225 格
    orders = [CloudOrder("东湖中餐", f"新{i}", "D2", f"1390000{i:04d}", "中餐", "经济", 1)
              for i in range(25)]
    # 关闭排序：本用例只验证分批，25 人 × 9 格 = 225 格
    result = apply_plan(cli, _plan(cli, orders, sort=False), ledger=None,
                        marker_enabled=False)
    assert len(calls) >= 2, "175 格必须拆成多批"
    assert sum(len(b) for b in calls) == 25 * 9
    assert result["written"] == 1 and result["failed"] == 0


def test_apply_plan_write_error_is_reported_not_raised():
    cli = FakeCli(make_grid(BASE_HEADER, [{0: "张", 2: "111", 7: "3"}]), fail_write=True)
    orders = [CloudOrder("东湖中餐", "张", "小", "111", "中餐", "经济", 6)]
    result = apply_plan(cli, _plan(cli, orders), ledger=None, marker_enabled=False)
    assert result["failed"] == 1 and result["written"] == 0


def test_apply_plan_writes_marker_when_enabled():
    cli = FakeCli(make_grid(BASE_HEADER, [{0: "张", 2: "111", 7: "3"}]))
    orders = [CloudOrder("东湖中餐", "张", "小", "111", "中餐", "经济", 6)]
    apply_plan(cli, _plan(cli, orders), ledger=None, marker_enabled=True)
    # 备注列（第 11 列 = index 10）+ 3 = 第 14 列（index 13），第 2 行；
    # 备注+2（index 12）是协作者的日期标记，绝不能被写
    assert cli.grid[(1, 13)] == str(weekday_number(dt.date(2026, 9, 11)))
    assert cli.grid[(1, 12)] == "9.11 周五"


def test_second_run_writes_nothing(tmp_path):
    """幂等：内容一致时第二次运行零写入（幂等锚点是账本）。"""
    led = SyncLedger(tmp_path / "state.json")
    cli = FakeCli(make_grid(BASE_HEADER, [{0: "张", 2: "111", 7: "3"}]))
    orders = [CloudOrder("东湖中餐", "张", "小", "111", "中餐", "经济", 6)]
    apply_plan(cli, _plan(cli, orders, ledger=led), ledger=led, marker_enabled=False)
    before = dict(cli.grid)
    result = apply_plan(cli, _plan(cli, orders, ledger=led), ledger=led,
                        marker_enabled=False)
    assert cli.grid == before
    assert result["sheets"][0]["status"] in {"noop", "ok"}


# ----------------------------------------------------------------------
# 汇总与渲染
# ----------------------------------------------------------------------

def test_summary_counts():
    plans = [SheetPlan(sheet="s", file_id="f", changes=[
        Change("existing", "a", "1", 3, 0, 5, 6, 6, True),    # 已完成
        Change("existing", "b", "2", 4, 3, 5, 1, 4, False),   # 需更新
        Change("new", "c", "3", 5, 2, 5, 0, 2, False),        # 新增
        # 日期格被协作者写了 0：总餐次照加（需更新）但日期格跳过
        Change("existing", "d", "4", 6, 6, 5, 6, 12, False, target_occupied="0"),
    ])]
    assert summarize_plan(plans) == {"to_update": 2, "to_append": 1,
                                     "unchanged": 1, "warned": 0, "skipped": 1}


def test_format_plan_mentions_missing_column():
    plans = [SheetPlan(sheet="东湖中餐", file_id="f",
                       target_date=dt.date(2026, 9, 12),
                       warnings=["云端表里没有 9.12 这一列"])]
    text = format_plan(plans)
    assert "东湖中餐" in text and "没有 9.12" in text


# ----------------------------------------------------------------------
# 账本
# ----------------------------------------------------------------------

def test_ledger_roundtrip(tmp_path: Path):
    path = tmp_path / "state.json"
    led = SyncLedger(path)
    assert led.synced_meals("2026-09-11", "F1", "张", "111") is None
    led.record("2026-09-11", "F1", {"张\u0000111": 6})
    led.save()
    again = SyncLedger(path)
    assert again.synced_meals("2026-09-11", "F1", "张", "111") == 6
    summary = again.batch_summary("2026-09-11", "F1")
    assert summary and summary["people"] == 1


def test_ledger_corrupt_file_is_rejected_not_reset(tmp_path: Path):
    """损坏账本必须失败关闭，不能被当成“没有记录”而重复加餐。"""
    path = tmp_path / "state.json"
    path.write_text("{ not json", encoding="utf-8")
    with pytest.raises(LedgerCorruptError):
        SyncLedger(path)


# ----------------------------------------------------------------------
# 本地表读取
# ----------------------------------------------------------------------

def test_read_local_orders(tmp_path: Path):
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.title = "东湖中餐"
    ws.cell(2, 1, "订单")
    ws.cell(2, 2, "姓名")
    ws.cell(3, 1, "W1")
    ws.cell(3, 2, "张")
    ws.cell(3, 3, "大西")
    ws.cell(3, 4, "19730037965")
    ws.cell(3, 12, "中餐")
    ws.cell(3, 13, "经济")
    ws.cell(3, 14, 6)
    path = tmp_path / "排单.xlsx"
    wb.save(path)

    orders = wc.read_local_orders(path, sheets=["东湖中餐"])
    assert len(orders["东湖中餐"]) == 1
    order = orders["东湖中餐"][0]
    assert (order.name, order.phone, order.meals) == ("张", "19730037965", 6)
    assert order.meal_type == "中餐" and order.meal_kind == "经济"


def test_read_local_orders_missing_file(tmp_path: Path):
    with pytest.raises(WpsCloudError):
        wc.read_local_orders(tmp_path / "nope.xlsx")


def _local_workbook(path: Path, rows) -> Path:
    """写一张最小本地排单表：rows = [(订单, 姓名, 地址, 电话, 星期标记, 餐次), ...]。"""
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.title = "东湖中餐"
    for col, title in ((1, "订单"), (2, "姓名"), (3, "地址"), (4, "电话"),
                       (5, "周一"), (6, "周二"), (7, "周三"), (8, "周四"),
                       (9, "周五"), (10, "周六"), (11, "周日"),
                       (12, "类型"), (13, "餐种"), (14, "餐次")):
        ws.cell(2, col, title)
    weekdays = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    for idx, (no, name, addr, phone, mark, meals) in enumerate(rows, start=3):
        ws.cell(idx, 1, no)
        ws.cell(idx, 2, name)
        ws.cell(idx, 3, addr)
        ws.cell(idx, 4, phone)
        ws.cell(idx, 5 + weekdays.index(mark), 1)
        ws.cell(idx, 12, "中餐")
        ws.cell(idx, 13, "经济")
        ws.cell(idx, 14, meals)
    wb.save(path)
    return path


def test_read_local_orders_records_order_no_and_weekday_marks(tmp_path: Path):
    """读取层要把「订单号」和「周一~周日」标记带给计划层（批次日期核对要用）。"""
    path = _local_workbook(tmp_path / "排单.xlsx", [
        ("W17", "金小雅", "D2", "13175798579", "周六", 6),
    ])
    order = wc.read_local_orders(path, sheets=["东湖中餐"])["东湖中餐"][0]
    assert order.order_no == "W17"
    assert order.weekday_marks == ("周六",)
    assert order.rows == (3,)


def test_local_workbook_batch_date_is_checked_end_to_end(tmp_path: Path):
    """读取 → 计划 的联动：本地表标着周六、目标日期却是周五 → 整张表被拒绝。

    这是"拿错本地表会把同一批餐重复加上去"的唯一防线，必须端到端可用。
    """
    path = _local_workbook(tmp_path / "排单.xlsx", [
        ("W17", "张", "大西", "19730037965", "周六", 6),
    ])
    cli = FakeCli(make_grid(BASE_HEADER, [{0: "张", 1: "大西", 2: "19730037965", 7: "5"}]))
    plans = _plan(cli, wc.read_local_orders(path, sheets=["东湖中餐"])["东湖中餐"])
    assert plans[0].blocked_reason, "目标 9.11 是周五，本地标的是周六"
    assert plans[0].changes == []


def test_local_workbook_matching_batch_date_is_written(tmp_path: Path):
    """同一批数据，星期标记与目标日期一致 → 正常累加（5 + 6 = 11）。"""
    path = _local_workbook(tmp_path / "排单.xlsx", [
        ("W17", "张", "大西", "19730037965", "周五", 6),
    ])
    cli = FakeCli(make_grid(BASE_HEADER, [{0: "张", 1: "大西", 2: "19730037965", 7: "5"}]))
    plans = _plan(cli, wc.read_local_orders(path, sheets=["东湖中餐"])["东湖中餐"])
    assert not plans[0].blocked_reason
    apply_plan(cli, plans, ledger=None, marker_enabled=False)
    assert cli.grid[(2, 7)] == "11" and cli.grid[(2, 4)] == "1"


# ----------------------------------------------------------------------
# CLI 查找
# ----------------------------------------------------------------------

def test_find_cli_prefers_explicit(tmp_path: Path):
    fake = tmp_path / "kdocs-cli"
    fake.write_text("#!/bin/sh\n", encoding="utf-8")
    assert find_cli(fake) == str(fake)


def test_find_cli_reports_missing(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(wps_cli.shutil, "which", lambda _name: None)
    monkeypatch.setattr(wps_cli, "__file__", str(tmp_path / "app" / "wps" / "cli.py"))
    monkeypatch.setattr(wps_cli.sys, "executable", str(tmp_path / "python"))
    monkeypatch.delattr(wps_cli.sys, "_MEIPASS", raising=False)
    with pytest.raises(WpsCloudError):
        find_cli()


def test_delete_rows_uses_range_data_for_real_cli_shape():
    cli = wc.KdocsCli.__new__(wc.KdocsCli)
    calls = []
    cli._run = lambda *args, **kwargs: calls.append((args, kwargs))
    cli.delete_rows("F1", 1, row=4, count=2)
    params = calls[0][1]["params"]
    assert params["range_data"] == [{"col_from": 0, "col_to": 16383,
                                      "row_from": 3, "row_to": 4}]
    assert params["shift_type"] == "shift_up"


def test_read_grid_with_format_accepts_real_background_field():
    cli = wc.KdocsCli.__new__(wc.KdocsCli)
    cli._run = lambda *args, **kwargs: {"detail": {"rangeData": [{
        "originRow": 2, "originCol": 10, "cellText": "6",
        "cell_background_color": "#FFFFC000",
    }]}}
    assert cli.read_grid("F1", 1, 0, 3, 0, 12, with_format=True) == {
        (2, 10): {"text": "6", "fill": "#FFFFC000"}}


def test_formula_cells_for_new_rows_use_dynamic_date_columns():
    plan = SheetPlan(sheet="东湖中餐", file_id="F1", target_col=8,
                     columns={"total": 12, "served": 13, "left": 14},
                     date_cols=[4, 5, 6, 7, 8, 9])
    cells = wc.formula_cells_for_new_rows(plan, [33, 34])
    assert cells == [
        {"row": 33, "col": 13, "value": "=SUM(D33:I33)"},
        {"row": 33, "col": 14, "value": "=L33-M33"},
        {"row": 34, "col": 13, "value": "=SUM(D34:I34)"},
        {"row": 34, "col": 14, "value": "=L34-M34"},
    ]


def test_empty_cloud_table_starts_at_first_data_row():
    """空表（还没有任何人）直接写第 3 行起：不插行、不留空行、不排序。"""
    cli = FakeCli(make_grid(BASE_HEADER, []))
    plans = _plan(cli, [CloudOrder("东湖中餐", "首单", "小", "123", "中餐", "经济", 6)],
                  address_order={"东湖中餐": ["小"]})
    plan = plans[0]
    assert plan.changes[0].row == 3
    assert plan.insert_blocks and plan.insert_blocks[0].append_only
    apply_plan(cli, plans, ledger=None, marker_enabled=False)
    assert cli.inserts == [], "空表不需要调用插入行接口"
    assert cli.grid[(2, 0)] == "首单" and cli.grid[(2, 4)] == "1"


def test_interior_blank_row_is_not_dumped_at_table_end():
    """表中间的空行（分组之间的分隔行）要留在原分组里，不能被排序甩到表尾。"""
    cli = FakeCli(make_grid(BASE_HEADER, [
        {0: "甲", 1: "小", 2: "111", 7: "3"},
        {0: "乙", 1: "小", 2: "222", 7: "3"},
        {11: "分隔行"},                       # 第 5 行：没有姓名，只有别列内容
        {0: "丙", 1: "大西", 2: "333", 7: "3"},
    ]))
    plan = _plan(cli, [CloudOrder("东湖中餐", "新人", "小", "999", "中餐", "经济", 1)],
                 address_order={"东湖中餐": ["小", "大西"]})[0]
    apply_plan(cli, [plan], ledger=None, marker_enabled=False)
    names = [cli.grid.get((r, 0)) for r in range(2, 7)]
    assert names == ["甲", "乙", "新人", None, "丙"], f"实际 {names}"
    assert cli.grid.get((5, 11)) == "分隔行", "分隔行留在小组之后、大西之前"


# ----------------------------------------------------------------------
# 生效目标（effective_tables）：闪时送云端名单读取与云同步共用同一份判定
# ----------------------------------------------------------------------


class _Cfg:
    def __init__(self, **kwargs):
        self.wps_test_mode = False
        self.wps_tables = {}
        self.wps_production_tables = {}
        self.wps_test_tables = {}
        for key, value in kwargs.items():
            setattr(self, key, value)


PROD = {"东湖中餐": {"file_id": "prod-lunch"},
        "东湖晚餐": {"file_id": "prod-dinner"}}


def test_effective_tables_production_mode():
    config = _Cfg(wps_tables={"东湖中餐": {"file_id": "prod-lunch"}},
                  wps_production_tables=PROD)
    assert wc.effective_tables(config) == {"东湖中餐": {"file_id": "prod-lunch"}}


def test_effective_tables_rejects_non_production_id():
    config = _Cfg(wps_tables={"东湖中餐": {"file_id": "other"}},
                  wps_production_tables=PROD)
    with pytest.raises(WpsCloudError, match="非正式表"):
        wc.effective_tables(config)


def test_effective_tables_test_mode_uses_copies_and_rejects_production():
    config = _Cfg(wps_test_mode=True, wps_production_tables=PROD,
                  wps_test_tables={"东湖中餐": "copy-lunch"})
    assert wc.effective_tables(config) == {"东湖中餐": {"file_id": "copy-lunch"}}

    config.wps_test_tables = {}
    with pytest.raises(WpsCloudError, match="未配置新的测试副本"):
        wc.effective_tables(config)

    config.wps_test_tables = {"东湖中餐": "prod-lunch"}
    with pytest.raises(WpsCloudError, match="包含正式表 ID"):
        wc.effective_tables(config)

    config.wps_test_tables = {"东湖中餐": "H8vzKoTJVrMP7mA9QG591xqS9W8Bg57iG"}
    with pytest.raises(WpsCloudError, match="已过期试验田"):
        wc.effective_tables(config)
