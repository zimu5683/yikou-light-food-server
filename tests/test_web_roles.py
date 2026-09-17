"""角色权限（管理员 / 普通用户）的端到端测试。

背景：界面隐藏字段**挡不住任何东西** —— 接口是公开可调的。实测确认过一个真实
越权：普通用户直接调 ``save_order_config({"phone": ..., "url": ...})`` 成功改掉了
下单手机号与管理网址，即可把下单目标指向自己的管理后台，从而拿到平台账号密码。

因此下列行为必须在**后端**强制，本文件就是它的回归锁：
1. 普通用户看不到目标/凭据（配置脱敏、密码不回传）；
2. 普通用户改不了配置（方法白名单，未登记的一律 403）；
3. 普通用户翻不了服务器文件系统；
4. 普通用户即使伪造 url/phone 下单，服务端也强制用预设值；
5. 普通用户仍能完成授权范围内的操作：开始/停止、看日志、回应交互、WPS 预览与上传。
"""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.parse
import urllib.request

import pytest

from app.web.auth import AuthStore
from app.web.server import SESSION_COOKIE, create_server

ADMIN = "admin@example.com"
ADMIN_PW = "adminpw123"
USER = "worker@example.com"
USER_PW = "workerpw123"


@pytest.fixture()
def auth(tmp_path):
    store = AuthStore(tmp_path / "cfg")
    store.create_admin(ADMIN, ADMIN_PW)
    store.register(USER, USER_PW)
    store.approve(USER, by=ADMIN)
    return store


@pytest.fixture()
def server(tmp_path, auth):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html><title>app</title>", encoding="utf-8")
    httpd = create_server("127.0.0.1", 0, dist_dir=dist, token="legacy",
                          config_path=tmp_path / "config.json", auth=auth)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield httpd
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def _call(server, method, path, *, body=None, form=None, cookie=None):
    port = server.server_address[1]
    data, headers = None, {}
    if form is not None:
        data = urllib.parse.urlencode(form).encode("utf-8")
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    elif body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if cookie:
        headers["Cookie"] = f"{SESSION_COOKIE}={cookie}"
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}",
                                     data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return response.status, response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


def _login(server, username, password):
    status, _ = _call(server, "POST", "/login",
                      form={"action": "login", "username": username, "password": password})
    assert status == 302, "登录应成功"
    reply = _call(server, "POST", "/login",
                  form={"action": "login", "username": username, "password": password})
    return reply


def _token(server, username, password):
    """登录并取出会话令牌（走登录重定向的 Set-Cookie）。"""
    port = server.server_address[1]
    data = urllib.parse.urlencode(
        {"action": "login", "username": username, "password": password}).encode("utf-8")
    request = urllib.request.Request(f"http://127.0.0.1:{port}/login", data=data, method="POST")
    request.add_header("Content-Type", "application/x-www-form-urlencoded")

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None

    try:
        with urllib.request.build_opener(_NoRedirect).open(request, timeout=15) as response:
            raw = response.headers.get("Set-Cookie", "")
    except urllib.error.HTTPError as exc:
        raw = exc.headers.get("Set-Cookie", "")
    for part in raw.split(";"):
        name, sep, value = part.strip().partition("=")
        if sep and name == SESSION_COOKIE:
            return value
    raise AssertionError(f"未取到会话令牌：{raw!r}")


def _api(server, cookie, method, payload=None):
    """调用桥接方法并返回 (状态码, 解析后的 body)。"""
    body = [] if payload is None else [payload]
    status, text = _call(server, "POST", f"/api/{method}", body=body, cookie=cookie)
    try:
        return status, json.loads(text)
    except json.JSONDecodeError:
        return status, text


# ----------------------------------------------------------------------
# 1. 配置脱敏：普通用户看不到目标与凭据
# ----------------------------------------------------------------------
def test_admin_sees_full_config(server):
    cookie = _token(server, ADMIN, ADMIN_PW)
    status, body = _api(server, cookie, "bridge_ready")
    assert status == 200
    assert body["is_admin"] is True
    assert "target_url" in body["config"]
    assert "phone_number" in body["config"]


