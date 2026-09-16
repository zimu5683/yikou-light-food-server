"""Tests for app/web_server.py —— 网页版 HTTP 桥接服务。

覆盖：令牌鉴权（含拒绝路径）、桥接方法分发与参数语义、文件浏览器、
静态资源与目录穿越防护、窗口适配层、以及不对外暴露的方法。
"""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest

from app.web_server import (EXCEL_SUFFIXES, WebServerError, _WebWindow,
                            create_server, lan_addresses, load_or_create_token)


@pytest.fixture()
def server(tmp_path, monkeypatch):
    """在临时端口上跑一个真实的 HTTP 服务，并用临时配置文件隔离用户状态。"""
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html><title>t</title>", encoding="utf-8")
    monkeypatch.setattr("app.web_server.user_data_dir", lambda: tmp_path / "cfg")
    httpd = create_server("127.0.0.1", 0, dist_dir=dist, token="secret",
                          config_path=tmp_path / "config.json")
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield httpd
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def _request(server, method, path, body=None, token="secret", raw=False):
    """发一个请求，返回 (status, 解析后的 body 或原始文本)。"""
    port = server.server_address[1]
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, method=method)
    request.add_header("Content-Type", "application/json")
    if token is not None:
        request.add_header("X-Yikou-Token", token)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            text = response.read().decode("utf-8")
            return response.status, (text if raw else json.loads(text or "null"))
    except urllib.error.HTTPError as exc:
        text = exc.read().decode("utf-8")
        if raw:
            return exc.code, text
        try:
            return exc.code, json.loads(text)
        except json.JSONDecodeError:
            return exc.code, text


# ----------------------------------------------------------------------
# 令牌
# ----------------------------------------------------------------------
def test_token_file_is_created_and_reused(tmp_path, monkeypatch):
    monkeypatch.setattr("app.web_server.user_data_dir", lambda: tmp_path)
    first = load_or_create_token()
    assert first
    # 第二次读取同一个令牌，保证客户端收藏的网址长期有效。
    assert load_or_create_token() == first
    assert (tmp_path / "web_token").read_text(encoding="utf-8").strip() == first


def test_rotate_token_replaces_existing(tmp_path, monkeypatch):
    monkeypatch.setattr("app.web_server.user_data_dir", lambda: tmp_path)
    first = load_or_create_token()
    assert load_or_create_token(rotate=True) != first


def test_healthz_needs_no_token(server):
    status, body = _request(server, "GET", "/healthz", token=None)
    assert status == 200
    assert body == {"ok": True}


@pytest.mark.parametrize("token", [None, "", "wrong"])
def test_bridge_calls_require_a_valid_token(server, token):
    status, body = _request(server, "POST", "/api/bridge_ready", [], token=token)
    assert status == 401
    assert body["code"] == "unauthorized"


def test_token_is_also_accepted_from_query_string(server):
    status, body = _request(server, "POST", "/api/bridge_ready?token=secret", [],
                            token=None)
    assert status == 200
    assert body["version"]


# ----------------------------------------------------------------------
# 桥接分发
# ----------------------------------------------------------------------
def test_bridge_ready_round_trip(server):
    status, body = _request(server, "POST", "/api/bridge_ready", [])
    assert status == 200
    assert body["event_producer_id"]
    assert "config" in body and "passwords" in body


def test_positional_arguments(server):
    status, body = _request(server, "POST", "/api/echo_test", ["hi", {"a": 1, "b": 2}])
    assert status == 200
    assert body == {"echo": "hi", "payload_keys": ["a", "b"]}


def test_keyword_arguments(server):
    status, body = _request(server, "POST", "/api/echo_test", {"message": "kw"})
    assert status == 200
    assert body["echo"] == "kw"


def test_empty_body_means_no_arguments(server):
    status, body = _request(server, "POST", "/api/worker_alive", None)
    assert status == 200
    assert body is False


def test_mismatched_arguments_return_400(server):
    status, body = _request(server, "POST", "/api/echo_test", ["a", "b", "c", "d"])
    assert status == 400
    assert body["code"] == "bad_arguments"


def test_invalid_json_returns_400(server):
    port = server.server_address[1]
    request = urllib.request.Request(f"http://127.0.0.1:{port}/api/echo_test",
                                     data=b"{not json", method="POST")
    request.add_header("X-Yikou-Token", "secret")
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(request, timeout=10)
    assert excinfo.value.code == 400


def test_unknown_method_returns_404(server):
    status, body = _request(server, "POST", "/api/no_such_method", [])
    assert status == 404
    assert body["code"] == "unknown_method"


@pytest.mark.parametrize("name", ["_run_order", "_emit_event", "attach", "on_native_closing"])
def test_private_and_non_http_methods_are_not_exposed(server, name):
    status, _ = _request(server, "POST", f"/api/{name}", [])
    assert status == 404


