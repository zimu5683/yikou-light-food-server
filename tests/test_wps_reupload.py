"""WPS「核对并补传」核心逻辑单测（无 Bridge、无网络）。

覆盖：整表回退 / 部分回退 / 云端完整 / 无法判定 / 未结案 / 目标表变化 /
归档记录 / 补传计划只补缺失 / 补传后账本锚点不重复 / 二次补传幂等。
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from app.wps.executor import apply_plan
from app.wps.journal import SyncJournal, journal_path_for
from app.wps.ledger import SyncLedger
from app.wps.models import CloudOrder
from app.wps.planner import build_plan
from app.wps import reupload
from tests.wps_ledger_harness import (
    COL_ADDR,
    COL_LEFT,
    COL_NAME,
    COL_PHONE,
    COL_SERVED,
    COL_TARGET,
    COL_TOTAL,
    FILE_ID,
    SHEET,
    TARGET,
    GridCloud,
    make_grid,
)

CFG = SimpleNamespace(
    wps_enabled=True, wps_test_mode=False, wps_sort_enabled=False,
    wps_marker_enabled=False, wps_address_order={},
)


def _local() -> list[CloudOrder]:
    """本批：老客户 +2 餐；两个新客户各 +1 / +2 餐。"""
    return [
        CloudOrder(SHEET, "老王", "小", "111", "中餐", "经济", 2, order_no="W1"),
        CloudOrder(SHEET, "小李", "大西", "222", "中餐", "经济", 1, order_no="W2"),
        CloudOrder(SHEET, "小赵", "A1", "333", "中餐", "豪华", 2, order_no="W3"),
    ]


def _baseline_rows() -> list[dict[int, str]]:
    return [
        {COL_NAME: "老王", COL_ADDR: "小", COL_PHONE: "111",
         COL_TOTAL: "5", COL_TARGET: "", COL_SERVED: "0", COL_LEFT: "5"},
    ]


def _make_env(tmp_path: Path):
    """建一份合成云表 + 临时账本/日志；返回上传后的对象。"""
    cli = GridCloud(make_grid(rows=_baseline_rows()))
    ledger = SyncLedger(tmp_path / "state.json")
    journal = SyncJournal(journal_path_for(ledger.path))
    plans = build_plan(
        cli, local_orders={SHEET: _local()},
        tables={SHEET: {"file_id": FILE_ID}},
        target=TARGET, ledger=ledger, address_order={}, sort_enabled=False)
    assert not plans[0].blocked_reason, plans[0].blocked_reason
    baseline_grid = dict(cli.grid)
    result = apply_plan(cli, plans, ledger=ledger, marker_enabled=False,
                        journal=journal)
    assert result["status"] == "ok", result
    return cli, ledger, journal, baseline_grid


def _check(cli, journal, ledger, *, config=CFG, sheet=SHEET, effective=None):
    terminal, pending, warnings = reupload.latest_sheet_records(journal)
    ref = terminal.get(sheet) or pending.get(sheet)
    assert ref is not None, (list(terminal), list(pending), warnings)
    effective = effective or {SHEET: {"file_id": FILE_ID}}
    return reupload.check_sheet(cli, ref, effective_tables=effective,
                                config=config, ledger=ledger)


def _plan_needs_write(plan) -> bool:
    return any(change.needs_write for change in plan.changes)


# ----------------------------------------------------------------------
# 核对结论
# ----------------------------------------------------------------------
def test_check_after_full_cloud_revert_lists_every_order(tmp_path):
    cli, ledger, journal, baseline = _make_env(tmp_path)
    cli.grid = dict(baseline)  # 上传成功后云端整表被回退

    check = _check(cli, journal, ledger)

    assert check.status == "ready", check.reason
    assert check.counts == {"total": 3, "to_resend": 3, "present": 0, "manual": 0}
    by_name = {order["name"]: order for order in check.orders}
    assert by_name["老王"]["status"] == "missing_meals"
    assert by_name["老王"]["expected_total"] == 7
    assert by_name["老王"]["actual_total"] == 5
    assert by_name["老王"]["missing_meals"] == 2
    assert "mark" in by_name["老王"]["missing"]
    assert by_name["小李"]["status"] == "missing_row"
    assert by_name["小赵"]["status"] == "missing_row"
    assert check.plan is not None


def test_check_complete_cloud_is_noop(tmp_path):
    cli, ledger, journal, _baseline = _make_env(tmp_path)
    check = _check(cli, journal, ledger)

    assert check.status == "complete", check.reason
    assert check.plan is None
    assert check.counts == {"total": 3, "to_resend": 0, "present": 3,
                            "manual": 0}


def test_check_partial_revert_only_writes_missing_person(tmp_path):
    cli, ledger, journal, _baseline = _make_env(tmp_path)
    # 只回退老客户：总餐次与日期格恢复原值；新客户行仍在云端。
    cli.grid[(2, 4)] = ""   # 第 3 行（0-based 2）目标日期格
    cli.grid[(2, 7)] = "5"  # 总餐次

    check = _check(cli, journal, ledger)
    assert check.status == "ready", check.reason
    by_name = {order["name"]: order for order in check.orders}
    assert by_name["老王"]["status"] == "missing_meals"
    assert by_name["小李"]["status"] == "present"
    assert by_name["小赵"]["status"] == "present"
    assert check.counts["to_resend"] == 1
    assert check.plan is not None

    writes_before = len(cli.writes)
    result = apply_plan(cli, [check.plan], ledger=ledger,
                        marker_enabled=False, journal=journal)
    assert result["status"] == "ok", result
    # 只补老客户的少量格子：不得整表重写。
    written_cells = [cell for batch in cli.writes[writes_before:]
                     for cell in batch]
    assert written_cells
    assert all(str(cell.get("row")) in ("3",) for cell in written_cells), written_cells
    assert cli.grid[(2, 7)] == "7"
    assert cli.grid[(2, 4)] == "1"


def test_check_manual_when_cloud_total_higher_than_expected(tmp_path):
    cli, ledger, journal, _baseline = _make_env(tmp_path)
    cli.grid[(2, 7)] = "99"  # 云端总餐次比期望还多
    check = _check(cli, journal, ledger)

    manual = [order for order in check.orders if order["status"] == "manual"]
    assert [order["name"] for order in manual] == ["老王"]
    assert "99" in manual[0]["reason"] or "期望" in manual[0]["reason"]
    # 其余两人已在云端：没有可安全补传的订单，整表标「无法判定」。
    assert check.status == "manual", check.reason
    assert check.counts == {"total": 3, "to_resend": 0, "present": 2,
                            "manual": 1}


def test_check_blocks_sheet_when_manual_and_insert_needed(tmp_path):
    cli, ledger, journal, baseline = _make_env(tmp_path)
    cli.grid = dict(baseline)   # 整表回退：新客户行没了
    cli.grid[(2, 7)] = "99"     # 老王又被改成无法判定
    check = _check(cli, journal, ledger)

    manual = [order for order in check.orders if order["status"] == "manual"]
    assert [order["name"] for order in manual] == ["老王"]
    # 需要给两位新客户插行 + 整表重排，但同一张表里有人无法判定：
    # 为避免排序影响协作者的行，整表停止补传。
    assert check.status == "blocked", check.reason
    assert "新增行" in check.reason and "无法判定" in check.reason
    assert check.counts["to_resend"] == 2


def test_check_partial_when_manual_and_no_insert_needed(tmp_path):
    cli, ledger, journal, _baseline = _make_env(tmp_path)
    # 老王被协作者改成更大的总餐次 → 无法判定；小李日期格被清空 → 可补；
    # 小赵完整。没有新增行，因此其余订单可以安全补传。
    cli.grid[(2, 7)] = "99"
    cli.grid[(3, 4)] = ""
    check = _check(cli, journal, ledger)

    by_name = {order["name"]: order for order in check.orders}
    assert by_name["老王"]["status"] == "manual"
    assert by_name["小李"]["status"] == "missing_mark"
    assert by_name["小赵"]["status"] == "present"
    assert check.status == "partial", check.reason
    assert check.counts == {"total": 3, "to_resend": 1, "present": 1,
                            "manual": 1}


def test_check_manual_when_target_mark_changed_to_other_value(tmp_path):
    cli, ledger, journal, _baseline = _make_env(tmp_path)
    cli.grid[(2, 4)] = "2"  # 日期格被协作者改成别的值（非 0、非 1）
    check = _check(cli, journal, ledger)
    manual = [order for order in check.orders if order["status"] == "manual"]
    assert [order["name"] for order in manual] == ["老王"]
    assert "日期格" in manual[0]["reason"]


def test_check_pending_record_is_readonly_and_blocked(tmp_path):
    cli, ledger, journal, baseline = _make_env(tmp_path)
    cli.grid = dict(baseline)
    terminal, _pending, _ = reupload.latest_sheet_records(journal)
    op_id = terminal[SHEET].operation_id
    journal.set_sheet_status(op_id, terminal[SHEET].sheet_key, "uncertain",
                             reason="测试未结案",
                             next_action="manual_reconcile")
    journal.save()

    check = _check(cli, journal, ledger)
    assert check.pending is True
    assert check.status == "blocked"
    assert "未结案" in check.reason
    assert check.plan is None


def test_check_target_file_changed_is_blocked(tmp_path):
    cli, ledger, journal, baseline = _make_env(tmp_path)
    cli.grid = dict(baseline)
    check = _check(cli, journal, ledger,
                   effective={SHEET: {"file_id": "OTHER-FILE"}})
    assert check.status == "blocked"
    assert "目标云文档已变化" in check.reason


def test_check_record_without_intents_is_blocked(tmp_path):
    cli, ledger, journal, baseline = _make_env(tmp_path)
    cli.grid = dict(baseline)
    terminal, _pending, _ = reupload.latest_sheet_records(journal)
    ref = terminal[SHEET]
    ref.record = {**ref.record, "intents": []}
    check = reupload.check_sheet(cli, ref,
                                 effective_tables={SHEET: {"file_id": FILE_ID}},
                                 config=CFG, ledger=ledger)
    assert check.status == "blocked"
    assert "订单明细" in check.reason


# ----------------------------------------------------------------------
# 归档记录 / 最近一次
# ----------------------------------------------------------------------
def test_archived_record_still_used(tmp_path):
    cli, ledger, journal, baseline = _make_env(tmp_path)
    cli.grid = dict(baseline)
    live = json.loads(journal.path.read_text(encoding="utf-8"))
    journal.path.write_text(json.dumps({"version": 1, "operations": {}},
                                       ensure_ascii=False), encoding="utf-8")
    Path(str(journal.path) + ".archive").write_text(
        json.dumps({"version": 1, "operations": live["operations"]},
                   ensure_ascii=False), encoding="utf-8")

    fresh_journal = SyncJournal(journal.path)
    terminal, pending, warnings = reupload.latest_sheet_records(fresh_journal)
    assert SHEET in terminal, warnings
    check = reupload.check_sheet(
        cli, terminal[SHEET],
        effective_tables={SHEET: {"file_id": FILE_ID}},
        config=CFG, ledger=ledger)
    assert check.status == "ready", check.reason


def test_latest_record_prefers_newest(tmp_path):
    cli, ledger, journal, baseline = _make_env(tmp_path)
    terminal, _pending, _ = reupload.latest_sheet_records(journal)
    old_ref = terminal[SHEET]
    # 造一条更新的记录（同一张表），最新记录必须被选中。
    newer = dict(old_ref.record)
    newer["status"] = "failed_no_write"
    op_id = "wps-0123456789abcdef"
    journal.create_operation(op_id, {"0:x": newer}, target_date=TARGET.isoformat())
    journal.save()

    fresh = SyncJournal(journal.path)
    terminal, _pending, _ = reupload.latest_sheet_records(fresh)
    assert terminal[SHEET].operation_id == op_id
    assert terminal[SHEET].status == "failed_no_write"


# ----------------------------------------------------------------------
# 补传后账本 / 幂等
# ----------------------------------------------------------------------
def test_repair_keeps_ledger_anchors_and_normal_preview_is_noop(tmp_path):
    cli, ledger, journal, baseline = _make_env(tmp_path)
    cli.grid = dict(baseline)
    check = _check(cli, journal, ledger)
    assert check.writeable

    repaired = apply_plan(cli, [check.plan], ledger=ledger,
                          marker_enabled=False, journal=journal)
    assert repaired["status"] == "ok", repaired

    # 账本锚点 = 本批本地餐次，不因为补传翻倍。
    assert ledger.synced_local(TARGET.isoformat(), FILE_ID, "老王", "111") == 2
    assert ledger.synced_local(TARGET.isoformat(), FILE_ID, "小李", "222") == 1
    assert ledger.synced_local(TARGET.isoformat(), FILE_ID, "小赵", "333") == 2
    # 普通上传计划：必须是 no-op（云端正向 + 锚点正确）。
    normal = build_plan(
        cli, local_orders={SHEET: _local()},
        tables={SHEET: {"file_id": FILE_ID}},
        target=TARGET, ledger=SyncLedger(ledger.path),
        address_order={}, sort_enabled=False)
    assert not any(_plan_needs_write(plan) for plan in normal)


def test_second_repair_is_noop(tmp_path):
    cli, ledger, journal, baseline = _make_env(tmp_path)
    cli.grid = dict(baseline)
    first = _check(cli, journal, ledger)
    apply_plan(cli, [first.plan], ledger=ledger, marker_enabled=False,
               journal=journal)

    second = _check(cli, journal, ledger)
    assert second.status == "complete", second.reason
    assert second.plan is None


def test_check_does_not_modify_cloud_ledger_or_journal(tmp_path):
    cli, ledger, journal, baseline = _make_env(tmp_path)
    cli.grid = dict(baseline)
    cloud_before = dict(cli.grid)
    writes_before = len(cli.writes)
    ledger_before = ledger.path.read_text(encoding="utf-8")
    journal_before = journal.path.read_text(encoding="utf-8")

    _check(cli, journal, ledger)

    assert cli.grid == cloud_before
    assert len(cli.writes) == writes_before
    assert ledger.path.read_text(encoding="utf-8") == ledger_before
    assert journal.path.read_text(encoding="utf-8") == journal_before
