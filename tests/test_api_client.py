"""``app.integrations.api_client`` 的登录态判定与网址解析回归锁。

改动前 ``tests/`` 里**没有任何一处**引用过这三个函数，而它们守的是两条要命的边界：

* ``is_auth_expired_payload`` —— 平台用 **HTTP 200 + ``code=10000``** 表示「登录态
  失效」，只看 HTTP 状态码是不够的。判错会导致**无限重登**或**静默失败**。
* ``origin_from_url`` —— 从后台网址里取出「协议 + 域名」，是纯接口模式构造请求头
  的基准；取错会让请求打向错误的主机。
"""
from __future__ import annotations

import json

import pytest

from app.integrations.api_client import (AUTH_ERROR_CODES, AUTH_MESSAGE_KEYWORDS,
                            AdminApiClient, ApiError, _browser_headers,
                            auth_error_message, is_auth_expired_payload,
                            origin_from_url)
from app.integrations import api_client as api_client_module
from app.integrations import android_web_login


# ----------------------------------------------------------------------
# is_auth_expired_payload
# ----------------------------------------------------------------------
@pytest.mark.parametrize("code", sorted(AUTH_ERROR_CODES))
def test_auth_expiry_detected_by_error_code(code):
    assert is_auth_expired_payload({"code": code}) is True


@pytest.mark.parametrize("code", ["401", "10000", " 10000 "])
def test_auth_expiry_detected_by_string_code(code):
    """接口有时把 code 序列化成字符串，必须按整数比较。"""
    assert is_auth_expired_payload({"code": code}) is True


@pytest.mark.parametrize("message", AUTH_MESSAGE_KEYWORDS)
def test_auth_expiry_detected_by_every_keyword(message):
    """每个关键词都要真的能命中 —— 少一个就可能漏判一次登录失效。"""
    assert is_auth_expired_payload({"message": message}) is True


def test_auth_expiry_message_match_is_case_insensitive():
    assert is_auth_expired_payload({"message": "TOKEN INVALID"}) is True
    assert is_auth_expired_payload({"message": "Unauthorized"}) is True
    assert is_auth_expired_payload({"message": "Login Expired"}) is True


def test_auth_expiry_accepts_msg_as_message_alias():
    assert is_auth_expired_payload({"msg": "登录已过期"}) is True


def test_auth_expiry_ignores_non_dict_and_garbage_code():
    for payload in (None, [], "token失效", 0, True):
        assert is_auth_expired_payload(payload) is False
    # code 转不成整数时不能崩，也不能误判成失效
    assert is_auth_expired_payload({"code": "abc"}) is False
    assert is_auth_expired_payload({"code": None}) is False
    assert is_auth_expired_payload({"code": [1, 2]}) is False


def test_normal_success_payload_is_not_treated_as_expired():
    """正常的成功响应**绝不能**被误判成登录失效，否则会无谓地重新登录。"""
    for payload in ({"code": 0, "message": "success"},
                    {"code": 200, "message": "ok", "data": {"x": 1}},
                    {}, {"message": "ok"}):
        assert is_auth_expired_payload(payload) is False


def test_message_path_works_even_when_code_is_absent():
    assert is_auth_expired_payload({"code": None, "message": "unauthorized"}) is True


# ----------------------------------------------------------------------
# auth_error_message
# ----------------------------------------------------------------------
def test_auth_error_message_includes_code_and_message():
    text = auth_error_message({"code": 10000, "message": "token失效，请重新登陆"})
    assert "10000" in text and "token失效" in text
    assert "重新登录" in text


def test_auth_error_message_includes_code_when_message_missing():
    text = auth_error_message({"code": 401})
    assert "401" in text and "重新登录" in text


def test_auth_error_message_uses_fallback_for_unusable_payload():
    assert auth_error_message(None, "兜底文案") == "兜底文案"
    assert auth_error_message({}, "兜底文案") == "兜底文案"
    # 空白 message + code 为 None → 只能用兜底
    assert auth_error_message({"code": None, "message": "   "}, "兜底") == "兜底"


def test_auth_error_message_has_default_when_no_fallback_given():
    assert auth_error_message(None) == "闪时送登录态已失效，请重新登录"
    assert auth_error_message({}, "") == "闪时送登录态已失效，请重新登录"


def test_auth_error_message_falls_back_for_non_dict():
    assert auth_error_message("boom", "兜底") == "兜底"


def test_auth_error_message_prefers_message_over_msg():
    text = auth_error_message({"code": 1, "message": "首选", "msg": "备选"})
    assert "首选" in text and "备选" not in text


