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


def _call(server, method, path, *, body=None, form=None, cookie=None, extra_headers=None):
    port = server.server_address[1]
    data, headers = None, dict(extra_headers or {})
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
    # 闪时送未决记录：列表含客户信息、核对要登录、解除会放开重发闸门，
    # 三者都默认仅管理员（白名单未登记 → 403）。
    "sss_uncertain_records",
    "start_sss_review",
    "sss_uncertain_resolve",
])
def test_non_admin_blocked_from_admin_methods(server, method):
    cookie = _token(server, USER, USER_PW)
    status, body = _api(server, cookie, method, {})
    assert status == 403, f"{method} 应被拦下，实际 {status}"
    assert body["code"] == "admin_only"
    assert body.get("ok") is not True, "权限失败绝不能返回 ok:true"


def test_mutex_failure_over_http_never_returns_ok_true(server):
    """HTTP 层也不得把互斥失败包装成成功。"""
    reservation = server.bridge._operations.try_reserve(
        "order", summary={"title": "订单处理"})
    assert reservation.granted
    try:
        cookie = _token(server, USER, USER_PW)
        status, body = _api(server, cookie, "start_order", {})
    finally:
        server.bridge._operations.finish(reservation.operation, status="success")

    assert status == 200
    assert body["ok"] is False
    assert body["status"] == "rejected"
    assert body["reason"] == "busy"
    assert body["code"] == "operation_conflict"
    assert body["operation_id"] == reservation.operation.operation_id


def test_operation_status_query_over_http_has_no_side_effects(server):
    """HTTP 断线恢复 query 只读：重复调用不创建操作、不改变 operation 计数。"""
    cookie = _token(server, USER, USER_PW)
    before_seq = server.bridge._operations._seq
    before_active = server.bridge._operations.active_operation_id()

    for _ in range(3):
        status, body = _api(server, cookie, "operation_status")
        assert status == 200
        assert body["ok"] is True
        assert body["active"] is False
        assert body["status"] == "idle"
        assert body["operation_id"] == ""

    status, ready = _api(server, cookie, "bridge_ready")
    assert status == 200
    assert ready["operation"]["status"] == "idle"
    assert server.bridge._operations._seq == before_seq
    assert server.bridge._operations.active_operation_id() == before_active


@pytest.mark.parametrize("mode", ["order", "sss"])
def test_non_admin_cannot_clear_password_namespaces(server, mode):
    """权限不足时 order/sss 两个命名空间都不得返回 ok:true。"""
    cookie = _token(server, USER, USER_PW)
    status, body = _api(server, cookie, "clear_password", {"mode": mode})
    assert status == 403
    assert body["code"] == "admin_only"
    assert body.get("ok") is not True


def test_admin_is_allowed_the_same_method(server):
    """同一方法管理员可调 —— 确认拦截是按角色而非误伤。"""
    cookie = _token(server, ADMIN, ADMIN_PW)
    status, _ = _api(server, cookie, "save_order_config", {})
    assert status == 200


def test_admin_uncertain_records_fail_closed_without_config(server):
    """管理员可调只读入口；但没配网址/账号时必须如实报错，不能假装“没有记录”。"""
    cookie = _token(server, ADMIN, ADMIN_PW)
    status, body = _api(server, cookie, "sss_uncertain_records")
    assert status == 200
    assert body["ok"] is False and body["code"] == "journal_unreadable"
    assert body["records"] == []
    assert "read_only" in body


