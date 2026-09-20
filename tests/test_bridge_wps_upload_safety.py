"""WPS 预览令牌与上传前复核的安全回归。

覆盖：无参拒绝、缺失/过期/已消费、成功一次性上传、重放、本地/配置/计划变化、
零写入、异常释放、并发同一令牌只允许一个 apply、uncertain 不当成功。
所有外部依赖均为合成替身，不联网、不读密钥、不写云端。
"""
from __future__ import annotations

import copy
import datetime as _dt
import threading
import time

import pytest

from app.api import bridge as bridge_module
from app.api.bridge import Bridge
from app.wps.errors import LedgerCorruptError
from app.wps.models import Change, SheetPlan


class _Cli:
    path = "/fake/kdocs-cli"

    def authenticated(self) -> bool:
        return True


def _make_plan(state: dict):
    change = Change(
        kind="existing",
        name="合成客户",
        phone="13800000000",
        row=4,
        delta=int(state.get("delta", 1)),
        target_col=6,
        total_before=int(state.get("total_before", 2)),
        total_after=int(state.get("total_before", 2)) + int(state.get("delta", 1)),
        target_ok=True,
        target_occupied=str(state.get("target_occupied", "")),
        local_rows=(3,),
        slot=1,
        local_meals=int(state.get("delta", 1)),
    )
    return SheetPlan(
        sheet="东湖中餐",
        file_id="F1",
        target_date=_dt.date(2026, 9, 20),
        target_col=6,
        target_header=str(state.get("header", "9.20")),
        weekday_number=5,
        changes=[change],
        columns={"name": 1, "phone": 3, "address": 2},
        sort_enabled=bool(state.get("sort_enabled", False)),
        sort_key_col=18 if state.get("sort_enabled") else 0,
        sort_range="A3:R100" if state.get("sort_enabled") else "",
        row_keys={row: row for row in range(3, 8)},
        last_data_row=6,
        blocked_reason=str(state.get("blocked_reason", "")),
    )


@pytest.fixture
def wps_env(tmp_path, monkeypatch):
    state: dict = {
        "excel_bytes": b"v1",
        "delta": 1,
        "total_before": 2,
        "header": "9.20",
        "sort_enabled": False,
        "blocked_reason": "",
    }
    excel = tmp_path / "排单.xlsx"
    excel.write_bytes(state["excel_bytes"])
    bridge = Bridge(config_path=str(tmp_path / "config.json"), is_admin=True)
    bridge._config.excel_path = excel
    bridge._config.wps_test_mode = True
    bridge._config.wps_marker_enabled = True
    # W5：云同步默认关闭时服务端拒绝预览/上传；本文件测的是已开启时的令牌流程。
    bridge._config.wps_enabled = True

    captured: dict = {"build_calls": 0, "build_kwargs": [], "apply_calls": [],
                      "ledger_saves": 0, "apply_result": None,
                      "cli": _Cli()}

    class _Ledger:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def save(self):
            captured["ledger_saves"] += 1
            return tmp_path / "ledger.json"

    monkeypatch.setattr(bridge_module, "effective_tables",
                        lambda _cfg: {"东湖中餐": {"file_id": "F1"}})
    monkeypatch.setattr(bridge_module, "SyncLedger", _Ledger)
    monkeypatch.setattr(bridge_module, "read_local_orders",
                        lambda path, log=None: {"东湖中餐": []})

    def fake_build_plan(cli, **kwargs):
        captured["build_calls"] += 1
        captured["build_kwargs"].append(kwargs)
        return [_make_plan(state)]

    def fake_apply_plan(cli, plans, **kwargs):
        captured["apply_calls"].append({"plans": copy.deepcopy(list(plans)),
                                        "kwargs": kwargs})
        if captured["apply_result"] is not None:
            return copy.deepcopy(captured["apply_result"])
        return {"sheets": [{"sheet": "东湖中餐", "status": "ok"}],
                "written": 1, "failed": 0}

    monkeypatch.setattr(bridge_module, "build_plan", fake_build_plan)
    monkeypatch.setattr(bridge_module, "format_plan", lambda plans: "合成预览正文")
    monkeypatch.setattr(bridge_module, "summarize_plan", lambda plans: {
        "to_update": 1, "to_append": 0, "unchanged": 0, "skipped": 0, "warned": 0})
    monkeypatch.setattr(bridge_module, "apply_plan", fake_apply_plan)
    monkeypatch.setattr(bridge, "_wps_cli",
                        lambda: captured["cli"])  # type: ignore[method-assign]
    return bridge, excel, state, captured