# ----------------------------------------------------------------------
# origin_from_url
# ----------------------------------------------------------------------
@pytest.mark.parametrize("url,expected", [
    ("https://m.icall.me/admin/#/login", "https://m.icall.me"),
    ("https://m.icall.me", "https://m.icall.me"),
    ("http://x.cn:8080/a/b?c=d", "http://x.cn:8080"),
    ("https://sssplusnew.zhuopaikeji.com/takeout", "https://sssplusnew.zhuopaikeji.com"),
])
def test_origin_from_url_keeps_scheme_host_and_port(url, expected):
    assert origin_from_url(url) == expected


@pytest.mark.parametrize("url", ["", "   ", "m.icall.me/x", "/only/path", "//host/x", "not a url"])
def test_origin_from_url_rejects_urls_without_scheme_or_host(url):
    """缺协议或缺主机时必须**抛错**，不能返回一个能用的假源去打请求。"""
    with pytest.raises(ValueError):
        origin_from_url(url)


def test_origin_from_url_error_message_quotes_the_input():
    with pytest.raises(ValueError, match="无法从网址提取源"):
        origin_from_url("m.icall.me/x")


# ----------------------------------------------------------------------
# 管理后台 WAF 兼容：登录前预热 + 403 换新会话重试
# ----------------------------------------------------------------------
class _FakeResponse:
    def __init__(self, status_code: int, payload=None, text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class _FakeSession:
    """记录 GET/POST 顺序的最小 requests.Session 替身。"""

    instances: list["_FakeSession"] = []
    post_statuses: list[int] = []

    def __init__(self) -> None:
        self.headers: dict[str, str] = {}
        self.calls: list[tuple[str, str]] = []
        self.closed = False
        _FakeSession.instances.append(self)

    def get(self, url, **kwargs):
        self.calls.append(("GET", url))
        return _FakeResponse(200, {"ok": True})

    def post(self, url, **kwargs):
        self.calls.append(("POST", url))
        status = _FakeSession.post_statuses.pop(0) if _FakeSession.post_statuses else 200
        if status == 403:
            return _FakeResponse(403, None, "Denied by http_bot_simple")
        return _FakeResponse(200, {"code": 200, "data": {"token": "t", "uniacid": "u"}})

    def close(self) -> None:
        self.closed = True


def test_browser_headers_include_waf_fingerprint():
    headers = _browser_headers("https://m.icall.me", admin=True)
    assert "Mozilla/5.0" in headers["User-Agent"]
    assert headers["Origin"] == "https://m.icall.me"
    assert headers["Referer"] == "https://m.icall.me/admin/"
    assert headers["X-Requested-With"] == "XMLHttpRequest"
    assert headers["Sec-Fetch-Mode"] == "cors"


def test_admin_login_warms_waf_before_post(monkeypatch):
    _FakeSession.instances = []
    _FakeSession.post_statuses = []
    monkeypatch.setattr(api_client_module.requests, "Session", _FakeSession)

    client = AdminApiClient("https://m.icall.me/admin/#/login", "u", "p", timeout=5)
    client.login()

    session = _FakeSession.instances[0]
    assert session.calls[0] == ("GET", "https://m.icall.me/admin/")
    assert session.calls[1] == ("POST", "https://m.icall.me/channel/login")
    assert client.token == "t" and client.uniacid == "u"


def test_admin_login_retries_403_with_fresh_session(monkeypatch):
    _FakeSession.instances = []
    _FakeSession.post_statuses = [403, 200]
    monkeypatch.setattr(api_client_module.requests, "Session", _FakeSession)

    client = AdminApiClient("https://m.icall.me/admin/#/login", "u", "p", timeout=5)
    client.login()

    assert len(_FakeSession.instances) == 2
    first, second = _FakeSession.instances
    assert first.closed is True
    assert first.calls[0] == ("GET", "https://m.icall.me/admin/")
    assert first.calls[1] == ("POST", "https://m.icall.me/channel/login")
    assert second.calls[0] == ("GET", "https://m.icall.me/admin/")
    assert second.calls[1] == ("POST", "https://m.icall.me/channel/login")


def test_admin_login_reports_waf_after_retry(monkeypatch):
    _FakeSession.instances = []
    _FakeSession.post_statuses = [403, 403]
    monkeypatch.setattr(api_client_module.requests, "Session", _FakeSession)

    client = AdminApiClient("https://m.icall.me/admin/#/login", "u", "p", timeout=5)
    try:
        client.login()
    except ApiError as exc:
        assert "HTTP 403" in str(exc)
        assert "VPN" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("连续 403 必须抛 ApiError")


# ----------------------------------------------------------------------
# Android WebView 登录兜底
# ----------------------------------------------------------------------
def test_android_web_login_parses_native_envelope(monkeypatch):
    from app.integrations import android_web_login

    class _FakeJava:
        def loginJson(self, origin, username, password, timeout_ms):
            assert origin == "https://m.icall.me"
            assert username == "u" and password == "p"
            assert timeout_ms == 30_000
            return '{"ok": true, "status": 200, ' \
                   '"body": "{\\"code\\":200,\\"data\\":{\\"token\\":\\"t\\",\\"uniacid\\":\\"x\\"}}"}'

    monkeypatch.setattr(android_web_login, "is_android", lambda: True)
    monkeypatch.setattr(android_web_login, "_java_class", lambda: _FakeJava())

    payload = android_web_login.webview_login(
        "https://m.icall.me", "u", "p", timeout_ms=30_000)

    assert payload["code"] == 200
    assert payload["data"]["token"] == "t"


def test_admin_login_uses_webview_fallback_on_403(monkeypatch):
    _FakeSession.instances = []
    _FakeSession.post_statuses = [403]
    monkeypatch.setattr(api_client_module.requests, "Session", _FakeSession)

    client = AdminApiClient("https://m.icall.me/admin/#/login", "u", "p", timeout=5)
    monkeypatch.setattr(client, "_android_webview_login", lambda: {
        "code": 200,
        "data": {"token": "wv-token", "uniacid": "wv-uniacid"},
    })
    client.login()

    assert client.token == "wv-token"
    assert client.uniacid == "wv-uniacid"
    # 成功走 WebView 后不应再创建新 Session 做第二次 requests 重试。
    assert len(_FakeSession.instances) == 1


# ----------------------------------------------------------------------
# Android WebView 管理接口全链路
# ----------------------------------------------------------------------
def test_admin_login_prefers_android_webview(monkeypatch):
    _FakeSession.instances = []
    _FakeSession.post_statuses = []
    monkeypatch.setattr(api_client_module.requests, "Session", _FakeSession)
    monkeypatch.setattr(android_web_login, "is_android", lambda: True)
    monkeypatch.setattr(android_web_login, "webview_login", lambda *a, **k: {
        "code": 200, "data": {"token": "wv", "uniacid": "wx"}})

    client = AdminApiClient("https://m.icall.me/admin/#/login", "u", "p", timeout=5)
    client.login()

    assert client.token == "wv" and client.uniacid == "wx"
    assert client._native_web_http is True
    assert _FakeSession.instances[0].calls == []


def test_admin_get_json_uses_webview_after_login(monkeypatch):
    _FakeSession.instances = []
    _FakeSession.post_statuses = []
    monkeypatch.setattr(api_client_module.requests, "Session", _FakeSession)
    client = AdminApiClient("https://m.icall.me/admin/#/login", "u", "p", timeout=5)
    client.token = "t"
    client.uniacid = "x"
    client._native_web_http = True

    calls: list[tuple] = []
    def fake_fetch(origin, path, **kwargs):
        calls.append((origin, path, kwargs))
        return 200, '{"code":200,"data":{"list":[1,2]}}'

    monkeypatch.setattr(android_web_login, "is_android", lambda: True)
    monkeypatch.setattr(android_web_login, "webview_fetch", fake_fetch)

    payload = client.get_json("/channel/order?scene=1&pageNo=1")

    assert payload["data"]["list"] == [1, 2]
    assert calls[0][1] == "/channel/order?scene=1&pageNo=1"
    assert calls[0][2]["token"] == "t" and calls[0][2]["uniacid"] == "x"
    # 不应再调用 requests。
    assert _FakeSession.instances[0].calls == []


def test_webview_fetch_parses_native_envelope(monkeypatch):
    class _FakeJava:
        def fetchJson(self, origin, path, method, token, uniacid, body_json, timeout_ms):
            assert origin == "https://m.icall.me"
            assert path == "/channel/order"
            assert method == "GET"
            assert token == "t" and uniacid == "x"
            return json.dumps({
                "status": 200,
                "body": json.dumps({"code": 200, "data": {"ok": True}}),
            })

    monkeypatch.setattr(android_web_login, "is_android", lambda: True)
    monkeypatch.setattr(android_web_login, "_java_class", lambda: _FakeJava())

    status, body = android_web_login.webview_fetch(
        "https://m.icall.me", "/channel/order", token="t", uniacid="x")
    assert status == 200
    assert json.loads(body)["data"]["ok"] is True