def test_admin_uncertain_resolve_requires_confirmation(server):
    """管理员入口也必须走确认/备注校验；缺一不可，且不能返回 ok:true。"""
    cookie = _token(server, ADMIN, ADMIN_PW)
    status, body = _api(server, cookie, "sss_uncertain_resolve", {
        "decision": "station_absent", "confirm": "wrong",
        "note": "人工核对完成", "record_ids": ["cr-1"]})
    assert status == 200
    assert body["ok"] is False
    assert body["code"] in ("confirmation_required", "journal_unreadable")
    assert body["changed"] is False
    assert body["post_sent"] is False


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
@pytest.mark.parametrize("method", ["worker_alive", "drain_events", "wps_status",
                                   "sss_day_orders", "operation_status"])
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
    """Bridge 是长生命周期对象，但角色必须按请求会话生效、不能跨请求残留。

    共享实例的默认角色仍是普通用户；请求线程通过 ContextVar 覆盖，
    因此主线程直接读 ``server.bridge.is_admin`` 不应保留任何 HTTP 会话角色。
    """
    admin_cookie = _token(server, ADMIN, ADMIN_PW)
    user_cookie = _token(server, USER, USER_PW)
    _, admin_body = _api(server, admin_cookie, "bridge_ready")
    assert admin_body["is_admin"] is True
    assert server.bridge.is_admin is False, "请求结束后共享实例不应保留管理员角色"

    _, user_body = _api(server, user_cookie, "bridge_ready")
    assert user_body["is_admin"] is False
    assert server.bridge.is_admin is False


def test_wps_recovery_status_auth_and_redaction_over_http(server, monkeypatch):
    import app.api.bridge as bridge_module

    raw = {
        "ok": True,
        "journal_path": "/tmp/private.journal",
        "operations": [{
            "operation_id": "张三-wps-secret",
            "status": "uncertain",
            "target_date": "2026-09-20",
            "sheets": [{
                "sheet": "东湖中餐", "file_id": "FILE-CUSTOMER-ID",
                "target_date": "2026-09-20",
                "target_ref": "张三 13800000000 浙江农林大学",
                "status": "uncertain", "raw_status": "uncertain",
                "risk_reason": "张三: 总餐次应为 9，实际 3",
                "reason": "异常原文",
                "problems": ["13800000000 浙江农林大学 豪华餐 9"],
                "next_action": "manual_reconcile",
            }],
        }],
        "pending_operations": [{"operation_id": "张三-wps-secret"}],
        "counts": {"uncertain": 1, "future_unknown_count": 99},
        "next_action": "manual_reconcile",
        "future_secret": "SENSITIVE_FUTURE_FIELD",
    }
    monkeypatch.setattr(
        bridge_module, "_wps_recovery_status_contract",
        lambda _ledger: raw)

    status, _body = _call(server, "POST", "/api/wps_recovery_status", body=[])
    assert status == 401

    markers = ("张三", "13800000000", "浙江农林大学", "豪华餐",
               "SENSITIVE_FUTURE_FIELD", "异常原文", "FILE-CUSTOMER-ID",
               "总餐次应为")

    cookie = _token(server, USER, USER_PW)
    status, body = _api(server, cookie, "wps_recovery_status")
    assert status == 200
    user_text = json.dumps(body, ensure_ascii=False)
    assert all(marker not in user_text for marker in markers)
    assert body["scope"] == "summary"
    assert "operations" not in body
    assert "journal_path" not in body
    assert "future_unknown_count" not in body["counts"]
    assert body["summary"]["needs_review"] is True

    admin_cookie = _token(server, ADMIN, ADMIN_PW)
    status, admin_body = _api(server, admin_cookie, "wps_recovery_status")
    assert status == 200
    admin_text = json.dumps(admin_body, ensure_ascii=False)
    assert all(marker not in admin_text for marker in markers)
    assert admin_body["scope"] == "admin"
    assert "future_secret" not in admin_body
    assert "journal_path" not in admin_body
    assert admin_body["operations"][0]["operation_id"] == ""