def test_non_admin_config_has_sensitive_fields_blanked(server):
    cookie = _token(server, USER, USER_PW)
    status, body = _api(server, cookie, "bridge_ready")
    assert status == 200
    assert body["is_admin"] is False
    config = body["config"]
    # 目标与凭据必须为空 —— 不是「前端不显示」，而是根本没发出去
    for key in ("target_url", "phone_number", "excel_path", "sss_url",
                "sss_account", "sss_excel_path", "wps_cli_path", "wps_drive_id"):
        assert config[key] == "", f"{key} 不应发给普通用户，实际 {config[key]!r}"
    assert config["wps_tables"] == {}
    assert config["wps_test_tables"] == {}


def test_non_admin_still_gets_task_parameters(server):
    """下单日期/数量属于「怎么跑」，普通用户需要看到（且不构成泄漏）。"""
    cookie = _token(server, USER, USER_PW)
    _, body = _api(server, cookie, "bridge_ready")
    assert "order_date" in body["config"]
    assert "order_count" in body["config"]


def test_non_admin_password_never_returned(server, auth):
    """即使密钥环里存了平台密码，也不能回传给普通用户。"""
    from app.api import bridge as bridge_module

    server.bridge._config.phone_number = "13900000000"
    monkey = "secret-platform-password"
    original = bridge_module.get_password
    bridge_module.get_password = lambda *_a, **_k: monkey
    try:
        admin_cookie = _token(server, ADMIN, ADMIN_PW)
        _, admin_body = _api(server, admin_cookie, "bridge_ready")
        assert admin_body["passwords"]["order"] == monkey, "管理员应能拿到（用于预填表单）"

        user_cookie = _token(server, USER, USER_PW)
        _, user_body = _api(server, user_cookie, "bridge_ready")
        assert user_body["passwords"]["order"] == "", "普通用户绝不能拿到平台密码"
    finally:
        bridge_module.get_password = original


# ----------------------------------------------------------------------
# 2. 方法白名单：普通用户改不了配置
# ----------------------------------------------------------------------
@pytest.mark.parametrize("method", [
    "save_order_config",      # 改下单目标（实测过的越权点）
    "save_sss_config",        # 改闪时送目标
    "save_wps_config",        # 改云文档目标
    "restore_wps_production_tables",
    "clear_password",         # 清掉已保存的凭据
    "choose_excel",           # 服务器端选文件
    "new_template",
    "check_updates",
    "wps_authorize",          # 重新授权云文档
    "open_external",
])
def test_non_admin_blocked_from_admin_methods(server, method):
    cookie = _token(server, USER, USER_PW)
    status, body = _api(server, cookie, method, {})
    assert status == 403, f"{method} 应被拦下，实际 {status}"
    assert body["code"] == "admin_only"


def test_admin_is_allowed_the_same_method(server):
    """同一方法管理员可调 —— 确认拦截是按角色而非误伤。"""
    cookie = _token(server, ADMIN, ADMIN_PW)
    status, _ = _api(server, cookie, "save_order_config", {})
    assert status == 200


def test_non_admin_cannot_change_target_via_short_field_names(server):
    """复现并锁死实测到的越权：用短字段名 url/phone 改下单目标。"""
    before = server.bridge._config.phone_number
    cookie = _token(server, USER, USER_PW)
    status, _ = _api(server, cookie, "save_order_config",
                     {"phone": "19900001111", "url": "https://attacker.example.com"})
    assert status == 403
    assert server.bridge._config.phone_number == before, "配置不得被普通用户改动"


# ----------------------------------------------------------------------
# 3. 文件浏览器
# ----------------------------------------------------------------------
def test_non_admin_cannot_browse_server_files(server, tmp_path):
    cookie = _token(server, USER, USER_PW)
    status, body = _call(server, "GET", f"/api/fs/list?path={tmp_path}", cookie=cookie)
    assert status == 403
    assert "admin_only" in body


def test_admin_can_browse_server_files(server, tmp_path):
    cookie = _token(server, ADMIN, ADMIN_PW)
    status, _ = _call(server, "GET", f"/api/fs/list?path={tmp_path}", cookie=cookie)
    assert status == 200


