"""Bridge 只读恢复接口回归：WPS recovery_status 契约 + pending interaction 归属/无副作用。

全部离线：WPS 契约用 monkeypatch，交互只操作内存字典，不读真实 keyring/云端。
"""
from __future__ import annotations

import json
import time
import types

from app.api import bridge as bridge_module
from app.api.bridge import Bridge
from app.wps.errors import LedgerCorruptError


def _bridge(tmp_path) -> Bridge:
    return Bridge(config_path=str(tmp_path / "config.json"), is_admin=True)


RAW_LEAK_MARKERS = ("张三", "13800000000", "浙江农林大学", "豪华餐",
                   "SENSITIVE_FUTURE_FIELD", "异常原文", "FILE-CUSTOMER-ID",
                   "总餐次应为")


def _raw_leaky_recovery_result() -> dict:
    return {
        "ok": True,
        "journal_path": "/tmp/private.journal",
        "operations": [{
            "operation_id": "张三-13800000000-wps-secret",
            "operation_ref": "wps-op:raw-secret",
            "status": "uncertain",
            "pending": True,
            "target_date": "2026-09-20",
            "target_refs": ["张三 13800000000 浙江农林大学"],
            "sheets": [{
                "sheet": "东湖中餐",
                "file_id": "FILE-CUSTOMER-ID",
                "target_date": "2026-09-20",
                "target_ref": "张三 13800000000 浙江农林大学",
                "status": "uncertain",
                "raw_status": "uncertain",
                "risk_reason": "张三: 总餐次应为 9，实际 3",
                "reason": "异常原文",
                "problems": ["13800000000 浙江农林大学 豪华餐 9"],
                "next_action": "manual_reconcile",
                "manual_required": "张三",
            }],
        }],
        "pending_operations": [{"operation_id": "张三-13800000000-wps-secret"}],
        "counts": {"uncertain": 1, "future_unknown_count": 99},
        "next_action": "manual_reconcile",
        "future_secret": "SENSITIVE_FUTURE_FIELD",
        "error_code": "",
    }


def _assert_no_leak(payload) -> None:
    text = json.dumps(payload, ensure_ascii=False)
    for marker in RAW_LEAK_MARKERS:
        assert marker not in text, f"响应泄漏敏感标记：{marker}\n{text}"


def test_wps_recovery_status_non_admin_gets_whitelisted_safe_summary(
        tmp_path, monkeypatch):
    bridge = Bridge(config_path=str(tmp_path / "config.json"), is_admin=False)
    monkeypatch.setattr(
        bridge_module, "_wps_recovery_status_contract",
        lambda ledger: _raw_leaky_recovery_result())
    monkeypatch.setattr(
        bridge_module, "SyncLedger",
        lambda *a, **k: types.SimpleNamespace(
            path=tmp_path / "ledger.json",
            journal_path=tmp_path / "ledger.json.journal"))

    got = bridge.wps_recovery_status()

    _assert_no_leak(got)
    assert got["ok"] is True
    assert got["scope"] == "summary"
    assert "operations" not in got
    assert "pending_operations" not in got
    assert got["counts"]["uncertain"] == 1
    assert "future_unknown_count" not in got["counts"]
    assert got["next_action"] == "manual_reconcile"
    assert got["summary"]["needs_review"] is True
    assert "只读核对" in got["summary"]["guidance"]


def test_wps_recovery_status_admin_gets_only_whitelisted_safe_dto(
        tmp_path, monkeypatch):
    bridge = _bridge(tmp_path)
    monkeypatch.setattr(
        bridge_module, "_wps_recovery_status_contract",
        lambda ledger: _raw_leaky_recovery_result())
    monkeypatch.setattr(
        bridge_module, "SyncLedger",
        lambda *a, **k: types.SimpleNamespace(
            path=tmp_path / "ledger.json",
            journal_path=tmp_path / "ledger.json.journal"))

    got = bridge.wps_recovery_status()

    _assert_no_leak(got)
    assert got["scope"] == "admin"
    assert "future_secret" not in got
    assert "journal_path" not in got
    assert "pending_operations" in got
    operation = got["operations"][0]
    assert operation["operation_id"] == ""       # 非白名单格式必须置空
    assert operation["operation_ref"] == ""
    assert operation["target_refs"] == []
    assert operation["status"] == "uncertain"
    sheet = operation["sheets"][0]
    # 原始 sheet/file_id/risk_reason/problems/reason 与未知字段必须完全不在响应中。
    assert "sheet" not in sheet
    assert "file_id" not in sheet
    assert "risk_reason" not in sheet
    assert "problems" not in sheet
    assert "reason" not in sheet
    assert set(sheet) <= {
        "target_date", "target_ref", "status", "raw_status", "error_code",
        "allowed_next_actions", "manual_required", "cloud_checked", "evidence",
    }