def test_pending_interactions_http_owner_scope_and_no_activation(server):
    """HTTP 断线恢复：本人才可查询/处理自己的 pending 交互；查询不改状态。"""
    other_user = "other@example.com"
    other_pw = "otherpw123"
    server.auth.register(other_user, other_pw)
    server.auth.approve(other_user, by=ADMIN)

    bridge = server.bridge
    bridge._task_owner = USER
    bridge._task_owner_is_admin = False
    bridge._task_operation_id = "op-http-pending"
    try:
        interaction_id, entry = bridge._register_interaction(
            "decision", request={
                "title": "请选择 张三 13800000000",
                "message": "浙江农林大学 豪华餐 总餐次应为 9",
                "choices": [{"value": "retry", "label": "张三 13800000000",
                             "future_unknown": "SENSITIVE_FUTURE_FIELD"}],
                "password": "HTTP-SECRET-PW",
            })
        bridge._task_owner = ""
        bridge._task_operation_id = ""

        status, _body = _call(server, "POST", "/api/pending_interactions",
                              body=[])
        assert status == 401, "未登录查询必须 401"

        user_cookie = _token(server, USER, USER_PW)
        user_status, user_body = _api(server, user_cookie,
                                      "pending_interactions")
        assert user_status == 200
        assert [item["interaction_id"]
                for item in user_body["interactions"]] == [interaction_id]
        assert user_body["interactions"][0]["operation_id"] == "op-http-pending"
        text = json.dumps(user_body, ensure_ascii=False)
        assert "HTTP-SECRET-PW" not in text
        # 普通 owner 只得到白名单 request 字段；未知嵌套字段被丢弃。
        assert set(user_body["interactions"][0]["request"]) == {
            "title", "message", "choices"}
        assert set(user_body["interactions"][0]["request"]["choices"][0]) == {
            "value", "label", "style"}

        other_cookie = _token(server, other_user, other_pw)
        other_status, other_body = _api(server, other_cookie,
                                        "pending_interactions")
        assert other_status == 200
        assert other_body["interactions"] == []

        admin_cookie = _token(server, ADMIN, ADMIN_PW)
        admin_status, admin_body = _api(server, admin_cookie,
                                        "pending_interactions")
        assert admin_status == 200
        assert [item["interaction_id"]
                for item in admin_body["interactions"]] == [interaction_id]
        admin_item = admin_body["interactions"][0]
        assert admin_item["request"] == {}
        assert admin_item.get("request_redacted") is True
        admin_text = json.dumps(admin_body, ensure_ascii=False)
        assert "张三" not in admin_text and "13800000000" not in admin_text
        assert "浙江农林大学" not in admin_text and "豪华餐" not in admin_text
        assert not entry.event.is_set()
        assert interaction_id in bridge._decisions, "查询不能消费/取消交互"
    finally:
        bridge._cancel_pending_interactions("测试清理")


def test_concurrent_admin_and_user_requests_do_not_share_role(server, monkeypatch):
    """复现并锁死共享 Bridge.is_admin 的线程串扰：管理员请求被普通用户请求抢角色。

    旧实现把角色写回共享实例属性；本测试让管理员请求在鉴权后、方法分发前停住，
    再让普通用户请求完整跑一遍，最后恢复管理员请求。旧实现会看到此时角色已被
    改成普通用户而返回 403；请求局部 ContextVar 实现仍返回 200。
    """
    import app.web.server as server_module

    original_require = server_module._Handler._require_token
    admin_entered = threading.Event()
    release_admin = threading.Event()

    def slow_require(handler):
        if handler.headers.get("X-Test-Barrier") == "admin":
            admin_entered.set()
            assert release_admin.wait(3.0), "测试未释放管理员请求"
        return original_require(handler)

    monkeypatch.setattr(server_module._Handler, "_require_token", slow_require)
    admin_cookie = _token(server, ADMIN, ADMIN_PW)
    user_cookie = _token(server, USER, USER_PW)
    results: dict[str, tuple[int, str]] = {}

    def admin_request() -> None:
        results["admin"] = _call(
            server, "POST", "/api/save_order_config", body=[{}],
            cookie=admin_cookie, extra_headers={"X-Test-Barrier": "admin"})

    admin_thread = threading.Thread(target=admin_request)
    admin_thread.start()
    assert admin_entered.wait(3.0), "管理员请求应已停在鉴权后"

    user_status, user_text = _call(server, "POST", "/api/bridge_ready",
                                   body=[], cookie=user_cookie)
    user_body = json.loads(user_text)

    release_admin.set()
    admin_thread.join(3.0)
    admin_status, admin_text = results["admin"]

    assert admin_status == 200, (
        f"管理员请求被普通用户请求串权限：{admin_status} {admin_text}")
    assert json.loads(admin_text)["ok"] is True
    assert user_status == 200 and user_body["is_admin"] is False


