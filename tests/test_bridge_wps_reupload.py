"""Bridge 层「核对并补传」集成测试：真实 build_plan/apply_plan + 合成云端。

覆盖：只读核对零写入、整表回退补传、幂等、拒绝码、未结案、目标表变化、
令牌失效/重复提交、核对后云端变化零写入、互斥与云同步关闭。
"""
from __future__ import annotations

import datetime as _dt
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.api import bridge as bridge_module
from app.api.bridge import Bridge
from app.order.templates import write_order_template
from app.wps.journal import SyncJournal, journal_path_for
from app.wps.ledger import SyncLedger
from app.wps.sync import target_date_for
from tests.test_frontend_contract import assert_required_present
from tests.test_bridge_wps_e2e import (
    _ADDR,
    _DATE,
    _FILE_ID,
    _KIND,
    _LEFT,
    _NAME,
    _PHONE,
    _SERVED,
    _SHEET,
    _TOTAL,
    _TYPE,
    SyntheticCloud,
    _make_grid,
)

def _write_local_rows(excel: Path, target: _dt.date,
                      rows: list[dict[str, object]]) -> None:
    """用真实模板生成排单表，并按行填入真实数据（读回走 read_local_orders）。"""
    from openpyxl import load_workbook

    write_order_template(excel)
    workbook = load_workbook(excel)
    try:
        sheet = workbook[_SHEET]
        for index, row in enumerate(rows):
            at = 3 + index
            sheet.cell(row=at, column=1, value=row.get("order_no", f"W{index + 1}"))
            sheet.cell(row=at, column=2, value=row["name"])
            sheet.cell(row=at, column=3, value=row.get("address", "西溪北苑"))
            sheet.cell(row=at, column=4, value=row["phone"])
            sheet.cell(row=at, column=5 + target.weekday(), value="1")
            sheet.cell(row=at, column=12, value=row.get("meal_type", "中餐"))
            sheet.cell(row=at, column=13, value=row.get("meal_kind", "经济"))
            sheet.cell(row=at, column=14, value=row["meals"])
        workbook.save(excel)
    finally:
        workbook.close()


@pytest.fixture
def reupload_env(tmp_path, monkeypatch):
    bridge = Bridge(config_path=str(tmp_path / "config.json"))
    target = target_date_for(start_hour=bridge._config.wps_target_hour_start,
                             end_hour=bridge._config.wps_target_hour_end)
    excel = tmp_path / "排单.xlsx"
    _write_local_rows(excel, target, [
        {"name": "老王", "phone": "111", "address": "小", "meals": 2,
         "order_no": "W1"},
        {"name": "小李", "phone": "222", "address": "大西", "meals": 1,
         "order_no": "W2"},
        {"name": "小赵", "phone": "333", "address": "A1", "meals": 2,
         "meal_kind": "豪华", "order_no": "W3"},
    ])
    cloud = SyntheticCloud(_make_grid(target, [
        {_NAME: "老王", _ADDR: "小", _PHONE: "111", _TYPE: "中餐",
         _KIND: "经济", _TOTAL: "5", _SERVED: "0", _LEFT: "5"},
    ]))
    baseline_grid = dict(cloud.grid)
    ledger_path = tmp_path / "wps_sync_state.json"
    bridge._config.excel_path = excel
    bridge._config.wps_enabled = True
    bridge._config.wps_test_mode = False
    bridge._config.wps_marker_enabled = False
    bridge._config.wps_sort_enabled = False
    bridge._config.wps_tables = {_SHEET: {"file_id": _FILE_ID}}
    bridge._config.wps_production_tables = {_SHEET: {"file_id": _FILE_ID}}
    monkeypatch.setattr(bridge_module, "SyncLedger",
                        lambda *args, **kwargs: SyncLedger(ledger_path))
    monkeypatch.setattr(bridge, "_wps_cli", lambda: cloud)
    return SimpleNamespace(
        bridge=bridge, cloud=cloud, target=target, excel=excel,
        ledger_path=ledger_path, baseline_grid=baseline_grid, tmp_path=tmp_path)


def _upload_once(env) -> dict:
    preview = env.bridge.wps_preview()
    assert preview["ok"] is True, preview
    uploaded = env.bridge.wps_upload(preview["preview_id"])
    assert uploaded["ok"] is True, uploaded
    return uploaded


def _journal(env) -> SyncJournal:
    return SyncJournal(journal_path_for(env.ledger_path))