def _preview(env):
    bridge, _excel, _state, captured = env
    got = bridge.wps_preview()
    assert got["ok"] is True, got
    return got


def test_preview_returns_one_shot_token_and_structured_changes(wps_env):
    bridge, _excel, _state, _captured = wps_env
    got = _preview(wps_env)

    for key in ("preview_id", "created_at", "expires_at", "expires_in",
                "local_sha256", "context_fingerprint", "plan_fingerprint",
                "fingerprint", "target_tables", "tables", "blocked", "warnings",
                "stats", "summary", "text", "target_date", "test_mode"):
        assert key in got, key
    assert got["status"] == "preview_ready"
    assert got["next_action"] == "wps_upload(preview_id)"
    assert 0 < got["expires_in"] <= 600
    assert len(got["local_sha256"]) == 64
    assert got["tables"][0]["sheet"] == "东湖中餐"
    change = got["tables"][0]["changes"][0]
    # 受影响行的旧值必须可见，前端才能核对“改哪一行、从几改成几”。
    assert change["total_before"] == 2
    assert change["total_after"] == 3
    assert change["target_ok"] is True
    assert change["row"] == 4
    # 预览只读：不落账本。
    assert _captured["ledger_saves"] == 0


def test_legacy_no_argument_upload_is_refused_with_zero_writes(wps_env):
    bridge, _excel, _state, captured = wps_env
    before_build = captured["build_calls"]

    got = bridge.wps_upload()

    assert got["ok"] is False
    assert got["code"] == "missing_preview"
    assert got["status"] == "rejected"
    assert captured["build_calls"] == before_build, "无参上传绝不能开始读云端/构建计划"
    assert captured["apply_calls"] == []


def test_upload_success_consumes_token_and_second_upload_is_replay(wps_env):
    bridge, _excel, _state, captured = wps_env
    preview = _preview(wps_env)

    first = bridge.wps_upload(preview["preview_id"])
    second = bridge.wps_upload(preview["preview_id"])

    assert first["ok"] is True
    assert first["status"] == "success"
    assert first["preview_id"] == preview["preview_id"]
    assert first["operation_id"].startswith("op-")
    assert len(captured["apply_calls"]) == 1
    assert second["ok"] is False
    assert second["code"] == "preview_consumed"
    assert len(captured["apply_calls"]) == 1, "重放不得再次 apply"


def test_missing_preview_id_returns_not_found_with_zero_writes(wps_env):
    bridge, _excel, _state, captured = wps_env
    got = bridge.wps_upload("pv-does-not-exist")
    assert got["ok"] is False
    assert got["code"] == "preview_not_found"
    assert captured["apply_calls"] == []


def test_expired_preview_is_refused_with_zero_writes(wps_env):
    bridge, _excel, _state, captured = wps_env
    preview = _preview(wps_env)
    record = bridge._previews._items[preview["preview_id"]]
    record.expires_at = time.time() - 1.0

    got = bridge.wps_upload(preview["preview_id"])

    assert got["ok"] is False
    assert got["code"] == "preview_expired"
    assert captured["apply_calls"] == []


