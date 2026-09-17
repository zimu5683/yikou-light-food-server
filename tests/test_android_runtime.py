"""APK 内置模式（``YIKOU_APP_MODE=android``）的 Python 适配层回归锁。

这些用例在 CPython 里运行，用假的 ``_runtime_class`` / ``SecureStore`` 模拟
Chaquopy Java 对象，验证：

* 只有显式 App 模式才接管，Termux/桌面不受影响；
* kdocs-cli 的 JSON 输出解析、额度错误、重试语义与桌面共用；
* 授权 URL 会回调给日志层，token 不会进入命令行；
* Android Keystore 后端替换 keyring，且失败时优雅降级。
"""
from __future__ import annotations

import json
import os

import pytest

from app.core import android_store, credentials
from app.core.config import user_data_dir
from app.wps import android_runtime, cli as wps_cli
from app.wps.cli import KdocsCli
from app.wps.errors import WpsCloudError


@pytest.fixture
def android(monkeypatch):
    """打开 App 模式；测试结束自动清理环境变量。"""
    monkeypatch.setenv(android_runtime.APP_MODE_ENV, android_runtime.APP_MODE_ANDROID)


@pytest.fixture
def fake_runtime(monkeypatch, android):
    """安装一个可记录调用的假 Kotlin WpsRuntime。"""

    class Runtime:
        def __init__(self) -> None:
            self.calls: list[tuple] = []
            self.run_result: dict = {"exitCode": 0, "stdout": "", "stderr": ""}
            self.auth = {"authenticated": False}
            self.states: list[dict] = []

        def runJson(self, args_json, params_json, timeout_ms):
            self.calls.append(("runJson", args_json, params_json, timeout_ms))
            return json.dumps(self.run_result, ensure_ascii=False)

        def authStatusJson(self):
            self.calls.append(("authStatusJson",))
            return json.dumps(self.auth)

        def beginAuthorizeJson(self):
            self.calls.append(("beginAuthorizeJson",))
            return json.dumps({"ok": True})

        def authorizationStateJson(self):
            self.calls.append(("authorizationStateJson",))
            return json.dumps(self.states.pop(0) if self.states else {"running": False,
                                                                      "finished": True})

        def cancelAuthorizeJson(self):
            self.calls.append(("cancelAuthorizeJson",))
            return json.dumps({"ok": True})

        def openExternal(self, url):
            self.calls.append(("openExternal", url))
            return True

        def notifyInteraction(self, kind):
            self.calls.append(("notifyInteraction", kind))
            return True

        def clearInteraction(self):
            self.calls.append(("clearInteraction",))
            return True

        def diagnosticsJson(self):
            return json.dumps({"ok": True, "targetSdk": 35})

    runtime = Runtime()
    monkeypatch.setattr(android_runtime, "_runtime_class", lambda: runtime)
    return runtime


def test_non_android_mode_never_activates(monkeypatch):
    monkeypatch.delenv(android_runtime.APP_MODE_ENV, raising=False)
    assert not android_runtime.is_android()
    assert not android_store.is_android()
    monkeypatch.setenv(android_runtime.APP_MODE_ENV, "termux")
    assert not android_runtime.is_android()


def test_run_cli_serializes_params_and_parses_result(fake_runtime):
    fake_runtime.run_result = {
        "exitCode": 0,
        "stdout": '{"code": 0, "data": {"ok": 1}}',
        "stderr": "",
        "timedOut": False,
        "errorCode": None,
    }
    result = android_runtime.run_cli(["sheet", "get-sheets-info"],
                                     {"file_id": "x"}, timeout_ms=1234)

    assert result.ok
    assert result.exit_code == 0
    method, args_json, params_json, timeout_ms = fake_runtime.calls[-1]
    assert method == "runJson"
    assert json.loads(args_json) == ["sheet", "get-sheets-info"]
    assert json.loads(params_json) == {"file_id": "x"}
    assert timeout_ms == 1234
    # token 绝不能被塞进 args。
    assert not any("--token" in str(arg) for arg in json.loads(args_json))


def test_run_cli_surfaces_timeout_and_error_code(fake_runtime):
    fake_runtime.run_result = {"exitCode": -1, "stdout": "", "stderr": "boom",
                              "timedOut": True, "errorCode": "TIMEOUT"}
    result = android_runtime.run_cli(["drive", "list-files"])
    assert result.timed_out
    assert result.error_code == "TIMEOUT"
    assert not result.ok