# ----------------------------------------------------------------------
# 正向：整表回退 → 核对 → 只补缺 → 幂等
# ----------------------------------------------------------------------
def test_full_cloud_revert_check_and_resend(reupload_env):
    env = reupload_env
    _upload_once(env)
    env.cloud.grid = dict(env.baseline_grid)   # “上传成功但云端没显示”

    checked = env.bridge.wps_reupload_check([_SHEET])
    assert checked["ok"] is True, checked
    assert checked["status"] == "check_ready", checked
    assert checked["check_id"].startswith("rw-")
    assert checked["summary"]["to_resend"] == 3
    table = checked["tables"][0]
    assert table["sheet"] == _SHEET
    assert table["status"] == "ready"
    assert table["counts"] == {"total": 3, "to_resend": 3, "present": 0,
                               "manual": 0}

    repaired = env.bridge.wps_reupload_upload(checked["check_id"])
    assert repaired["ok"] is True, repaired
    assert repaired["status"] in ("ok", "success"), repaired
    assert repaired["written"] >= 1

    # 云端恢复为上次上传的期望值：老王 5+2=7，两位新客户各 1/2 餐。
    rows = {}
    for (row0, col), value in env.cloud.grid.items():
        if row0 >= 2:
            rows.setdefault(row0, {})[col] = value
    totals = {row[_NAME]: row.get(_TOTAL) for row in rows.values() if row.get(_NAME)}
    marks = {row[_NAME]: row.get(_DATE) for row in rows.values() if row.get(_NAME)}
    assert totals == {"老王": "7", "小李": "1", "小赵": "2"}, totals
    assert marks == {"老王": "1", "小李": "1", "小赵": "1"}, marks

    # 再核对：全部已在云端，补传 noop。
    recheck = env.bridge.wps_reupload_check([_SHEET])
    assert recheck["ok"] is True
    assert recheck["status"] == "noop", recheck
    assert recheck["summary"]["to_resend"] == 0

    # 账本锚点 = 本批本地餐次，普通再预览是 no-op（不会重复加餐）。
    ledger = SyncLedger(env.ledger_path)
    assert ledger.synced_local(env.target.isoformat(), _FILE_ID, "老王", "111") == 2
    preview = env.bridge.wps_preview()
    assert preview["ok"] is True
    planned = preview["summary"]
    assert planned.get("to_update", 0) == 0
    assert planned.get("to_append", 0) == 0


def test_check_is_read_only(reupload_env):
    env = reupload_env
    _upload_once(env)
    env.cloud.grid = dict(env.baseline_grid)
    journal = _journal(env)
    ledger_before = Path(env.ledger_path).read_text(encoding="utf-8")
    journal_before = journal.path.read_text(encoding="utf-8")
    cloud_before = dict(env.cloud.grid)
    writes_before = len(env.cloud.writes)
    inserts_before = len(env.cloud.inserts)

    checked = env.bridge.wps_reupload_check([_SHEET])
    assert checked["ok"] is True

    assert env.cloud.grid == cloud_before
    assert len(env.cloud.writes) == writes_before
    assert len(env.cloud.inserts) == inserts_before
    assert Path(env.ledger_path).read_text(encoding="utf-8") == ledger_before
    assert journal.path.read_text(encoding="utf-8") == journal_before


def test_partial_revert_only_resends_missing(reupload_env):
    env = reupload_env
    _upload_once(env)
    # 只回退老王：总餐次与日期格恢复原值，新客户行仍在。
    env.cloud.grid[(2, _DATE)] = ""
    env.cloud.grid[(2, _TOTAL)] = "5"

    checked = env.bridge.wps_reupload_check([_SHEET])
    assert checked["ok"] is True, checked
    assert checked["status"] == "check_ready"
    assert checked["summary"]["to_resend"] == 1
    assert checked["summary"]["present"] == 2
    repaired = env.bridge.wps_reupload_upload(checked["check_id"])
    assert repaired["ok"] is True, repaired
    assert env.cloud.grid[(2, _TOTAL)] == "7"
    assert env.cloud.grid[(2, _DATE)] == "1"


# ----------------------------------------------------------------------
# 拒绝 / 闸门
# ----------------------------------------------------------------------
def test_disabled_rejects_check_and_upload(reupload_env):
    env = reupload_env
    env.bridge._config.wps_enabled = False
    checked = env.bridge.wps_reupload_check([_SHEET])
    assert checked["ok"] is False
    assert checked["code"] == "wps_disabled"
    uploaded = env.bridge.wps_reupload_upload("rw-fake")
    assert uploaded["ok"] is False
    assert uploaded["code"] == "wps_disabled"