def test_local_file_change_between_preview_and_upload_is_refused_zero_write(wps_env):
    bridge, excel, _state, captured = wps_env
    preview = _preview(wps_env)
    excel.write_bytes(b"v2")

    got = bridge.wps_upload(preview["preview_id"])

    assert got["ok"] is False
    assert got["code"] == "preview_changed"
    assert captured["apply_calls"] == [], "复核失败必须在 apply 之前返回"
    # 失效后重试也不能绕过，必须重新预览。
    again = bridge.wps_upload(preview["preview_id"])
    assert again["ok"] is False
    assert again["code"] == "preview_changed"


def test_config_change_between_preview_and_upload_is_refused(wps_env):
    bridge, _excel, _state, captured = wps_env
    preview = _preview(wps_env)
    bridge._config.wps_sort_enabled = not bridge._config.wps_sort_enabled

    got = bridge.wps_upload(preview["preview_id"])

    assert got["ok"] is False
    assert got["code"] == "preview_changed"
    assert "sort_enabled" in got["summary"]["changed"]
    assert captured["apply_calls"] == []


def test_cloud_plan_change_old_value_and_header_are_refused(wps_env):
    bridge, _excel, state, captured = wps_env
    preview = _preview(wps_env)

    # 只改“受影响行的旧值/表头”这类危险输入，本地文件与配置完全不动。
    state["total_before"] = 99
    state["header"] = "9.21"
    got = bridge.wps_upload(preview["preview_id"])

    assert got["ok"] is False
    assert got["code"] == "preview_changed"
    assert captured["apply_calls"] == []


def test_sort_input_change_is_refused(wps_env):
    bridge, _excel, state, captured = wps_env
    state["sort_enabled"] = True
    preview = _preview(wps_env)

    state["sort_enabled"] = False
    got = bridge.wps_upload(preview["preview_id"])

    assert got["ok"] is False
    assert got["code"] == "preview_changed"
    assert captured["apply_calls"] == []


@pytest.mark.parametrize("apply_result,expected_status,expected_code", [
    ({"sheets": [{"sheet": "东湖中餐", "status": "uncertain",
                  "reason": "回读超时"}],
      "written": 0, "failed": 0, "uncertain": True,
      "next_action": "manual_reconcile"},
     "uncertain", "uncertain"),
    ({"sheets": [{"sheet": "东湖中餐", "status": "failed",
                  "reason": "写入失败"}],
      "written": 0, "failed": 1,
      "next_action": "repreview"},
     "failed", "cloud_write_failed"),
])
def test_uncertain_and_failed_are_not_reported_as_success(
        wps_env, apply_result, expected_status, expected_code):
    bridge, _excel, _state, captured = wps_env
    preview = _preview(wps_env)
    captured["apply_result"] = apply_result

    got = bridge.wps_upload(preview["preview_id"])

    assert got["ok"] is False
    assert got["status"] == expected_status
    if expected_code:
        assert got["code"] == expected_code
    assert captured["apply_calls"], "已消费令牌并进入执行器"
    if expected_status == "uncertain":
        assert got.get("uncertain") is True
        assert got["next_action"] == "只读核对，不重新上传"
        assert "只读核对，不重新上传" in got["reason"]
        assert got["executor_next_action"] == "manual_reconcile"
        operation = bridge.operation_status(got["operation_id"])
        assert operation["status"] == "uncertain"
        assert operation["next_action"] == got["next_action"]
    if expected_status == "failed":
        assert got["failed"] == 1
        assert got["written"] == 0
        assert got["next_action"] == "repreview"
        assert got["result"]["failed"] == 1
        operation = bridge.operation_status(got["operation_id"])
        assert operation["status"] == "failed"
        assert operation["next_action"] == got["next_action"]


def test_executor_top_level_uncertain_status_is_not_success(wps_env):
    bridge, _excel, _state, captured = wps_env
    preview = _preview(wps_env)
    captured["apply_result"] = {
        "status": "uncertain", "sheets": [], "written": 0, "failed": 0,
        "next_action": "先核对",
    }

    got = bridge.wps_upload(preview["preview_id"])

    assert got["ok"] is False
    assert got["status"] == "uncertain"
    assert got.get("uncertain") is True
    assert got["next_action"] == "只读核对，不重新上传"
    assert "只读核对，不重新上传" in got["reason"]
    operation = bridge.operation_status(got["operation_id"])
    assert operation["status"] == "uncertain"
    assert operation["next_action"] == got["next_action"]