def test_wps_recovery_status_failure_hides_exception_text_for_all_viewers(
        tmp_path, monkeypatch):
    for is_admin in (False, True):
        bridge = Bridge(config_path=str(tmp_path / f"config-{is_admin}.json"),
                        is_admin=is_admin)
        monkeypatch.setattr(
            bridge_module, "SyncLedger",
            lambda *a, **k: types.SimpleNamespace(
                path=tmp_path / "ledger.json",
                journal_path=tmp_path / "ledger.json.journal"))

        def boom(_ledger):
            raise RuntimeError("异常原文 SENSITIVE_FUTURE_FIELD 张三 13800000000")

        monkeypatch.setattr(bridge_module, "_wps_recovery_status_contract", boom)
        got = bridge.wps_recovery_status()

        _assert_no_leak(got)
        assert got["ok"] is False
        assert got["error_code"] == "wps_recovery_internal_error"
        assert got["next_action"] == "fix_journal"
        assert "reason" not in got
        if not is_admin:
            assert "operations" not in got
        else:
            assert got["operations"] == []


def test_wps_recovery_status_corrupt_ledger_is_safe_failure(tmp_path, monkeypatch):
    bridge = _bridge(tmp_path)

    def broken_ledger(*_a, **_k):
        raise LedgerCorruptError("账本损坏 张三 13800000000")

    monkeypatch.setattr(bridge_module, "SyncLedger", broken_ledger)

    got = bridge.wps_recovery_status()

    _assert_no_leak(got)
    assert got["ok"] is False
    assert got["error_code"] == "wps_recovery_ledger_unreadable"
    assert got["next_action"] == "fix_journal"
    assert got["operations"] == []
    assert got["pending_operations"] == []


def test_wps_recovery_status_contract_failure_shape_is_safe(tmp_path, monkeypatch):
    bridge = _bridge(tmp_path)
    monkeypatch.setattr(
        bridge_module, "SyncLedger",
        lambda *a, **k: types.SimpleNamespace(
            path=tmp_path / "ledger.json",
            journal_path=tmp_path / "ledger.json.journal"))

    def boom(_ledger):
        raise RuntimeError("contract boom")

    monkeypatch.setattr(bridge_module, "_wps_recovery_status_contract", boom)

    got = bridge.wps_recovery_status()

    assert got["ok"] is False
    assert got["error_code"] == "wps_recovery_internal_error"
    assert got["next_action"] == "fix_journal"
    assert got["scope"] == "admin"


def test_pending_admin_non_owner_gets_redacted_request_without_leaks(tmp_path):
    bridge = _bridge(tmp_path)
    bridge._task_owner = "alice"
    bridge._task_owner_is_admin = False
    interaction_id, _entry = bridge._register_interaction(
        "decision", request={
            "title": "请选择 张三 13800000000",
            "message": "浙江农林大学 豪华餐 总餐次应为 9",
            "choices": [{"value": "a", "label": "张三 13800000000"}],
            "nested_secret": {"future": "SENSITIVE_FUTURE_FIELD"},
        })
    bridge._task_owner = ""
    try:
        bridge.set_request_is_admin(True)
        bridge.set_request_identity("root-admin")
        got = bridge.pending_interactions()
    finally:
        bridge.set_request_is_admin(None)
        bridge.set_request_identity(None)

    assert got["count"] == 1
    item = got["interactions"][0]
    assert item["interaction_id"] == interaction_id
    assert item["request"] == {}
    assert item.get("request_redacted") is True
    _assert_no_leak(got)


def test_pending_owner_gets_only_whitelisted_request_fields(tmp_path):
    bridge = _bridge(tmp_path)
    bridge._task_owner = "alice"
    bridge._task_owner_is_admin = False
    interaction_id, _entry = bridge._register_interaction(
        "address_input", request={
            "title": "地址待确认",
            "message": "请输入最终地址",
            "items": [{
                "raw_address": "张三 浙江农林大学 13800000000",
                "order_numbers": ["W1", "W2"],
                "campus": "东湖",
                "reason": "无法识别",
                "suggested_point": "东湖校区",
                "future_unknown": "SENSITIVE_FUTURE_FIELD",
                "password": "SECRET-PW",
            }],
            "future_top": "SENSITIVE_FUTURE_FIELD",
        })
    bridge._task_owner = ""
    try:
        bridge.set_request_is_admin(False)
        bridge.set_request_identity("alice")
        got = bridge.pending_interactions()
    finally:
        bridge.set_request_is_admin(None)
        bridge.set_request_identity(None)

    item = got["interactions"][0]
    assert item["interaction_id"] == interaction_id
    request = item["request"]
    assert set(request) == {"title", "message", "items"}
    assert set(request["items"][0]) == {
        "raw_address", "order_numbers", "campus", "reason",
        "suggested_point", "confidence",
    }
    assert "future_unknown" not in request["items"][0]
    assert "password" not in request["items"][0]
    assert "future_top" not in request
    assert "SECRET-PW" not in json.dumps(got, ensure_ascii=False)