def test_no_upload_record_is_rejected(reupload_env):
    checked = reupload_env.bridge.wps_reupload_check()
    assert checked["ok"] is False
    assert checked["code"] == "no_upload_record"
    # 明确点名一张表时，按“这张表没有上传记录”逐表说明（方便界面展示）。
    explicit = reupload_env.bridge.wps_reupload_check([_SHEET])
    assert explicit["ok"] is True
    assert explicit["status"] == "blocked"
    assert "没有找到这张表的上传记录" in explicit["tables"][0]["reason"]


def test_token_cannot_be_reused(reupload_env):
    env = reupload_env
    _upload_once(env)
    env.cloud.grid = dict(env.baseline_grid)
    checked = env.bridge.wps_reupload_check([_SHEET])
    assert checked["ok"] is True
    first = env.bridge.wps_reupload_upload(checked["check_id"])
    assert first["ok"] is True, first
    replay = env.bridge.wps_reupload_upload(checked["check_id"])
    assert replay["ok"] is False
    assert replay["code"] == "check_consumed"


def test_cloud_changed_after_check_writes_nothing(reupload_env):
    env = reupload_env
    _upload_once(env)
    env.cloud.grid = dict(env.baseline_grid)
    checked = env.bridge.wps_reupload_check([_SHEET])
    assert checked["ok"] is True
    writes_before = len(env.cloud.writes)
    inserts_before = len(env.cloud.inserts)

    env.cloud.grid[(2, _TOTAL)] = "6"   # 核对之后协作者改了云端总餐次
    repaired = env.bridge.wps_reupload_upload(checked["check_id"])
    assert repaired["ok"] is False
    assert repaired["code"] == "check_changed"
    assert len(env.cloud.writes) == writes_before
    assert len(env.cloud.inserts) == inserts_before


def test_pending_record_blocks_check_write(reupload_env):
    env = reupload_env
    _upload_once(env)
    env.cloud.grid = dict(env.baseline_grid)
    journal = _journal(env)
    terminal, _pending, _ = __import__(
        "app.wps.reupload", fromlist=["latest_sheet_records"]
    ).latest_sheet_records(journal)
    ref = terminal[_SHEET]
    journal.set_sheet_status(ref.operation_id, ref.sheet_key, "uncertain",
                             reason="测试未结案",
                             next_action="manual_reconcile")
    journal.save()

    checked = env.bridge.wps_reupload_check([_SHEET])
    assert checked["ok"] is True
    assert checked["status"] == "blocked"
    assert checked["tables"][0]["pending"] is True
    assert "未结案" in checked["tables"][0]["reason"]

    writes_before = len(env.cloud.writes)
    result = env.bridge.wps_reupload_upload(checked["check_id"])
    assert result["ok"] is False
    assert result["code"] == "check_not_ready"
    assert len(env.cloud.writes) == writes_before


def test_target_table_changed_blocks_check(reupload_env):
    env = reupload_env
    _upload_once(env)
    env.bridge._config.wps_test_mode = True
    env.bridge._config.wps_test_tables = {_SHEET: "TEST-COPY-FILE"}

    checked = env.bridge.wps_reupload_check([_SHEET])
    assert checked["ok"] is True
    assert checked["status"] == "blocked"
    assert "目标云文档已变化" in checked["tables"][0]["reason"]


def test_config_change_invalidates_token(reupload_env):
    env = reupload_env
    _upload_once(env)
    env.cloud.grid = dict(env.baseline_grid)
    checked = env.bridge.wps_reupload_check([_SHEET])
    assert checked["ok"] is True
    env.bridge.save_wps_config({"marker_enabled": True})
    result = env.bridge.wps_reupload_upload(checked["check_id"])
    assert result["ok"] is False
    assert result["code"] in ("check_invalidated", "check_changed")


def test_operation_conflict_rejects_check(reupload_env):
    env = reupload_env
    _upload_once(env)
    reservation = env.bridge._operations.try_reserve(
        "order", summary={"title": "订单处理任务"})
    assert reservation.granted
    try:
        checked = env.bridge.wps_reupload_check([_SHEET])
        assert checked["ok"] is False
        assert checked["code"] == "operation_conflict"
    finally:
        env.bridge._operations.finish(
            reservation.operation, status="success", reason="",
            summary={}, next_action="")


def test_worker_alive_rejects_resend(reupload_env, monkeypatch):
    env = reupload_env
    _upload_once(env)
    env.cloud.grid = dict(env.baseline_grid)
    checked = env.bridge.wps_reupload_check([_SHEET])
    assert checked["ok"] is True
    monkeypatch.setattr(env.bridge, "worker_alive", lambda: True)
    result = env.bridge.wps_reupload_upload(checked["check_id"])
    assert result["ok"] is False
    assert result["code"] == "worker_busy"