# ----------------------------------------------------------------------
# R3：真实 HTTP 链路重放敏感 journal 恢复查询
# ----------------------------------------------------------------------
RECOVERY_LEAK_MARKERS = (
    "张三", "13800000000", "浙江农林大学", "豪华餐", "总餐次应为",
    "异常原文", "FILE-CUSTOMER-ID", "/home/客户", "RuntimeError",
)


def _write_sensitive_recovery_journal(data_dir):
    """用 B 的真实 SyncJournal 写入合成敏感 journal；不碰真实用户数据。"""
    from app.wps.journal import SyncJournal, journal_path_for

    data_dir.mkdir(parents=True, exist_ok=True)
    ledger_path = data_dir / "wps_sync_state.json"
    ledger_path.write_text(
        json.dumps({"version": 1, "batches": {}}, ensure_ascii=False),
        encoding="utf-8")
    journal_path = journal_path_for(ledger_path)
    journal = SyncJournal(journal_path)
    operation_id = "wps-" + "a" * 16
    sheet_key = "东湖中餐"
    journal.create_operation(operation_id, {sheet_key: {
        "sheet": "东湖中餐",
        "file_id": "FILE-CUSTOMER-ID",
    }}, target_date="2026-09-20")
    journal.set_sheet_status(
        operation_id, sheet_key, "uncertain",
        reason="张三: 总餐次应为 9，实际 3",
        problems=["13800000000 浙江农林大学 豪华餐 9"],
        risk_reason="异常原文",
        manual_required="张三",
        cloud_checked=True,
        target_date="2026-09-20",
        nested_unknown={
            "name": "张三", "phone": "13800000000",
            "path": "/home/客户/张三/排单.xlsx",
        },
        error_text="RuntimeError: 张三 13800000000",
    )
    journal.save()
    return journal_path, ledger_path, operation_id


def _assert_recovery_no_leak(payload):
    text = json.dumps(payload, ensure_ascii=False)
    for marker in RECOVERY_LEAK_MARKERS:
        assert marker not in text, f"HTTP 恢复响应泄漏标记 {marker}: {text}"


