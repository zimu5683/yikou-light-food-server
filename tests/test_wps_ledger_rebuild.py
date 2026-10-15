"""账本锚点重建的判定规则：宁漏勿重，逐行可核对。

只使用合成内存网格与临时账本；不联网、不读真实客户数据。
"""
from __future__ import annotations

import json

from app.wps.models import CloudOrder
from app.wps.rebuild import (LEAVE_EMPTY, MARK_SYNCED, REASON_CELL_EMPTY,
                             REASON_CELL_MARKED, REASON_NEW_PERSON,
                             build_rebuild, format_rebuild, verify_rebuild,
                             write_rebuild)
from app.wps.sync import SyncLedger
from tests.wps_ledger_harness import (COL_NAME, COL_PHONE, COL_TARGET,
                                      COL_TOTAL, FILE_ID, SHEET, TARGET,
                                      GridCloud, make_grid, orders,
                                      plan_without_ledger)

KEY = "张\u0000111"
TARGET_DATE = TARGET.isoformat()


def _cloud(*rows: dict[int, str]) -> GridCloud:
    return GridCloud(make_grid(rows=list(rows)))


def _existing(cell: str = "", total: str = "5") -> dict[int, str]:
    row = {COL_NAME: "张", COL_PHONE: "111", COL_TOTAL: total}
    if cell:
        row[COL_TARGET] = cell
    return row


def _proposal(cloud, *meals: int):
    return build_rebuild(plan_without_ledger(cloud, orders(*meals)))


# ----------------------------------------------------------------------
# 三条判定
# ----------------------------------------------------------------------

def test_cell_already_marked_means_synced():
    proposal = _proposal(_cloud(_existing(cell="1")), 6)
    row = proposal.sheets[0].rows[0]

    assert row.decision == MARK_SYNCED
    assert row.reason == REASON_CELL_MARKED
    assert row.cloud_present is True
    assert row.cell_marked is True and row.cell_value == "1"
    assert row.anchor == 6
    assert proposal.sheets[0].entries[KEY]["slots"] == [6]
    assert proposal.counts["mark_synced"] == 1


def test_collaborator_zero_counts_as_synced():
    """协作者写 0（当天不送）分不清是谁写的：按已入账处理 —— 宁漏勿重。"""
    proposal = _proposal(_cloud(_existing(cell="0")), 6)
    row = proposal.sheets[0].rows[0]

    assert row.decision == MARK_SYNCED
    assert row.cell_value == "0"
    assert row.anchor == 6


def test_empty_cell_stays_empty():
    proposal = _proposal(_cloud(_existing()), 6)
    row = proposal.sheets[0].rows[0]

    assert row.decision == LEAVE_EMPTY
    assert row.reason == REASON_CELL_EMPTY
    assert row.anchor == 0
    assert proposal.sheets[0].entries == {}
    assert proposal.counts[REASON_CELL_EMPTY] == 1


def test_absent_person_stays_empty():
    """云端没有这个人：必须留空，否则这一批的新客户永远建不出来。"""
    proposal = _proposal(_cloud(), 6)
    row = proposal.sheets[0].rows[0]

    assert row.decision == LEAVE_EMPTY
    assert row.reason == REASON_NEW_PERSON
    assert row.cloud_present is False
    assert proposal.sheets[0].entries == {}
    assert proposal.counts[REASON_NEW_PERSON] == 1


def test_only_marked_slot_gets_an_anchor():
    """同一个人两个槽位：只有云端那一格已经标了的那一行记锚点。"""
    proposal = _proposal(
        _cloud(_existing(cell="1", total="5"), _existing(total="2")), 6, 2)
    sheet = proposal.sheets[0]

    assert [row.decision for row in sheet.rows] == [MARK_SYNCED, LEAVE_EMPTY]
    assert sheet.entries[KEY]["slots"] == [6, 0]
    assert sheet.entries[KEY]["local"] == 6


def test_entries_carry_order_identity_and_position():
    """重建写下的锚点与真实上传同一套口径（slots/orders/positions 等长）。"""
    proposal = _proposal(_cloud(_existing(cell="1")), 6)
    entry = proposal.sheets[0].entries[KEY]

    assert len(entry["slots"]) == len(entry["orders"]) == len(entry["positions"])
    assert entry["total"] == 5      # 重建不写云端：云端总餐次仍是读到的那份