# ----------------------------------------------------------------------
# 选表补传（前端只补勾选的子表）
# ----------------------------------------------------------------------
def test_resend_only_selected_sheets(reupload_env):
    env = reupload_env
    _upload_once(env)
    env.cloud.grid = dict(env.baseline_grid)
    checked = env.bridge.wps_reupload_check([_SHEET])
    assert checked["ok"] is True

    repaired = env.bridge.wps_reupload_upload(checked["check_id"], [_SHEET])
    assert repaired["ok"] is True, repaired
    assert [table["sheet"] for table in repaired["tables"]] == [_SHEET]
    assert env.cloud.grid[(2, _TOTAL)] == "7"


def test_resend_subset_must_come_from_check_result(reupload_env):
    env = reupload_env
    _upload_once(env)
    env.cloud.grid = dict(env.baseline_grid)
    checked = env.bridge.wps_reupload_check([_SHEET])
    assert checked["ok"] is True
    writes_before = len(env.cloud.writes)

    bad = env.bridge.wps_reupload_upload(checked["check_id"], ["不存在的表"])
    assert bad["ok"] is False
    assert bad["code"] == "bad_sheets"
    empty = env.bridge.wps_reupload_upload(checked["check_id"], [])
    assert empty["ok"] is False
    assert empty["code"] == "empty_sheets"
    assert len(env.cloud.writes) == writes_before


# ----------------------------------------------------------------------
# 前端契约（TS 接口必需的字段一个都不能少）
# ----------------------------------------------------------------------
def test_reupload_check_satisfies_frontend_contract(reupload_env):
    env = reupload_env
    _upload_once(env)
    env.cloud.grid = dict(env.baseline_grid)
    checked = env.bridge.wps_reupload_check([_SHEET])
    assert_required_present("WpsReuploadCheckResult", checked)
    for table in checked["tables"]:
        assert_required_present("WpsReuploadTable", table)
        for order in table["orders"]:
            assert_required_present("WpsReuploadOrder", order)


def test_reupload_upload_satisfies_frontend_contract(reupload_env):
    env = reupload_env
    _upload_once(env)
    env.cloud.grid = dict(env.baseline_grid)
    checked = env.bridge.wps_reupload_check([_SHEET])
    repaired = env.bridge.wps_reupload_upload(checked["check_id"])
    assert_required_present("WpsReuploadUploadResult", repaired)
    for table in repaired["tables"]:
        assert_required_present("WpsReuploadTable", table)


def test_reupload_check_noop_satisfies_contract(reupload_env):
    env = reupload_env
    _upload_once(env)
    checked = env.bridge.wps_reupload_check([_SHEET])
    assert_required_present("WpsReuploadCheckResult", checked)


# ----------------------------------------------------------------------
# 写失败/结果未知：绝不能把没写进去的餐记成已同步（账本锚点必须不动）
# ----------------------------------------------------------------------
@pytest.mark.parametrize("sheet_status", ["failed", "uncertain"])
def test_unsuccessful_write_leaves_ledger_untouched(reupload_env, monkeypatch,
                                                    sheet_status):
    env = reupload_env
    _upload_once(env)
    env.cloud.grid = dict(env.baseline_grid)
    checked = env.bridge.wps_reupload_check([_SHEET])
    assert checked["ok"] is True
    ledger_before = Path(env.ledger_path).read_text(encoding="utf-8")

    def fake_apply_plan(*args, **kwargs):
        return {
            "status": sheet_status, "uncertain": sheet_status == "uncertain",
            "reason": "测试：云端没有写成功", "next_action": "manual_reconcile",
            "sheets": [{"sheet": _SHEET, "status": sheet_status,
                        "reason": "测试：云端没有写成功",
                        "uncertain": sheet_status == "uncertain"}],
            "written": 0, "failed": 1,
        }

    monkeypatch.setattr(bridge_module, "apply_plan", fake_apply_plan)
    repaired = env.bridge.wps_reupload_upload(checked["check_id"])
    assert repaired["ok"] is False, repaired
    assert repaired["tables"][0]["write"]["status"] == sheet_status

    # 账本锚点不得被 refresh_plan_ledger_entries 提前记成「已同步」：
    # 否则下次普通预览直接 no-op，漏传再也补不回来。
    assert Path(env.ledger_path).read_text(encoding="utf-8") == ledger_before
    ledger = SyncLedger(env.ledger_path)
    assert ledger.synced_local(env.target.isoformat(), _FILE_ID,
                               "老王", "111") == 2
    assert ledger.synced_local(env.target.isoformat(), _FILE_ID,
                               "小李", "222") == 1