def test_apply_plan_verified_status_maps_to_success(wps_env):
    """B 执行器 status=verified 必须按成功承接（旧 ok/noop 之外的新成功状态）。"""
    bridge, _excel, _state, captured = wps_env
    preview = _preview(wps_env)
    captured["apply_result"] = {
        "status": "verified",
        "uncertain": False,
        "written": 1,
        "failed": 0,
        "next_action": "",
        "sheets": [{"sheet": "东湖中餐", "status": "verified",
                    "reason": "", "next_action": "none"}],
        "summary": {"written": 1, "failed": 0, "uncertain": False,
                    "sheets": 1, "next_action": ""},
    }

    got = bridge.wps_upload(preview["preview_id"])

    assert got["ok"] is True
    assert got["status"] == "success"
    assert got["failed"] == 0 and got["written"] == 1
    assert got["next_action"] == ""
    assert got["executor_status"] == "verified"
    operation = bridge.operation_status(got["operation_id"])
    assert operation["status"] == "success"
    assert operation["active"] is False


@pytest.mark.parametrize("executor_status,written,failed,next_action", [
    ("partial", 1, 1, "repreview"),
    ("partial", 1, 0, "repreview"),
    ("failed", 0, 1, "repreview"),
])
def test_partial_failed_preserve_counts_summary_next_action(
        wps_env, executor_status, written, failed, next_action):
    bridge, _excel, _state, captured = wps_env
    preview = _preview(wps_env)
    captured["apply_result"] = {
        "status": executor_status,
        "uncertain": False,
        "written": written,
        "failed": failed,
        "next_action": next_action,
        "sheets": [
            {"sheet": "东湖中餐", "status": "ok" if written else "failed",
             "reason": "", "next_action": "none"},
            {"sheet": "衣锦中餐", "status": "failed", "reason": "写入失败",
             "next_action": next_action},
        ],
        "summary": {"written": written, "failed": failed, "uncertain": False,
                    "sheets": 2, "next_action": next_action},
    }

    got = bridge.wps_upload(preview["preview_id"])

    assert got["ok"] is False
    assert got["status"] == executor_status
    assert got["written"] == written
    assert got["failed"] == failed
    assert got["next_action"] == next_action
    assert got["result"]["summary"]["failed"] == failed
    operation = bridge.operation_status(got["operation_id"])
    assert operation["status"] == executor_status
    assert operation["next_action"] == next_action


@pytest.mark.parametrize("recovery_status", ["recovered", "not_started"])
def test_recovery_status_repreview_and_operation_status_consistent(
        wps_env, recovery_status):
    bridge, _excel, _state, captured = wps_env
    preview = _preview(wps_env)
    captured["apply_result"] = {
        "status": recovery_status,
        "uncertain": False,
        "written": 0,
        "failed": 0,
        "next_action": "repreview",
        "sheets": [{"sheet": "东湖中餐",
                    "status": "verified" if recovery_status == "recovered"
                    else "not_started",
                    "next_action": "none" if recovery_status == "recovered"
                    else "repreview"}],
        "recovery": {"status": recovery_status, "next_action": "repreview"},
        "journal_path": "/tmp/fake.journal",
        "summary": {"written": 0, "failed": 0, "uncertain": False,
                    "sheets": 1, "next_action": "repreview"},
    }

    got = bridge.wps_upload(preview["preview_id"])

    assert got["ok"] is False
    assert got["status"] == recovery_status
    assert got["next_action"] == "repreview"
    assert got["recovery"] == {"status": recovery_status,
                               "next_action": "repreview"}
    assert got["journal_path"] == "/tmp/fake.journal"
    operation = bridge.operation_status(got["operation_id"])
    assert operation["status"] == recovery_status
    assert operation["next_action"] == got["next_action"]
    assert operation["summary"]["recovery"] == got["recovery"]
    assert operation["summary"]["journal_path"] == "/tmp/fake.journal"