# ----------------------------------------------------------------------
# 4. 下单目标强制取预设
# ----------------------------------------------------------------------
def test_non_admin_cannot_redirect_order_target(server):
    """伪造 url/phone 下单也不行：服务端强制用预设值。"""
    cookie = _token(server, USER, USER_PW)
    payload = {
        "url": "https://attacker.example.com",
        "phone": "19900001111",
        "password": "whatever",
        "excel": "/tmp/attacker.xlsx",
        "date": "",
        "count": "",
    }
    _api(server, cookie, "start_order", payload)
    config = server.bridge._config
    assert config.target_url != "https://attacker.example.com"
    assert config.phone_number != "19900001111"


def test_forced_payload_uses_preset_values(server):
    """直接验证强制逻辑（不依赖线程是否真的启动）。"""
    server.bridge._config.target_url = "https://preset.example.com"
    server.bridge._config.phone_number = "13900000000"
    server.bridge.is_admin = False
    forced = server.bridge._forced_order_payload(
        {"url": "https://attacker.example.com", "phone": "199", "password": "x", "remember": True})
    assert forced["url"] == "https://preset.example.com"
    assert forced["phone"] == "13900000000"
    assert forced["password"] == "", "普通用户不该借下单写入或替换平台口令"
    assert forced["remember"] is False


def test_non_admin_payload_uses_saved_password(server, monkeypatch):
    """非管理员看不到密码输入框，服务端必须用已保存的口令补上。

    否则会卡在校验「请输入登录密码」，表现为「点开始处理没反应」。
    """
    from app.api import bridge as bridge_module

    server.bridge._config.phone_number = "13900000000"
    monkeypatch.setattr(bridge_module, "get_password", lambda *_a, **_k: "saved-pw")
    server.bridge.is_admin = False
    forced = server.bridge._forced_order_payload({})
    assert forced["password"] == "saved-pw"
    assert forced["remember"] is False


def test_non_admin_payload_without_saved_password_stays_empty(server, monkeypatch):
    """密钥环里没存过口令时保持为空，让校验如实报错（由管理员去补）。"""
    from app.api import bridge as bridge_module

    server.bridge._config.phone_number = "13900000000"
    monkeypatch.setattr(bridge_module, "get_password", lambda *_a, **_k: None)
    server.bridge.is_admin = False
    assert server.bridge._forced_order_payload({})["password"] == ""


def test_admin_payload_is_not_forced(server):
    """管理员提交什么就是什么。"""
    server.bridge.is_admin = True
    payload = {"url": "https://mine.example.com", "phone": "13800000000"}
    server.bridge._forced_order_payload(payload)  # 只在非管理员时被调用
    assert payload["url"] == "https://mine.example.com"


# ----------------------------------------------------------------------
# 5. 授权范围内的功能仍然可用
# ----------------------------------------------------------------------
@pytest.mark.parametrize("method", ["worker_alive", "drain_events", "wps_status", "sss_day_orders"])
def test_non_admin_can_still_use_granted_methods(server, method):
    cookie = _token(server, USER, USER_PW)
    status, _ = _api(server, cookie, method)
    assert status == 200, f"{method} 应对普通用户开放，实际 {status}"


def test_non_admin_can_read_logs_via_events(server):
    server.bridge.log("普通用户也应看得见这行", "INFO")
    cookie = _token(server, USER, USER_PW)
    status, body = _api(server, cookie, "drain_events", 0)
    assert status == 200
    messages = [e.get("payload", {}).get("msg") for e in body.get("events", [])]
    assert any("普通用户也应看得见" in str(m) for m in messages)


def test_role_is_reset_between_requests(server):
    """Bridge 是长生命周期对象：切回普通用户后不得残留管理员权限。"""
    admin_cookie = _token(server, ADMIN, ADMIN_PW)
    user_cookie = _token(server, USER, USER_PW)
    _api(server, admin_cookie, "bridge_ready")
    assert server.bridge.is_admin is True
    _api(server, user_cookie, "bridge_ready")
    assert server.bridge.is_admin is False, "角色必须按当次会话重设，不能残留"