def test_http_recovery_sensitive_journal_redaction_and_readonly(
        server, tmp_path, monkeypatch):
    assert server.server_address[0] == "127.0.0.1", "测试服务必须仅监听 loopback"
    data_dir = tmp_path / "recovery-data"
    journal_path, ledger_path, operation_id = _write_sensitive_recovery_journal(
        data_dir)
    monkeypatch.setenv("YIKOU_DATA_DIR", str(data_dir))

    journal_before = journal_path.read_bytes()
    ledger_before = ledger_path.read_bytes()
    previews_before = len(server.bridge._previews._items)
    apply_calls: list[object] = []
    import app.api.bridge as bridge_module
    monkeypatch.setattr(bridge_module, "apply_plan", lambda *a, **k: apply_calls.append(a))

    status, unauth_text = _call(server, "POST", "/api/wps_recovery_status", body=[])
    assert status == 401
    _assert_recovery_no_leak(unauth_text)

    user_cookie = _token(server, USER, USER_PW)
    user_status, user_body = _api(server, user_cookie, "wps_recovery_status")
    assert user_status == 200
    _assert_recovery_no_leak(user_body)
    assert user_body["scope"] == "summary"
    assert "operations" not in user_body
    assert "pending_operations" not in user_body
    assert "journal_path" not in user_body
    assert user_body["counts"]["uncertain"] == 1
    assert user_body["next_action"] == "manual_reconcile"
    assert user_body["summary"]["needs_review"] is True
    assert "只读核对" in user_body["summary"]["guidance"]

    admin_cookie = _token(server, ADMIN, ADMIN_PW)
    admin_status, admin_body = _api(server, admin_cookie, "wps_recovery_status")
    assert admin_status == 200
    _assert_recovery_no_leak(admin_body)
    assert admin_body["scope"] == "admin"
    assert "journal_path" not in admin_body
    assert admin_body["operations"][0]["operation_id"] == operation_id
    assert admin_body["operations"][0]["target_refs"]
    assert all(ref.startswith("wps-target:")
               for ref in admin_body["operations"][0]["target_refs"])
    flat = json.dumps(admin_body, ensure_ascii=False)
    for forbidden in ("problems", "risk_reason", "reason", "sheet\":",
                      "file_id", "nested_unknown", "error_text"):
        assert forbidden not in flat, f"管理员响应出现字段 {forbidden}"

    # 只读：journal/ledger 字节不变，预览未消费，未触发上传。
    assert journal_path.read_bytes() == journal_before
    assert ledger_path.read_bytes() == ledger_before
    assert len(server.bridge._previews._items) == previews_before
    assert apply_calls == []


def test_http_recovery_corrupt_journal_is_safe_error(
        server, tmp_path, monkeypatch):
    data_dir = tmp_path / "recovery-corrupt"
    data_dir.mkdir(parents=True)
    ledger_path = data_dir / "wps_sync_state.json"
    ledger_path.write_text(json.dumps({"version": 1, "batches": {}}), encoding="utf-8")
    journal_path = data_dir / "wps_sync_state.json.journal"
    journal_path.write_text(
        '{"broken": "张三 13800000000 浙江农林大学 豪华餐",',
        encoding="utf-8")
    monkeypatch.setenv("YIKOU_DATA_DIR", str(data_dir))
    journal_before = journal_path.read_bytes()
    ledger_before = ledger_path.read_bytes()

    user_cookie = _token(server, USER, USER_PW)
    admin_cookie = _token(server, ADMIN, ADMIN_PW)
    for cookie in (user_cookie, admin_cookie):
        status, body = _api(server, cookie, "wps_recovery_status")
        assert status == 200
        _assert_recovery_no_leak(body)
        assert body["ok"] is False
        assert body["error_code"] == "wps_recovery_journal_unreadable"
        assert body["next_action"] == "fix_journal"
        assert "reason" not in body
        assert body["counts"]["uncertain"] == 0
        if body["scope"] == "summary":
            assert "operations" not in body
        else:
            assert body["operations"] == []

    assert journal_path.read_bytes() == journal_before
    assert ledger_path.read_bytes() == ledger_before


def test_http_recovery_contract_exception_does_not_bypass_safe_dto(
        server, tmp_path, monkeypatch):
    import app.api.bridge as bridge_module

    data_dir = tmp_path / "recovery-exception"
    data_dir.mkdir(parents=True)
    (data_dir / "wps_sync_state.json").write_text(
        json.dumps({"version": 1, "batches": {}}), encoding="utf-8")
    monkeypatch.setenv("YIKOU_DATA_DIR", str(data_dir))

    def boom(_ledger):
        raise RuntimeError("异常原文 张三 13800000000 浙江农林大学 豪华餐")

    monkeypatch.setattr(bridge_module, "_wps_recovery_status_contract", boom)

    user_cookie = _token(server, USER, USER_PW)
    admin_cookie = _token(server, ADMIN, ADMIN_PW)
    for cookie in (user_cookie, admin_cookie):
        status, body = _api(server, cookie, "wps_recovery_status")
        assert status == 200
        _assert_recovery_no_leak(body)
        assert body["ok"] is False
        assert body["error_code"] == "wps_recovery_internal_error"
        assert body["next_action"] == "fix_journal"
        assert body["summary"]["needs_review"] is True
        assert "reason" not in body