def test_blocked_journal_matches_operation_status_and_never_success(wps_env):
    bridge, _excel, _state, captured = wps_env
    preview = _preview(wps_env)
    captured["apply_result"] = {
        "status": "blocked",
        "uncertain": True,
        "written": 0,
        "failed": 1,
        "next_action": "fix_journal",
        "sheets": [{"sheet": "东湖中餐", "status": "blocked",
                    "reason": "意图日志不可写", "next_action": "fix_journal",
                    "uncertain": True}],
        "journal_path": "/tmp/fake.journal",
        "summary": {"written": 0, "failed": 1, "uncertain": True,
                    "sheets": 1, "next_action": "fix_journal"},
    }

    got = bridge.wps_upload(preview["preview_id"])

    assert got["ok"] is False
    assert got["status"] == "blocked"
    assert got["next_action"] == "fix_journal"
    assert got["uncertain"] is True
    assert got["journal_path"] == "/tmp/fake.journal"
    operation = bridge.operation_status(got["operation_id"])
    assert operation["status"] == "blocked"
    assert operation["next_action"] == "fix_journal"


def test_corrupt_ledger_in_preview_blocks_without_build_or_apply(wps_env, monkeypatch):
    bridge, _excel, _state, captured = wps_env

    def broken_ledger(*_a, **_k):
        raise LedgerCorruptError("账本损坏，不是空账本")

    monkeypatch.setattr(bridge_module, "SyncLedger", broken_ledger)

    got = bridge.wps_preview()

    assert got["ok"] is False
    assert got["status"] == "blocked"
    assert got["code"] == "local_state_blocked"
    assert "只读核对" in got["next_action"]
    assert captured["build_calls"] == 0
    assert captured["apply_calls"] == []


def test_corrupt_ledger_in_upload_blocks_and_never_success(wps_env, monkeypatch):
    bridge, _excel, _state, captured = wps_env
    preview = _preview(wps_env)

    def broken_ledger(*_a, **_k):
        raise LedgerCorruptError("账本损坏，不是空账本")

    monkeypatch.setattr(bridge_module, "SyncLedger", broken_ledger)

    got = bridge.wps_upload(preview["preview_id"])

    assert got["ok"] is False
    assert got["status"] == "blocked"
    assert got["code"] == "local_state_blocked"
    assert "只读核对" in got["next_action"]
    # 令牌没有被消费到 apply；没有再次提交，也没有任何云端写入。
    assert captured["apply_calls"] == []
    operation = bridge.operation_status(got["operation_id"])
    assert operation["status"] == "blocked"
    assert operation["status"] != "success"


def test_apply_plan_ledger_corrupt_maps_to_blocked_not_success(wps_env, monkeypatch):
    """账本在 apply 阶段才损坏/不可用时也必须阻断，不能报 success。"""
    bridge, _excel, _state, captured = wps_env
    preview = _preview(wps_env)

    def broken_apply(*_args, **_kwargs):
        raise LedgerCorruptError("提交账本前发现损坏")

    monkeypatch.setattr(bridge_module, "apply_plan", broken_apply)

    got = bridge.wps_upload(preview["preview_id"])

    assert got["ok"] is False
    assert got["status"] == "blocked"
    assert got["code"] == "local_state_blocked"
    assert "只读核对" in got["next_action"]
    assert captured["apply_calls"] == []
    operation = bridge.operation_status(got["operation_id"])
    assert operation["status"] == "blocked"
    assert operation["status"] != "success"