def test_bridge_methods_reject_get(server):
    status, body = _request(server, "GET", "/api/bridge_ready")
    assert status == 405
    assert body["code"] == "use_post"


def test_state_change_survives_round_trip(server):
    status, body = _request(server, "POST", "/api/set_split_ratio", [0.99])
    assert status == 200
    # 复用了 config.clamp_split_ratio 的夹紧逻辑，说明真的调到了 Bridge。
    assert body["ratio"] == pytest.approx(0.55)


# ----------------------------------------------------------------------
# 静态资源
# ----------------------------------------------------------------------
def test_index_html_is_served_without_token(server):
    status, body = _request(server, "GET", "/", token=None, raw=True)
    assert status == 200
    assert "<!doctype html" in body.lower()


def test_missing_asset_returns_404(server):
    status, _ = _request(server, "GET", "/nope.js", token=None)
    assert status == 404


@pytest.mark.parametrize("path", ["/%2e%2e/%2e%2e/etc/passwd", "/..%2f..%2fetc%2fpasswd"])
def test_path_traversal_is_rejected(server, path):
    status, _ = _request(server, "GET", path, token=None)
    assert status in (403, 404)


# ----------------------------------------------------------------------
# 文件浏览器
# ----------------------------------------------------------------------
def test_fs_list_returns_dirs_and_excel_only(server, tmp_path):
    root = tmp_path / "browse"
    (root / "sub").mkdir(parents=True)
    (root / "排单.xlsx").write_text("x", encoding="utf-8")
    (root / "note.txt").write_text("x", encoding="utf-8")
    (root / ".hidden.xlsx").write_text("x", encoding="utf-8")

    status, body = _request(server, "GET", f"/api/fs/list?path={root}")
    assert status == 200
    names = {entry["name"] for entry in body["entries"]}
    assert names == {"sub", "排单.xlsx"}
    assert body["error"] == ""
    assert body["parent"]


def test_fs_list_reports_missing_directory(server, tmp_path):
    status, body = _request(server, "GET", f"/api/fs/list?path={tmp_path / 'nope'}")
    assert status == 200
    assert body["entries"] == []
    assert "不存在" in body["error"]


def test_fs_list_is_authenticated(server, tmp_path):
    status, body = _request(server, "GET", f"/api/fs/list?path={tmp_path}", token=None)
    assert status == 401
    assert body["code"] == "unauthorized"


def test_fs_list_result_is_json_serialisable(server, tmp_path):
    status, body = _request(server, "GET", f"/api/fs/list?path={tmp_path}")
    assert status == 200
    assert json.loads(json.dumps(body, ensure_ascii=False)) == body


def test_excel_suffixes_cover_the_dialog_filters():
    assert {".xlsx", ".xlsm"} <= EXCEL_SUFFIXES


# ----------------------------------------------------------------------
# 窗口适配层
# ----------------------------------------------------------------------
def test_web_window_destroy_triggers_callback_once():
    calls = []
    window = _WebWindow(lambda: calls.append(1))
    window.destroy()
    window.destroy()
    assert calls == [1]


def test_web_window_file_dialog_returns_empty_selection():
    # 网页版没有原生对话框；空选择让 Bridge.choose_excel 走提示分支。
    assert _WebWindow().create_file_dialog(None, allow_multiple=False) == ()


def test_web_window_window_actions_are_noops():
    window = _WebWindow()
    assert window.minimize() is None
    assert window.restore() is None
    assert window.maximize() is None


def test_destroy_callback_shuts_the_server_down(tmp_path, monkeypatch):
    """request_close 走到窗口 destroy 时应当关闭服务（对应网页版「停止服务」）。"""
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html>", encoding="utf-8")
    monkeypatch.setattr("app.web_server.user_data_dir", lambda: tmp_path / "cfg")
    stopped = threading.Event()
    httpd = create_server("127.0.0.1", 0, dist_dir=dist, token="secret",
                          on_destroy=stopped.set, config_path=tmp_path / "config.json")
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        httpd.bridge._window.destroy()
        assert stopped.wait(timeout=5)
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


# ----------------------------------------------------------------------
# 启动校验与地址探测
# ----------------------------------------------------------------------
def test_missing_frontend_build_is_reported(tmp_path):
    with pytest.raises(WebServerError) as excinfo:
        create_server("127.0.0.1", 0, dist_dir=tmp_path / "empty")
    assert "pnpm build" in str(excinfo.value)


def test_lan_addresses_never_include_loopback():
    for name, address in lan_addresses():
        assert name != "lo"
        assert not address.startswith("127.")


def test_lan_addresses_rank_ethernet_before_vpn(monkeypatch):
    monkeypatch.setattr("app.web_server._interface_addresses",
                        lambda: [("tun0", "172.19.0.1"), ("wlan0", "10.0.0.5")])
    # WiFi 必须排在 VPN 前面：VPN 地址其它设备根本连不上。
    assert [name for name, _ in lan_addresses()] == ["wlan0", "tun0"]