# ----------------------------------------------------------------------
# 6. W3：管理员恢复/退场入口的 HTTP 权限与审计（普通用户 403）
# ----------------------------------------------------------------------
def _write_pending_journal(data_dir, *, target_date="2026-09-20"):
    """在临时数据目录里造一条 pending 操作（合成数据，不碰真实账本）。"""
    from app.wps.journal import SyncJournal, journal_path_for, new_operation_id

    data_dir.mkdir(parents=True, exist_ok=True)
    ledger_path = data_dir / "wps_sync_state.json"
    ledger_path.write_text(json.dumps({"version": 1, "batches": {}}),
                           encoding="utf-8")
    journal_path = journal_path_for(ledger_path)
    journal = SyncJournal(journal_path)
    operation_id = new_operation_id()
    journal.create_operation(operation_id, {
        "0:东湖中餐:F-SYNTH": {
            "sheet": "东湖中餐", "file_id": "F-SYNTH",
            "target_date": target_date, "status": "writing",
            "next_action": "recover_journal",
            "reason": "写入过程中断电，无法判断是否已写", "problems": [],
        },
    }, target_date=target_date)
    journal.save()
    return ledger_path, journal_path, operation_id


def _retire_payload(operation_id, **overrides):
    payload = {
        "operation_id": operation_id,
        "decision": "retire_guarded",
        "confirm": "retire_guarded",
        "note": "人工核对云端后仍无法判定，保留防重复闸门退出",
        "confirm_structure_checked": True,
    }
    payload.update(overrides)
    return payload


def test_non_admin_cannot_call_wps_recovery_resolve_over_http(server, tmp_path,
                                                              monkeypatch):
    data_dir = tmp_path / "recovery-resolve-user"
    monkeypatch.setenv("YIKOU_DATA_DIR", str(data_dir))
    _ledger_path, journal_path, operation_id = _write_pending_journal(data_dir)
    before = journal_path.read_bytes()

    cookie = _token(server, USER, USER_PW)
    status, body = _api(server, cookie, "wps_recovery_resolve",
                        _retire_payload(operation_id))

    assert status == 403, body
    assert body["code"] == "admin_only"
    assert body.get("ok") is not True
    assert journal_path.read_bytes() == before, "越权请求不得改 journal"
    from app.wps.journal import SyncJournal
    assert operation_id in SyncJournal(journal_path).pending_operations()


@pytest.mark.parametrize("payload_overrides,expected_code", [
    ({"confirm": "nope"}, "confirmation_required"),
    ({"note": ""}, "note_required"),
    ({"decision": "force_clear", "confirm": "force_clear"}, "decision_not_allowed"),
])
def test_admin_bad_confirmation_over_http_is_rejected_not_500(
        server, tmp_path, monkeypatch, payload_overrides, expected_code):
    data_dir = tmp_path / "recovery-resolve-bad"
    monkeypatch.setenv("YIKOU_DATA_DIR", str(data_dir))
    _ledger_path, journal_path, operation_id = _write_pending_journal(data_dir)
    before = journal_path.read_bytes()

    cookie = _token(server, ADMIN, ADMIN_PW)
    status, body = _api(server, cookie, "wps_recovery_resolve",
                        _retire_payload(operation_id, **payload_overrides))

    assert status == 200, body
    assert body["ok"] is False
    assert body["code"] == expected_code
    assert body["changed"] is False
    assert body["cloud_write"] is False
    assert journal_path.read_bytes() == before