def test_pending_interactions_admin_sees_all_but_filters_secrets(tmp_path):
    bridge = _bridge(tmp_path)
    bridge._task_owner = "alice"
    bridge._task_owner_is_admin = False
    bridge._task_operation_id = "op-owner-1"
    interaction_id, entry = bridge._register_interaction(
        "decision", request={
            "title": "需要选择", "message": "请选择一个地址",
            "choices": [{"value": "a", "label": "A"}],
            "password": "SECRET-PW", "token": "SECRET-TOKEN",
        })
    bridge._task_owner = ""
    bridge._task_operation_id = ""

    try:
        got = bridge.pending_interactions()
    finally:
        bridge.set_request_is_admin(None)
        bridge.set_request_identity(None)

    assert got["ok"] is True
    assert got["count"] == 1
    item = got["interactions"][0]
    assert item["interaction_id"] == interaction_id
    assert item["operation_id"] == "op-owner-1"
    assert item["status"] == "pending"
    assert item["kind"] == "decision"
    assert item["request"] == {}
    assert item.get("request_redacted") is True
    # 只读：没有消费、唤醒或修改 holder。
    assert not entry.event.is_set()
    assert entry.holder == []
    assert interaction_id in bridge._decisions


def test_pending_interactions_non_owner_cannot_see_or_resolve(tmp_path):
    bridge = _bridge(tmp_path)
    bridge._task_owner = "alice"
    bridge._task_owner_is_admin = False
    interaction_id, entry = bridge._register_interaction(
        "decision", request={"title": "t", "message": "m", "choices": []})
    bridge._task_owner = ""

    try:
        bridge.set_request_is_admin(False)
        bridge.set_request_identity("bob")
        hidden = bridge.pending_interactions()
        denied = bridge.resolve_decision(interaction_id, "retry")

        bridge.set_request_identity("alice")
        visible = bridge.pending_interactions()
    finally:
        bridge.set_request_is_admin(None)
        bridge.set_request_identity(None)

    assert hidden["interactions"] == []
    assert denied["ok"] is False
    assert denied["status"] == "forbidden"
    assert denied["reason"] == "interaction_not_owned_by_caller"
    assert [item["interaction_id"] for item in visible["interactions"]] == [
        interaction_id]
    assert not entry.event.is_set()


def test_pending_interactions_can_filter_by_operation_id(tmp_path):
    bridge = _bridge(tmp_path)
    bridge._task_owner = "alice"
    bridge._task_operation_id = "op-a"
    a_id, _a_entry = bridge._register_interaction(
        "decision", request={"title": "A", "message": "a", "choices": []})
    bridge._task_operation_id = "op-b"
    b_id, _b_entry = bridge._register_interaction(
        "decision", request={"title": "B", "message": "b", "choices": []})
    bridge._task_owner = ""
    bridge._task_operation_id = ""
    try:
        bridge.set_request_is_admin(True)
        only_a = bridge.pending_interactions("op-a")
        only_b = bridge.pending_interactions("op-b")
        unknown = bridge.pending_interactions("op-missing")
    finally:
        bridge.set_request_is_admin(None)

    assert [item["interaction_id"] for item in only_a["interactions"]] == [a_id]
    assert [item["interaction_id"] for item in only_b["interactions"]] == [b_id]
    assert unknown["interactions"] == []


def test_pending_interactions_query_is_repeatable_and_expired_items_are_hidden(
        tmp_path):
    bridge = _bridge(tmp_path)
    bridge._task_owner = "alice"
    interaction_id, entry = bridge._register_interaction(
        "decision", request={"title": "t", "message": "m", "choices": []})
    bridge._task_owner = ""
    try:
        bridge.set_request_is_admin(True)
        first = bridge.pending_interactions()
        second = bridge.pending_interactions()
        assert first["count"] == 1 and second["count"] == 1
        assert not entry.event.is_set()

        entry.expires_at = time.time() - 1
        assert bridge.pending_interactions()["interactions"] == []
        # 过期后 resolve 必须明确拒绝，不得激活。
        expired = bridge.resolve_decision(interaction_id, "retry")
        assert expired["ok"] is False
        assert expired["status"] == "expired"
    finally:
        bridge.set_request_is_admin(None)


def test_resolve_duplicate_and_stop_race_are_explicit(tmp_path):
    bridge = _bridge(tmp_path)
    bridge._task_owner = "alice"
    first_id, first_entry = bridge._register_interaction(
        "decision", request={"title": "t", "message": "m", "choices": []})
    second_id, _second_entry = bridge._register_interaction(
        "decision", request={"title": "t2", "message": "m2", "choices": []})
    bridge._task_owner = ""

    first = bridge.resolve_decision(first_id, "retry")
    duplicate = bridge.resolve_decision(first_id, "skip")
    assert first["ok"] is True and first["status"] == "accepted"
    assert duplicate["ok"] is False and duplicate["status"] == "not_pending"

    # stop 先取消 second_id，再 resolve 必须得到明确 not_pending。
    bridge._worker = types.SimpleNamespace(is_alive=lambda: True)
    stopped = bridge.stop_task()
    after_stop = bridge.resolve_decision(second_id, "retry")
    assert stopped["ok"] is True
    assert after_stop["ok"] is False
    assert after_stop["status"] == "not_pending"
    assert bridge.pending_interactions()["interactions"] == []
    assert first_entry.event.is_set()