# ----------------------------------------------------------------------
# 指纹
# ----------------------------------------------------------------------

def test_fingerprint_changes_when_the_cloud_changes():
    marked = _proposal(_cloud(_existing(cell="1")), 6)
    empty = _proposal(_cloud(_existing()), 6)

    assert marked.fingerprint != empty.fingerprint


def test_fingerprint_is_stable_for_the_same_input():
    first = _proposal(_cloud(_existing(cell="1")), 6)
    second = _proposal(_cloud(_existing(cell="1")), 6)

    assert first.fingerprint == second.fingerprint
    assert len(first.fingerprint) == 64


# ----------------------------------------------------------------------
# 落盘与回读
# ----------------------------------------------------------------------

def test_write_rebuild_writes_anchors_and_keeps_other_batches(tmp_path):
    path = tmp_path / "state.json"
    ledger = SyncLedger(path)
    ledger.record("2026-09-10", FILE_ID, {"旧\u0000999": {
        "slots": [1], "orders": [""], "positions": [1], "local": 1, "total": 1}})
    ledger.save()

    proposal = _proposal(_cloud(_existing(cell="1")), 6)
    result = write_rebuild(proposal, ledger_path=path, note="账本丢失后重建")

    assert result["changed"] is True and result["people"] == 1
    fresh = SyncLedger(path)
    assert fresh.synced_slots(TARGET_DATE, FILE_ID, "张", "111") == [6]
    assert fresh.synced_slots("2026-09-10", FILE_ID, "旧", "999") == [1]
    assert fresh.data["batches"][TARGET_DATE][FILE_ID]["rebuilt_note"] == "账本丢失后重建"
    assert verify_rebuild(proposal, ledger_path=path) is True


def test_verify_detects_missing_anchors(tmp_path):
    path = tmp_path / "state.json"
    proposal = _proposal(_cloud(_existing(cell="1")), 6)
    write_rebuild(proposal, ledger_path=path, note="先写一遍")
    assert verify_rebuild(proposal, ledger_path=path) is True

    path.write_text(json.dumps({"version": 1, "batches": {}}), encoding="utf-8")
    assert verify_rebuild(proposal, ledger_path=path) is False


def test_write_rebuild_is_a_noop_when_nothing_is_marked(tmp_path):
    path = tmp_path / "state.json"
    proposal = _proposal(_cloud(_existing()), 6)

    result = write_rebuild(proposal, ledger_path=path, note="没有可写的")

    assert result["changed"] is False and result["people"] == 0
    assert not path.exists()


def test_rebuild_recovers_from_a_corrupt_ledger(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{ broken", encoding="utf-8")

    proposal = _proposal(_cloud(_existing(cell="1")), 6)
    result = write_rebuild(proposal, ledger_path=path, note="损坏后重建")

    assert result["replaced_corrupt"] is True
    assert SyncLedger(path).synced_slots(TARGET_DATE, FILE_ID, "张", "111") == [6]


# ----------------------------------------------------------------------
# 整表被拒绝写入时不猜
# ----------------------------------------------------------------------

def test_blocked_sheet_is_skipped():
    """批次日期不符时本地行与目标日期不对应：没有可推断的前提，不重建。"""
    cloud = _cloud(_existing(cell="1"))
    mismatched = [CloudOrder(SHEET, "张", "小", "111", "中餐", "经济", 6,
                             weekday_marks=("周一",))]

    proposal = build_rebuild(plan_without_ledger(cloud, mismatched))

    assert proposal.sheets[0].skipped_reason
    assert proposal.sheets[0].entries == {}
    assert proposal.counts["skipped_sheets"] == 1
    assert proposal.warnings


def test_format_rebuild_shows_both_directions():
    # 两个不同的人：张（云端已标当天）与新人（云端还没有这个人）
    cloud = _cloud(_existing(cell="1"), {COL_NAME: "新人", COL_PHONE: "222",
                                         COL_TOTAL: "0"})
    local = orders(6) + [CloudOrder(SHEET, "新人", "小", "222", "中餐", "经济", 1)]
    proposal = build_rebuild(plan_without_ledger(cloud, local))

    text = format_rebuild(proposal)

    assert "账本重建提案" in text
    assert "记已入账" in text
    assert "留空" in text
    assert "新人" in text