def test_wps_status_survives_corrupt_ledger(wps_env, monkeypatch):
    bridge, _excel, _state, _captured = wps_env

    def broken_ledger(*_a, **_k):
        raise LedgerCorruptError("账本损坏，不能当空账本")

    monkeypatch.setattr(bridge_module, "SyncLedger", broken_ledger)

    got = bridge.wps_status()

    assert got["ok"] is False
    assert "账本损坏" in got["reason"]
    assert got["state_path"] == ""
    assert got["tables"]  # 表清单仍可展示，但不会用空账本伪造成功


@pytest.mark.parametrize("apply_result,expected_status", [
    # 未知未来状态：即使 written>0 也不能 fail-open 成 success。
    ({"status": "future_status", "written": 1, "failed": 0,
      "sheets": [{"sheet": "东湖中餐", "status": "ok"}],
      "summary": {"status": "future_status"}},
     "uncertain"),
    # 顶层 ok 但空 sheets 且有写入：验证缺失，保守 uncertain。
    ({"status": "ok", "written": 1, "failed": 0, "sheets": [],
      "summary": {"status": "ok"}},
     "uncertain"),
    # 空 sheets 且无写入也没有任何验证证据：不能 success。
    ({"status": "ok", "written": 0, "failed": 0, "sheets": [],
      "summary": {"status": "ok"}},
     "failed"),
    # sheets 畸形。
    ({"status": "ok", "written": 1, "failed": 0, "sheets": "bad",
      "summary": {"status": "ok"}},
     "uncertain"),
    # 顶层 ok 与逐表 failed 混合，且可能已写。
    ({"status": "ok", "written": 1, "failed": 1,
      "sheets": [{"sheet": "东湖中餐", "status": "ok"},
                 {"sheet": "衣锦中餐", "status": "failed",
                  "reason": "写入失败"}],
      "summary": {"status": "ok"}},
     "uncertain"),
    # 顶层 ok 与 sheet failed 矛盾，虽然 written=0。
    ({"status": "ok", "written": 0, "failed": 0,
      "sheets": [{"sheet": "东湖中餐", "status": "failed",
                  "reason": "回读失败"}],
      "summary": {"status": "ok"}},
     "uncertain"),
    # written>0 但逐表状态缺失验证。
    ({"status": "ok", "written": 1, "failed": 0,
      "sheets": [{"sheet": "东湖中餐", "status": ""}],
      "summary": {"status": "ok"}},
     "uncertain"),
    # 顶层 verified=False：明确验证缺失。
    ({"status": "ok", "written": 1, "failed": 0, "verified": False,
      "sheets": [{"sheet": "东湖中餐", "status": "ok"}],
      "summary": {"status": "ok"}},
     "uncertain"),
    # 未知状态且没有任何写入痕迹：至少不能 success。
    ({"status": "future_status", "written": 0, "failed": 0, "sheets": [],
      "summary": {"status": "future_status"}},
     "failed"),
    # 旧字段 name="success" 不是白名单，必须保守。
    ({"status": "success", "written": 1, "failed": 0,
      "sheets": [{"sheet": "东湖中餐", "status": "ok"}],
      "summary": {"status": "success"}},
     "uncertain"),
])
def test_unknown_or_unverified_executor_states_never_success(
        wps_env, apply_result, expected_status):
    bridge, _excel, _state, captured = wps_env
    preview = _preview(wps_env)
    captured["apply_result"] = apply_result

    got = bridge.wps_upload(preview["preview_id"])

    assert got["ok"] is False, got
    assert got["status"] != "success", got
    assert got["status"] == expected_status, got
    if got["status"] == "uncertain":
        assert got["uncertain"] is True
        assert got["next_action"] == "只读核对，不重新上传"
    operation = bridge.operation_status(got["operation_id"])
    assert operation["status"] == got["status"]
    assert operation["status"] != "success"