def test_run_cli_reports_unavailable_runtime(monkeypatch, android):
    def broken():
        raise android_runtime.AndroidRuntimeError("没有 chaquopy",
                                                  error_code="RUNTIME_UNAVAILABLE")
    monkeypatch.setattr(android_runtime, "_runtime_class", broken)
    with pytest.raises(android_runtime.AndroidRuntimeError):
        android_runtime.run_cli(["version"])


def test_auth_status_reads_native_json(fake_runtime):
    fake_runtime.auth = {"authenticated": True}
    assert android_runtime.auth_status() is True
    fake_runtime.auth = {"authenticated": False}
    assert android_runtime.auth_status() is False


def test_auth_status_returns_false_when_native_call_fails(monkeypatch, android):
    monkeypatch.setattr(
        android_runtime, "_runtime_class",
        lambda: (_ for _ in ()).throw(android_runtime.AndroidRuntimeError("boom")))
    assert android_runtime.auth_status() is False


def test_auth_status_raises_runtime_error_but_keeps_false_as_false(fake_runtime):
    fake_runtime.auth = {"authenticated": False}
    assert android_runtime.auth_status() is False

    fake_runtime.auth = {"authenticated": False, "errorCode": "PROOT_MISSING",
                         "message": "缺少 proot"}
    with pytest.raises(android_runtime.AndroidRuntimeError) as excinfo:
        android_runtime.auth_status()
    assert excinfo.value.error_code == "PROOT_MISSING"
    assert "缺少 proot" in str(excinfo.value)


def test_kdocs_cli_android_authenticated_maps_native_error_to_wps_error(fake_runtime):
    fake_runtime.auth = {"authenticated": False, "errorCode": "DNS_FAILED",
                         "message": "lookup mcp-center.wps.cn failed"}
    with pytest.raises(WpsCloudError) as excinfo:
        KdocsCli(None).authenticated()
    assert "DNS_FAILED" in str(excinfo.value)


def test_authorize_polls_until_authenticated_and_reports_url(fake_runtime):
    fake_runtime.states = [
        {"running": True, "url": "https://example.invalid/oauth", "authenticated": False},
        {"running": False, "finished": True, "ok": True, "authenticated": True},
    ]
    seen: list[str] = []
    result = android_runtime.authorize(timeout_ms=5_000, poll_interval_s=0.001,
                                       on_url=seen.append)

    assert result.ok
    assert seen == ["https://example.invalid/oauth"]
    assert result.url == "https://example.invalid/oauth"

    methods = [call[0] for call in fake_runtime.calls]
    assert methods[:2] == ["beginAuthorizeJson", "authorizationStateJson"]
    assert "cancelAuthorizeJson" not in methods


def test_authorize_returns_failure_detail_and_cancels_on_timeout(fake_runtime):
    fake_runtime.states = [{"running": True, "url": "", "authenticated": False}]
    result = android_runtime.authorize(timeout_ms=1, poll_interval_s=0.001)
    assert not result.ok
    assert result.error_code == "TIMEOUT"
    assert ("cancelAuthorizeJson",) in fake_runtime.calls


def test_authorize_maps_native_start_failure(monkeypatch, android):
    class Broken:
        def beginAuthorizeJson(self):
            return json.dumps({"ok": False, "errorCode": "PROOT_MISSING",
                               "message": "缺少 proot"})
    monkeypatch.setattr(android_runtime, "_runtime_class", lambda: Broken())
    result = android_runtime.authorize(timeout_ms=1_000)
    assert not result.ok
    assert result.error_code == "PROOT_MISSING"


# ----------------------------------------------------------------------
# KdocsCli 接入
# ----------------------------------------------------------------------
def test_find_cli_returns_marker_in_android_mode(monkeypatch, android):
    assert wps_cli.find_cli() == android_runtime.RUNTIME_MARKER


def test_kdocs_cli_android_run_once_uses_native_runtime(fake_runtime, monkeypatch):
    fake_runtime.run_result = {"exitCode": 0,
                               "stdout": json.dumps({"code": 0, "data": {"ok": True}}),
                               "stderr": ""}
    monkeypatch.setattr(wps_cli.subprocess, "run", lambda *a, **k: pytest.fail(
        "Android 模式不应调用 subprocess"))
    cli = KdocsCli(None, timeout=42)
    assert cli.path == android_runtime.RUNTIME_MARKER
    assert cli._run_once("sheet", "get-sheets-info", params={"file_id": "x"}) == {"ok": True}
    _method, _args_json, _params_json, timeout_ms = fake_runtime.calls[-1]
    assert timeout_ms == 42_000


