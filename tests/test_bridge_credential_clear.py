"""clear_password 三态回归：使用假凭据存储，不读取真实 keyring。

锁死：删除函数返回 False / 抛异常 / 无法确认时不能返回 ok=true，
不泄漏密码内容，不触发“密码已清除”成功日志；重复清除幂等。
"""
from __future__ import annotations

import json

import pytest

from app.api import bridge as bridge_module
from app.api.bridge import Bridge


def _bridge(tmp_path) -> Bridge:
    return Bridge(config_path=str(tmp_path / "config.json"), is_admin=True)


def _set_account(bridge: Bridge, kind: str, account: str) -> None:
    if kind == "sss":
        bridge._config.sss_account = account
    else:
        bridge._config.phone_number = account


@pytest.mark.parametrize("kind", ["order", "sss"])
def test_success_state_and_no_secret_leak(tmp_path, monkeypatch, kind):
    bridge = _bridge(tmp_path)
    _set_account(bridge, kind, "acct-1")
    calls: list[str] = []
    delete_name = "delete_sss_password" if kind == "sss" else "delete_password"
    getter_name = "get_sss_password" if kind == "sss" else "get_password"
    monkeypatch.setattr(bridge_module, delete_name,
                        lambda account: calls.append(account) or True)
    monkeypatch.setattr(bridge_module, getter_name, lambda account: None)

    got = bridge.clear_password(kind)

    assert calls == ["acct-1"]
    assert got["ok"] is True
    assert got["status"] == "success"
    assert got["state"] == "deleted"
    assert got["deleted"] is True and got["mode"] == kind
    logs = [event["payload"]["msg"] for event in bridge.drain_events(0)["events"]
            if event["event"] == "log"]
    assert any(msg.startswith("已清除本机保存的") for msg in logs)


@pytest.mark.parametrize("kind", ["order", "sss"])
def test_false_with_remaining_password_is_failure_and_hides_secret(
        tmp_path, monkeypatch, kind):
    bridge = _bridge(tmp_path)
    _set_account(bridge, kind, "acct-2")
    secret = "super-secret-password"
    delete_name = "delete_sss_password" if kind == "sss" else "delete_password"
    getter_name = "get_sss_password" if kind == "sss" else "get_password"
    monkeypatch.setattr(bridge_module, delete_name, lambda account: False)
    monkeypatch.setattr(bridge_module, getter_name, lambda account: secret)

    got = bridge.clear_password(kind)

    assert got["ok"] is False
    assert got["status"] == "error"
    assert got["state"] == "delete_failed"
    assert got["deleted"] is False
    assert got["reason"] and got["next_action"]
    assert secret not in json.dumps(got, ensure_ascii=False)
    logs = [event["payload"]["msg"] for event in bridge.drain_events(0)["events"]
            if event["event"] == "log"]
    assert secret not in "\n".join(logs)
    assert not any(msg.startswith("已清除本机保存的") for msg in logs)


@pytest.mark.parametrize("kind", ["order", "sss"])
def test_false_without_readable_value_is_unconfirmed_not_success(
        tmp_path, monkeypatch, kind):
    bridge = _bridge(tmp_path)
    _set_account(bridge, kind, "acct-3")
    delete_name = "delete_sss_password" if kind == "sss" else "delete_password"
    getter_name = "get_sss_password" if kind == "sss" else "get_password"
    monkeypatch.setattr(bridge_module, delete_name, lambda account: False)
    monkeypatch.setattr(bridge_module, getter_name, lambda account: None)

    got = bridge.clear_password(kind)

    assert got["ok"] is False
    assert got["status"] == "error"
    assert got["state"] in ("already_absent_or_unavailable", "delete_unconfirmed")
    assert got["reason"] and got["next_action"]


@pytest.mark.parametrize("kind", ["order", "sss"])
def test_delete_exception_is_failure_and_hides_exception_text(
        tmp_path, monkeypatch, kind):
    bridge = _bridge(tmp_path)
    _set_account(bridge, kind, "acct-4")
    delete_name = "delete_sss_password" if kind == "sss" else "delete_password"
    getter_name = "get_sss_password" if kind == "sss" else "get_password"
    monkeypatch.setattr(bridge_module, delete_name,
                        lambda account: (_ for _ in ()).throw(
                            RuntimeError("secret-backend-detail")))
    monkeypatch.setattr(bridge_module, getter_name, lambda account: None)

    got = bridge.clear_password(kind)

    assert got["ok"] is False
    assert got["state"] == "delete_error"
    assert "secret-backend-detail" not in json.dumps(got, ensure_ascii=False)
    logs = [event["payload"]["msg"] for event in bridge.drain_events(0)["events"]
            if event["event"] == "log"]
    assert "secret-backend-detail" not in "\n".join(logs)


@pytest.mark.parametrize("kind", ["order", "sss"])
def test_getter_exception_after_false_is_unconfirmed(tmp_path, monkeypatch, kind):
    bridge = _bridge(tmp_path)
    _set_account(bridge, kind, "acct-5")
    delete_name = "delete_sss_password" if kind == "sss" else "delete_password"
    getter_name = "get_sss_password" if kind == "sss" else "get_password"
    monkeypatch.setattr(bridge_module, delete_name, lambda account: False)
    monkeypatch.setattr(bridge_module, getter_name,
                        lambda account: (_ for _ in ()).throw(RuntimeError("x")))

    got = bridge.clear_password(kind)

    assert got["ok"] is False
    assert got["state"] == "delete_unconfirmed"
    assert got["reason"] and got["next_action"]


@pytest.mark.parametrize("kind", ["order", "sss"])
def test_repeat_after_success_is_idempotent(tmp_path, monkeypatch, kind):
    bridge = _bridge(tmp_path)
    _set_account(bridge, kind, "acct-6")
    delete_name = "delete_sss_password" if kind == "sss" else "delete_password"
    getter_name = "get_sss_password" if kind == "sss" else "get_password"
    responses = iter([True, False])
    calls: list[str] = []
    monkeypatch.setattr(bridge_module, delete_name,
                        lambda account: calls.append(account) or next(responses))
    monkeypatch.setattr(bridge_module, getter_name, lambda account: None)

    first = bridge.clear_password(kind)
    second = bridge.clear_password(kind)

    assert first["ok"] is True and first["state"] == "deleted"
    assert second["ok"] is True
    assert second["state"] == "already_cleared"
    assert calls == ["acct-6", "acct-6"]


@pytest.mark.parametrize("kind", ["order", "sss"])
def test_empty_account_never_calls_backend(tmp_path, monkeypatch, kind):
    bridge = _bridge(tmp_path)
    _set_account(bridge, kind, "   ")
    calls: list[str] = []
    delete_name = "delete_sss_password" if kind == "sss" else "delete_password"
    getter_name = "get_sss_password" if kind == "sss" else "get_password"
    monkeypatch.setattr(bridge_module, delete_name,
                        lambda account: calls.append(account) or True)
    monkeypatch.setattr(bridge_module, getter_name, lambda account: None)

    got = bridge.clear_password(kind)

    assert calls == []
    assert got["ok"] is True
    assert got["state"] == "account_empty"