def test_explicit_ok_with_verified_sheets_still_succeeds(wps_env):
    bridge, _excel, _state, captured = wps_env
    preview = _preview(wps_env)
    captured["apply_result"] = {
        "status": "ok",
        "written": 1,
        "failed": 0,
        "uncertain": False,
        "sheets": [{"sheet": "东湖中餐", "status": "ok",
                    "reason": "", "next_action": "none"}],
        "summary": {"written": 1, "failed": 0, "uncertain": False,
                    "sheets": 1, "next_action": "none"},
    }

    got = bridge.wps_upload(preview["preview_id"])

    assert got["ok"] is True
    assert got["status"] == "success"
    assert got["failed"] == 0 and got["written"] == 1


def test_apply_exception_releases_mutex_and_token_cannot_replay(wps_env):
    bridge, _excel, _state, captured = wps_env
    preview = _preview(wps_env)

    def boom(*_a, **_k):
        raise KeyError("执行器坏了")

    bridge_module.apply_plan = boom  # type: ignore[assignment]
    got = bridge.wps_upload(preview["preview_id"])

    assert got["ok"] is False
    assert got["status"] == "failed"
    assert "KeyError" in got["reason"]
    status = bridge.operation_status(got["operation_id"])
    assert status["active"] is False
    assert status["status"] in ("error", "failed")
    # 令牌已经在 apply 前消费，不能重放造成二次写入。
    again = bridge.wps_upload(preview["preview_id"])
    assert again["code"] == "preview_consumed"


def test_operation_conflict_does_not_consume_preview(wps_env):
    bridge, _excel, _state, captured = wps_env
    preview = _preview(wps_env)
    reservation = bridge._operations.try_reserve("order", summary={"title": "订单任务"})
    assert reservation.granted

    blocked = bridge.wps_upload(preview["preview_id"])

    assert blocked["ok"] is False
    assert blocked["reason"] == "busy"
    assert captured["apply_calls"] == []
    bridge._operations.finish(reservation.operation, status="success")
    ok = bridge.wps_upload(preview["preview_id"])
    assert ok["ok"] is True
    assert len(captured["apply_calls"]) == 1


def test_concurrent_upload_same_token_only_applies_once(wps_env):
    bridge, _excel, _state, captured = wps_env
    preview = _preview(wps_env)
    entered = threading.Event()
    release = threading.Event()
    original_apply = bridge_module.apply_plan

    def slow_apply(cli, plans, **kwargs):
        entered.set()
        release.wait(2.0)
        return original_apply(cli, plans, **kwargs)

    bridge_module.apply_plan = slow_apply  # type: ignore[assignment]
    results: list[dict] = []

    def call() -> None:
        results.append(bridge.wps_upload(preview["preview_id"]))

    first = threading.Thread(target=call)
    second = threading.Thread(target=call)
    first.start()
    assert entered.wait(2.0), "第一个上传应已进入 apply 并被 barrier 停住"
    # 浏览器此时断开也没关系：另一个请求仍能查到 active/最近状态，不会被 apply 阻塞。
    assert bridge.operation_status()["active"] is True
    assert bridge.operation_status()["mode"] == "wps_upload"
    assert bridge.operation_status()["next_action"]
    assert bridge.bridge_ready()["operation"]["active"] is True
    second.start()
    time.sleep(0.05)
    release.set()
    first.join(3.0)
    second.join(3.0)

    assert len(captured["apply_calls"]) + 1 == 2  # slow_apply 未经过 captured，只加一次
    assert sum(1 for item in results if item.get("status") == "success") == 1
    blocked = [item for item in results if item.get("status") != "success"]
    assert len(blocked) == 1
    assert blocked[0]["code"] in ("preview_consumed", "missing_preview", "operation_conflict")


def test_preview_changed_does_not_touch_apply_on_config_fingerprint(wps_env):
    bridge, _excel, _state, captured = wps_env
    preview = _preview(wps_env)
    bridge._config.wps_marker_enabled = not bridge._config.wps_marker_enabled
    got = bridge.wps_upload(preview["preview_id"])
    assert got["code"] == "preview_changed"
    assert captured["apply_calls"] == []