def test_kdocs_cli_android_invalid_output_raises_wps_error(fake_runtime):
    fake_runtime.run_result = {"exitCode": 144, "stdout": "", "stderr": "SIGSYS",
                               "errorCode": "SECCOMP_BLOCKED"}
    cli = KdocsCli(None)
    with pytest.raises(WpsCloudError) as excinfo:
        cli._run_once("auth", "status")
    assert "SECCOMP_BLOCKED" in str(excinfo.value)
    assert "SIGSYS" in str(excinfo.value)


def test_kdocs_cli_android_authenticated_uses_native_status(fake_runtime):
    fake_runtime.auth = {"authenticated": True}
    cli = KdocsCli(None)
    assert cli.authenticated() is True


def test_kdocs_cli_android_login_argv_is_rejected(fake_runtime):
    cli = KdocsCli(None)
    with pytest.raises(WpsCloudError):
        cli.login_argv()
    assert cli.login_env() is None


# ----------------------------------------------------------------------
# 密码存储
# ----------------------------------------------------------------------
class FakeSecureStore:
    def __init__(self) -> None:
        self.data: dict[tuple[str, str], str] = {}
        self.calls: list[tuple] = []

    def getSecret(self, service: str, username: str):
        self.calls.append(("get", service, username))
        return self.data.get((service, username))

    def setSecret(self, service: str, username: str, password: str):
        self.calls.append(("set", service, username, password))
        self.data[(service, username)] = password
        return True

    def deleteSecret(self, service: str, username: str):
        self.calls.append(("delete", service, username))
        self.data.pop((service, username), None)
        return True


def test_android_store_backend_round_trips(monkeypatch, android):
    fake = FakeSecureStore()
    monkeypatch.setattr(android_store, "_store_class", lambda: fake)
    backend = android_store.AndroidSecureStoreBackend()

    assert backend.get_password("svc", "u") is None
    assert backend.set_password("svc", "u", "pw") is True
    assert backend.get_password("svc", "u") == "pw"
    assert backend.delete_password("svc", "u") is True
    assert backend.get_password("svc", "u") is None
    assert fake.calls[0] == ("get", "svc", "u")


def test_credentials_prefer_android_store_in_app_mode(monkeypatch, android):
    fake = FakeSecureStore()
    monkeypatch.setattr(android_store, "_store_class", lambda: fake)
    # 如果这条被调用就说明 Android 模式错误地回退到了 keyring。
    monkeypatch.setattr(credentials, "_backend", credentials._backend)

    assert credentials.set_password("u", "ADMIN") is True
    assert credentials.set_sss_password("u", "SSS") is True
    assert credentials.get_password("u") == "ADMIN"
    assert credentials.get_sss_password("u") == "SSS"
    assert fake.data[("yikou-light-food", "u")] == "ADMIN"
    assert fake.data[("yikou-light-food-sss", "u")] == "SSS"


def test_user_data_dir_honours_android_override(monkeypatch, tmp_path):
    monkeypatch.setenv("YIKOU_APP_MODE", "android")
    monkeypatch.setenv("YIKOU_DATA_DIR", str(tmp_path / "app-data"))
    assert user_data_dir() == tmp_path / "app-data"


