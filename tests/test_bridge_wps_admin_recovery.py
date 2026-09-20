"""W3：管理员专用恢复/退场入口（Bridge 层）。

R6 反例 4.3（PROBE C）证明旧状态会“永久阻断且无解除路径”：三种 resolve 决策全部
失败、二次 ``apply_plan`` 零写入且 ``blocked forever with no clearing path``。
本文件钉住新入口的四件事：

1. **权限**：普通用户不可用（HTTP 403 见 ``tests/test_web_roles.py``；直接调用也
   必须 ``forbidden``）；
2. **明确确认**：必须逐字确认 decision、写审计备注，退场还要确认已核对表结构；
3. **操作范围**：一次只处理一个 ``operation_id``，返回的只是脱敏范围（目标日期/
   目标引用/表数），绝不返回 sheet 原名、file_id、客户信息；
4. **审计 + 重复请求保护**：备注与决策写进 journal（不留原文外泄），重复退场返回
   ``already_retired`` 且**不再写盘**。

并且：退场只退出全局 pending，**同一 target_date + 云表仍保留防重复闸门**；
未知写入结果不会因此变成“可以自动重传”（端到端证明见
``tests/test_bridge_wps_e2e.py``）。

全部离线：journal/ledger 都在 ``tmp_path``，不网络、不碰真实账本/客户数据。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.api.bridge import Bridge
from app.wps.journal import SyncJournal, journal_path_for, new_operation_id
from app.wps.sync import SyncLedger

_SHEET = "东湖中餐"
_FILE_ID = "F-SYNTH-1"
_TARGET_DATE = "2026-09-20"


def _bridge(tmp_path, *, is_admin: bool) -> Bridge:
    return Bridge(config_path=str(tmp_path / "config.json"), is_admin=is_admin)


@pytest.fixture(autouse=True)
def _reset_request_context():
    """Bridge 的角色是请求局部 ContextVar；测试之间必须清掉，避免串权。

    （网页版每个请求都会 ``set_request_is_admin``；pytest 里同一线程复用上下文，
    所以这里显式重置。）
    """
    from app.api import bridge as bridge_module

    bridge_module._REQUEST_IS_ADMIN.set(None)
    bridge_module._REQUEST_IDENTITY.set(None)
    try:
        yield
    finally:
        bridge_module._REQUEST_IS_ADMIN.set(None)
        bridge_module._REQUEST_IDENTITY.set(None)


@pytest.fixture
def recovery_env(tmp_path, monkeypatch):
    """临时账本 + 一条“上传中途断电”的 pending 操作。"""
    from app.api import bridge as bridge_module

    ledger_path = tmp_path / "wps_sync_state.json"
    monkeypatch.setattr(bridge_module, "SyncLedger",
                        lambda *args, **kwargs: SyncLedger(ledger_path))
    ledger = SyncLedger(ledger_path)
    ledger.save()  # 先建立账本文件，让摘要/恢复状态都基于真实文件

    journal = SyncJournal(journal_path_for(ledger_path))
    operation_id = new_operation_id()
    journal.create_operation(operation_id, {
        f"0:{_SHEET}:{_FILE_ID}": {
            "sheet": _SHEET,
            "file_id": _FILE_ID,
            "target_date": _TARGET_DATE,
            "status": "writing",
            "next_action": "recover_journal",
            "reason": "写入过程中断电，无法判断是否已写",
            "problems": ["张三: 目标列应为 1，实际 ''"],
        },
    }, target_date=_TARGET_DATE)
    journal.save()
    return {
        "bridge": _bridge(tmp_path, is_admin=True),
        "admin": _bridge(tmp_path, is_admin=True),
        "user": _bridge(tmp_path, is_admin=False),
        "ledger_path": ledger_path,
        "journal_path": journal_path_for(ledger_path),
        "operation_id": operation_id,
    }


def _journal_bytes(path: Path) -> bytes:
    return path.read_bytes() if path.exists() else b""


def _retire(env, bridge=None, **overrides):
    payload = {
        "operation_id": env["operation_id"],
        "decision": "retire_guarded",
        "confirm": "retire_guarded",
        "note": "人工核对云端后确认无法判定，保留防重复闸门退出",
        "confirm_structure_checked": True,
    }
    payload.update(overrides)
    return (bridge or env["bridge"]).wps_recovery_resolve(payload)


# ----------------------------------------------------------------------
# 权限：普通用户无论走 HTTP 还是直接调用都不能用
# ----------------------------------------------------------------------
def test_w3_non_admin_direct_call_is_forbidden_and_touches_nothing(recovery_env):
    env = recovery_env
    before = _journal_bytes(env["journal_path"])

    got = _retire(env, env["user"])

    assert got["ok"] is False
    assert got["status"] == "forbidden"
    assert got["code"] == "forbidden"
    assert got["changed"] is False
    assert got["cloud_write"] is False
    assert _journal_bytes(env["journal_path"]) == before, "越权请求不得改 journal"
    # 仍然 pending（没有被“悄悄退场”）。
    assert SyncJournal(env["journal_path"]).pending_operations()


def test_w3_forbidden_check_happens_before_argument_validation(recovery_env):
    """越权请求不能靠报错内容探测 operation_id 是否存在。"""
    env = recovery_env
    got = env["user"].wps_recovery_resolve({"operation_id": "wps-0123456789abcdef",
                                            "decision": "retire_guarded",
                                            "confirm": "retire_guarded",
                                            "note": "probe"})
    assert got["status"] == "forbidden"
    assert got["scope"] == {}


# ----------------------------------------------------------------------
# 明确确认：逐字确认 + 审计备注 + 结构确认
# ----------------------------------------------------------------------
@pytest.mark.parametrize("override,expected", [
    ({"confirm": "yes"}, "confirmation_required"),
    ({"confirm": ""}, "confirmation_required"),
    ({"note": ""}, "note_required"),
    ({"note": "ok"}, "note_required"),
    ({"confirm_structure_checked": False}, "structure_confirmation_required"),
    ({"decision": "force_clear"}, "decision_not_allowed"),
    ({"decision": ""}, "decision_not_allowed"),
    ({"operation_id": "not-an-id"}, "invalid_operation_id"),
    ({"operation_id": "wps-XYZ"}, "invalid_operation_id"),
])
def test_w3_confirmation_and_validation_gates(recovery_env, override, expected):
    env = recovery_env
    before = _journal_bytes(env["journal_path"])

    got = _retire(env, **override)

    assert got["ok"] is False, got
    assert got["code"] == expected, got
    assert got["changed"] is False
    assert _journal_bytes(env["journal_path"]) == before, "被拒时不得写 journal"
    assert SyncJournal(env["journal_path"]).pending_operations()


def test_w3_unknown_operation_is_not_found_without_writes(recovery_env):
    env = recovery_env
    before = _journal_bytes(env["journal_path"])
    got = _retire(env, operation_id="wps-ffffffffffffffff")
    assert got["ok"] is False
    assert got["code"] == "not_found"
    assert got["cloud_write"] is False
    assert _journal_bytes(env["journal_path"]) == before


def test_w3_retire_alias_is_normalized(recovery_env):
    """B 的等价别名（retire/abandon/...）仍然要求逐字确认归一后的决策名。"""
    env = recovery_env
    got = _retire(env, decision="abandon",
                  confirm="retire_guarded")
    assert got["ok"] is True, got
    assert got["status"] == "retired_guarded"


# ----------------------------------------------------------------------
# 成功退场：审计落盘、范围脱敏、闸门保留
# ----------------------------------------------------------------------
def test_w3_retire_writes_audit_and_keeps_guard(recovery_env):
    env = recovery_env
    note = "人工核对云端后确认无法判定写入结果，保留闸门退出"

    got = _retire(env, note=note)

    assert got["ok"] is True, got
    assert got["status"] == "retired_guarded"
    assert got["code"] == "retired_guarded"
    assert got["changed"] is True
    assert got["verified_on_disk"] is True
    assert got["cloud_write"] is False
    assert got["next_action"] == "manual_reconcile"
    assert got["scope"]["guard_retained"] is True
    assert got["scope"]["target_dates"] == [_TARGET_DATE]
    assert got["scope"]["sheet_count"] == 1
    # 审计：谁、何时、什么决策、是否记录备注；并且没有任何 sheet 原名/file_id。
    audit = got["audit"]
    assert audit["decision"] == "retire_guarded"
    assert audit["note_recorded"] is True
    assert audit["duplicate"] is False
    assert audit["effects"]["auto_retry_allowed"] is False
    assert _SHEET not in json.dumps(got, ensure_ascii=False)
    assert _FILE_ID not in json.dumps(got, ensure_ascii=False)

    # journal 真的写了审计字段，且保留原问题描述。
    raw = json.loads(env["journal_path"].read_text(encoding="utf-8"))
    op = raw["operations"][env["operation_id"]]
    assert op["status"] == "retired_guarded"
    assert op["retired_guarded"] is True
    assert op["retire_note"] == note
    assert op["retired_at"]
    record = next(iter(op["sheets"].values()))
    assert record["status"] == "retired_guarded"
    assert record["retire_note"] == note
    assert record["prior_status"] == "writing"
    # 已退出全局 pending，但同目标闸门仍在。
    journal = SyncJournal(env["journal_path"])
    assert env["operation_id"] not in journal.pending_operations()
    assert journal.has_guard(_TARGET_DATE, _FILE_ID) is True


def test_w3_duplicate_retire_is_idempotent(recovery_env):
    env = recovery_env
    first = _retire(env)
    assert first["ok"] is True
    after_first = _journal_bytes(env["journal_path"])

    second = _retire(env)

    assert second["ok"] is True
    assert second["status"] == "already_retired"
    assert second["code"] == "already_retired"
    assert second["changed"] is False
    assert second["audit"]["duplicate"] is True
    assert second["scope"]["guard_retained"] is True
    assert _journal_bytes(env["journal_path"]) == after_first, "重复请求不得再写盘"


def test_w3_retired_operation_surfaces_in_recovery_status(recovery_env):
    env = recovery_env
    assert _retire(env)["ok"] is True

    admin_view = env["bridge"].wps_recovery_status()
    assert admin_view["ok"] is True
    assert admin_view["counts"]["retired_guarded"] == 1
    assert admin_view["counts"]["uncertain"] == 0
    assert admin_view["summary"]["retired_guarded_count"] == 1
    assert admin_view["summary"]["needs_review"] is True
    assert admin_view["next_action"] == "manual_reconcile"
    op = admin_view["operations"][0]
    assert op["status"] == "retired_guarded"
    assert op["pending"] is False
    assert op["error_code"] == "wps_recovery_retired_guarded"
    assert op["allowed_next_actions"] == ["manual_reconcile"]

    # 普通用户仍只看安全摘要，且不会因为退场而误判为“没有待处理”。
    env["user"].set_request_is_admin(False)
    user_view = env["user"].wps_recovery_status()
    assert "operations" not in user_view
    assert user_view["counts"]["retired_guarded"] == 1
    assert user_view["summary"]["needs_review"] is True
    assert user_view["summary"]["guidance"]


# ----------------------------------------------------------------------
# 不能靠“退场/归档”把未知写入结果变成可自动重传
# ----------------------------------------------------------------------
def test_w3_cloud_untouched_cannot_clear_unproven_write(recovery_env, monkeypatch):
    """云端读不到目标行时，cloud_untouched 必须失败并保留闸门（不得自动重传）。"""
    env = recovery_env

    class _UnreadableCloud:
        path = "/fake/kdocs-cli"

        def authenticated(self) -> bool:
            return True

        def read_grid(self, *args, **kwargs):
            return {}  # 读不到任何行：无法证明“完全未执行”

        def read_formulas(self, *args, **kwargs):
            return {}

        def sheets_info(self, file_id):
            return [{"sheetId": 1, "sheetName": "Sheet1", "rowTo": 50, "colTo": 20}]

    monkeypatch.setattr(env["bridge"], "_wps_cli", lambda: _UnreadableCloud())
    got = env["bridge"].wps_recovery_resolve({
        "operation_id": env["operation_id"],
        "decision": "cloud_untouched",
        "confirm": "cloud_untouched",
        "note": "试图证明完全未执行",
    })

    assert got["ok"] is False, got
    assert got["code"] in ("cloud_not_untouched", "cli_required"), got
    # 没有退场，也没有被“清障”：仍然是全局 pending，写入继续被阻断。
    assert got["scope"]["guard_retained"] is False
    assert got["scope"]["blocking"] == "pending"
    journal = SyncJournal(env["journal_path"])
    assert env["operation_id"] in journal.pending_operations()
    assert journal.has_guard(_TARGET_DATE, _FILE_ID) is False


def test_w3_cloud_decision_without_cli_is_clearly_reported(recovery_env):
    """没有 kdocs-cli 时，需要读云端的决策必须明确报 cli_unavailable，而不是假装成功。"""
    env = recovery_env
    got = env["bridge"].wps_recovery_resolve({
        "operation_id": env["operation_id"],
        "decision": "cloud_verified",
        "confirm": "cloud_verified",
        "note": "试图按云端结果补账本",
    })
    assert got["ok"] is False, got
    assert got["code"] == "cli_unavailable"
    assert got["changed"] is False
    assert env["operation_id"] in SyncJournal(env["journal_path"]).pending_operations()


def test_w3_keep_records_note_and_stays_blocked(recovery_env):
    env = recovery_env
    got = env["bridge"].wps_recovery_resolve({
        "operation_id": env["operation_id"],
        "decision": "keep",
        "confirm": "keep",
        "note": "先只读核对，暂不解除阻断",
    })

    assert got["ok"] is True, got
    assert got["status"] == "keep_recorded"
    assert got["next_action"] == "manual_reconcile"
    assert got["changed"] is True
    # 仍然 pending：keep 只留痕，不解除阻断。
    assert env["operation_id"] in SyncJournal(env["journal_path"]).pending_operations()
    assert got["scope"]["guard_retained"] is False


def test_w3_retire_works_while_another_operation_is_active(recovery_env):
    """恢复入口自己也要占互斥槽位：并发时第二个请求必须被明确拒绝。"""
    env = recovery_env
    reservation = env["bridge"]._operations.try_reserve(
        "wps_upload", summary={"title": "云文档上传"})
    assert reservation.granted
    try:
        got = _retire(env)
    finally:
        env["bridge"]._operations.finish(reservation.operation, status="success")

    assert got["ok"] is False
    assert got["code"] == "operation_conflict"
    assert got["changed"] is False
    assert env["operation_id"] in SyncJournal(env["journal_path"]).pending_operations()


def test_w3_is_admin_is_request_scoped(recovery_env):
    """共享 Bridge 上用请求局部角色：普通用户请求不能被管理员上下文串权。"""
    env = recovery_env
    env["bridge"].set_request_is_admin(False)
    try:
        got = _retire(env)
    finally:
        env["bridge"].set_request_is_admin(None)
    assert got["status"] == "forbidden"
    # 清掉请求上下文后回到实例默认（admin=True），恢复可用。
    assert _retire(env)["ok"] is True


def test_w3_keep_reports_journal_write_failure_as_error(recovery_env, monkeypatch):
    """journal 落盘失败时 keep 不能报成功（审计没写进去就不算记录）。"""
    from app.wps.journal import SyncJournal as _SyncJournal

    env = recovery_env

    def boom(self):
        raise OSError("磁盘只读")

    monkeypatch.setattr(_SyncJournal, "save", boom, raising=True)
    got = env["bridge"].wps_recovery_resolve({
        "operation_id": env["operation_id"],
        "decision": "keep",
        "confirm": "keep",
        "note": "先只读核对，暂不解除阻断",
    })

    assert got["ok"] is False, got
    assert got["code"] in ("journal_write_failed", "journal_unreadable"), got
    assert got["changed"] is False
    assert got["next_action"] == "fix_journal"