def test_admin_can_retire_pending_operation_over_http_with_audit(
        server, tmp_path, monkeypatch):
    data_dir = tmp_path / "recovery-resolve-admin"
    monkeypatch.setenv("YIKOU_DATA_DIR", str(data_dir))
    _ledger_path, journal_path, operation_id = _write_pending_journal(data_dir)

    admin_cookie = _token(server, ADMIN, ADMIN_PW)
    status, body = _api(server, admin_cookie, "wps_recovery_resolve",
                        _retire_payload(operation_id))

    assert status == 200, body
    assert body["ok"] is True
    assert body["status"] == "retired_guarded"
    assert body["changed"] is True
    assert body["verified_on_disk"] is True
    assert body["cloud_write"] is False
    assert body["scope"]["guard_retained"] is True
    assert body["scope"]["blocking"] == "retired_guarded"
    assert body["scope"]["target_dates"] == ["2026-09-20"]
    assert body["audit"]["decision"] == "retire_guarded"
    assert body["audit"]["note_recorded"] is True
    assert body["next_action"] == "manual_reconcile"
    # 响应里不能出现 sheet 原名 / file_id / 客户信息。
    raw = json.dumps(body, ensure_ascii=False)
    assert "东湖中餐" not in raw
    assert "F-SYNTH" not in raw
    assert "张三" not in raw

    from app.wps.journal import SyncJournal
    journal = SyncJournal(journal_path)
    assert operation_id not in journal.pending_operations()
    assert journal.has_guard("2026-09-20", "F-SYNTH") is True

    # 重复请求：幂等，且不再写盘。
    after_first = journal_path.read_bytes()
    status2, second = _api(server, admin_cookie, "wps_recovery_resolve",
                           _retire_payload(operation_id))
    assert status2 == 200
    assert second["status"] == "already_retired"
    assert second["changed"] is False
    assert journal_path.read_bytes() == after_first

    # 普通用户依旧只能看到安全摘要，但能看到“有退场待人工核对”。
    user_cookie = _token(server, USER, USER_PW)
    status3, user_view = _api(server, user_cookie, "wps_recovery_status")
    assert status3 == 200
    assert user_view["counts"]["retired_guarded"] == 1
    assert user_view["summary"]["retired_guarded_count"] == 1
    assert user_view["summary"]["needs_review"] is True
    assert "operations" not in user_view
    assert "东湖中餐" not in json.dumps(user_view, ensure_ascii=False)


def test_admin_retire_unknown_operation_over_http_is_404_like_not_500(
        server, tmp_path, monkeypatch):
    data_dir = tmp_path / "recovery-resolve-missing"
    monkeypatch.setenv("YIKOU_DATA_DIR", str(data_dir))
    _write_pending_journal(data_dir)
    cookie = _token(server, ADMIN, ADMIN_PW)

    status, body = _api(server, cookie, "wps_recovery_resolve",
                        _retire_payload("wps-ffffffffffffffff"))

    assert status == 200, body
    assert body["ok"] is False
    assert body["code"] == "not_found"
    assert body["changed"] is False


def test_w5_disabled_over_http_refuses_preview_and_upload_for_normal_user(server):
    """W5 的 HTTP 证据：关闭云同步后普通用户直调接口也写不进云端。"""
    server.bridge._config.wps_enabled = False
    cookie = _token(server, USER, USER_PW)

    status, preview = _api(server, cookie, "wps_preview")
    assert status == 200
    assert preview["ok"] is False
    assert preview["code"] == "wps_disabled"
    assert preview["execution_summary"]["proven_no_write"] is True

    status, upload = _api(server, cookie, "wps_upload", "pv-old-token")
    assert status == 200
    assert upload["ok"] is False
    assert upload["code"] == "wps_disabled"
    assert upload["written"] == 0
    assert upload["execution_summary"]["proven_no_write"] is True