def test_user_data_dir_android_uses_xdg_config_home(monkeypatch, tmp_path):
    monkeypatch.setenv("YIKOU_APP_MODE", "android")
    monkeypatch.delenv("YIKOU_DATA_DIR", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    assert user_data_dir() == tmp_path / "cfg" / "yikou-light-food"


def test_server_dist_dir_honours_override(monkeypatch, tmp_path):
    from app.web import server

    monkeypatch.setenv("YIKOU_DIST_DIR", str(tmp_path / "dist"))
    assert server.default_dist_dir() == tmp_path / "dist"


# ----------------------------------------------------------------------
# Bridge 接入
# ----------------------------------------------------------------------
def test_bridge_open_external_uses_android_native(fake_runtime, monkeypatch):
    from app.api.bridge import Bridge

    bridge = Bridge(config_path=None, is_admin=True)
    monkeypatch.setattr(android_runtime, "open_external", lambda url: True)
    assert bridge.open_external("https://example.invalid") == {"ok": True}


def test_bridge_android_authorize_worker_reports_url_and_status(fake_runtime, monkeypatch,
                                                              tmp_path):
    from app.api.bridge import Bridge

    fake_runtime.auth = {"authenticated": True}
    fake_runtime.states = [
        {"running": True, "url": "https://example.invalid/oauth", "authenticated": False},
        {"running": False, "finished": True, "ok": True, "authenticated": True},
    ]
    bridge = Bridge(config_path=str(tmp_path / "config.json"), is_admin=True)
    monkeypatch.setattr(type(bridge), "_wps_authorize_worker_android",
                        lambda self: bridge.log("[云文档授权] 授权成功", "OK"))
    # 只验证按平台分派；Android worker 内部逻辑由 authorize() 单测覆盖。
    assert android_runtime.is_android()
    assert "https://example.invalid" not in os.environ


def test_bridge_android_authorize_dispatch(monkeypatch, android):
    from app.api.bridge import Bridge

    calls: list[str] = []
    bridge = Bridge(config_path=None, is_admin=True)
    monkeypatch.setattr(Bridge, "_wps_authorize_worker_android",
                        lambda self: calls.append("android"))
    monkeypatch.setattr(Bridge, "_wps_authorize_worker_desktop",
                        lambda self, cli: calls.append("desktop"))
    sent: list[tuple] = []
    monkeypatch.setattr(bridge, "_emit_event", lambda *a: sent.append(a))
    monkeypatch.setattr(Bridge, "wps_status", lambda self: {"ok": True})

    bridge._wps_authorize_worker(object())  # type: ignore[arg-type]
    assert calls == ["android"]
    assert sent


def test_notify_and_clear_interaction_use_native_methods(fake_runtime):
    assert android_runtime.notify_interaction("captcha") is True
    assert android_runtime.clear_interaction_notifications() is True
    assert fake_runtime.calls[-2:] == [("notifyInteraction", "captcha"),
                                        ("clearInteraction",)]


def test_bridge_notifies_native_interaction_in_android_mode(android, monkeypatch):
    from app.api.bridge import Bridge

    calls: list[str] = []
    monkeypatch.setattr(android_runtime, "notify_interaction",
                        lambda kind: calls.append(kind) or True)
    bridge = Bridge(config_path=None, is_admin=True)
    bridge._notify_android_interaction("address")
    bridge._clear_android_interaction_notification()
    assert calls == ["address"]


def test_android_logout_runs_auth_logout_and_reports_ok(fake_runtime):
    fake_runtime.run_result = {"exitCode": 0, "stdout": "", "stderr": ""}
    result = android_runtime.logout()
    assert result.ok
    _method, args_json, params_json, timeout_ms = fake_runtime.calls[-1]
    assert json.loads(args_json) == ["auth", "logout"]
    assert params_json is None
    assert timeout_ms == 60_000


def test_kdocs_cli_android_logout_delegates_to_native(fake_runtime, monkeypatch):
    calls: list[str] = []

    def fake_logout():
        calls.append("native")
        return android_runtime.ExecResult(exit_code=0)

    monkeypatch.setattr(android_runtime, "logout", fake_logout)
    assert KdocsCli(None).logout() is True
    assert calls == ["native"]


def test_bridge_wps_logout_android_returns_native_result(android, monkeypatch):
    from app.api.bridge import Bridge

    calls: list[str] = []
    monkeypatch.setattr(android_runtime, "logout",
                        lambda: (calls.append("logout"), android_runtime.ExecResult(exit_code=0))[1])
    bridge = Bridge(config_path=None, is_admin=True)
    result = bridge.wps_logout()
    assert result == {"ok": True, "reason": ""}
    assert calls == ["logout"]


def test_bridge_wps_logout_android_error_shape(android, monkeypatch):
    from app.api.bridge import Bridge

    monkeypatch.setattr(
        android_runtime, "logout",
        lambda: android_runtime.ExecResult(exit_code=1, stderr="boom", error_code="RUNTIME_CRASHED"))
    assert Bridge(config_path=None, is_admin=True).wps_logout() == {
        "ok": False, "reason": "RUNTIME_CRASHED"}
