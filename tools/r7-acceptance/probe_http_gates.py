"""R7 独立验收探针 4：直接 HTTP 调用下的 W5（WPS 禁用）、恢复权限、危险并发（W7）。

不是调 Python 方法，而是起真实 ``app.web.server``（loopback + 会话 cookie），
用 HTTP POST 打这些端点，验证闸门在**服务端**生效：

    PYTHONPATH=<repo> python3 probe_http_gates.py [--mutate-open-gates]

``--mutate-open-gates`` 会关掉服务端闸门（wps_enabled 检查 + 只读入口占位），
用来证明本探针能抓到“只在后端 Python 层加了闸门、HTTP 层却能绕过”的回归。
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ADMIN = "admin@example.com"
ADMIN_PW = "adminpw123"
USER = "worker@example.com"
USER_PW = "workerpw123"


def _call(server, method, path, *, body=None, form=None, cookie=None):
    from app.web.server import SESSION_COOKIE
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
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data,
                                     method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status, response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


def _token(server, username, password):
    port = server.server_address[1]
    data = urllib.parse.urlencode({"action": "login", "username": username,
                                   "password": password}).encode("utf-8")
    request = urllib.request.Request(f"http://127.0.0.1:{port}/login", data=data,
                                     method="POST")
    request.add_header("Content-Type", "application/x-www-form-urlencoded")

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None

    opener = urllib.request.build_opener(_NoRedirect)
    try:
        opener.open(request, timeout=20)
    except urllib.error.HTTPError:
        pass
    # 再登录一次拿 Set-Cookie（沿用项目既有测试的做法）
    opener2 = urllib.request.build_opener(_NoRedirect)
    try:
        response = opener2.open(request, timeout=20)
        raw = response.headers.get("Set-Cookie") or ""
    except urllib.error.HTTPError as exc:
        raw = exc.headers.get("Set-Cookie") or ""
    from app.web.server import SESSION_COOKIE
    for part in raw.split(";"):
        if part.strip().startswith(f"{SESSION_COOKIE}="):
            return part.strip().split("=", 1)[1]
    return ""


def _json(text: str):
    try:
        return json.loads(text)
    except Exception:
        return {}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mutate-open-gates", action="store_true")
    args = parser.parse_args()

    from app.web.auth import AuthStore
    from app.web.server import create_server

    if args.mutate_open_gates:
        import app.api.bridge as bridge_module
        real = bridge_module.Bridge._begin_cloud_read

        def loose_begin(self, mode, title, next_action):  # type: ignore[no-untyped-def]
            # 还原“只读入口不占位”的旧行为
            return None, None
        bridge_module.Bridge._begin_cloud_read = loose_begin
        print("[MUTATION] 已关闭只读云入口的服务端互斥（_begin_cloud_read 直接放行）")

    tmp = Path(tempfile.mkdtemp(prefix="r7-http-"))
    dist = tmp / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html><title>app</title>", encoding="utf-8")
    auth = AuthStore(tmp / "cfg")
    auth.create_admin(ADMIN, ADMIN_PW)
    auth.register(USER, USER_PW)
    auth.approve(USER, by=ADMIN)

    httpd = create_server("127.0.0.1", 0, dist_dir=dist, token="legacy",
                          config_path=tmp / "config.json", auth=auth)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()

    checks: dict[str, bool] = {}
    try:
        admin_cookie = _token(httpd, ADMIN, ADMIN_PW)
        user_cookie = _token(httpd, USER, USER_PW)
        print(f"[setup] admin cookie={'ok' if admin_cookie else 'MISSING'} "
              f"user cookie={'ok' if user_cookie else 'MISSING'}")

        # 1) 恢复入口：普通用户必须 403
        status, text = _call(httpd, "POST", "/api/wps_recovery_resolve",
                             body=[{"operation_id": "wps-0123456789abcdef",
                                    "decision": "retire_guarded"}], cookie=user_cookie)
        print(f"[1] 普通用户 wps_recovery_resolve -> HTTP {status} body={text[:120]}")
        checks["recovery_forbidden_for_non_admin"] = status == 403

        # 2) 管理员坏请求：必须结构化拒绝，不能 500
        status, text = _call(httpd, "POST", "/api/wps_recovery_resolve",
                             body=[{"operation_id": "bad-id", "decision": "retire_guarded",
                                    "confirm": "retire_guarded", "note": "xxxx",
                                    "confirm_structure_checked": True}], cookie=admin_cookie)
        payload = _json(text)
        print(f"[2] 管理员非法 operation_id -> HTTP {status} ok={payload.get('ok')} "
              f"code={payload.get('code')}")
        checks["admin_bad_request_structured"] = status == 200 and payload.get("ok") is False

        # 3) WPS 禁用：HTTP 层必须拒绝预览与上传
        httpd.bridge._config.wps_enabled = False
        called = {"n": 0}
        real_cli = httpd.bridge._wps_cli

        def counting_cli():
            called["n"] += 1
            return real_cli()
        httpd.bridge._wps_cli = counting_cli
        s_prev, t_prev = _call(httpd, "POST", "/api/wps_preview", body=[], cookie=user_cookie)
        p_prev = _json(t_prev)
        s_up, t_up = _call(httpd, "POST", "/api/wps_upload",
                           body=[{"preview_id": "pv-x"}], cookie=user_cookie)
        p_up = _json(t_up)
        print(f"[3] wps_enabled=False: preview HTTP {s_prev} ok={p_prev.get('ok')} "
              f"code={p_prev.get('code')} | upload HTTP {s_up} ok={p_up.get('ok')} "
              f"code={p_up.get('code')} | kdocs-cli 调用次数={called['n']}")
        disabled_ok = (p_prev.get("ok") is False and p_prev.get("code") == "wps_disabled"
                       and p_up.get("ok") is False and p_up.get("code") == "wps_disabled"
                       and called["n"] == 0)
        checks["wps_disabled_enforced_over_http"] = disabled_ok
        httpd.bridge._config.wps_enabled = True

        # 4) 危险并发：占住互斥槽位后，只读云入口必须被拒且不触达 kdocs-cli
        reservation = httpd.bridge._operations.try_reserve(
            "wps_upload", summary={"test": "hold"}, next_action="hold")
        print(f"[4] 占位: granted={reservation.granted}")
        called["n"] = 0
        results = {}
        for method, body in (("wps_preview", []), ("wps_check_copies", []),
                             ("sss_day_orders", [])):
            status, text = _call(httpd, "POST", f"/api/{method}", body=body,
                                 cookie=user_cookie)
            payload = _json(text)
            results[method] = (status, payload.get("ok"), payload.get("code")
                               or payload.get("reason"))
            print(f"    并发 {method}: HTTP {status} ok={payload.get('ok')} "
                  f"code/reason={results[method][2]}")
        print(f"    kdocs-cli 调用次数={called['n']}（应 0）")
        conflict_ok = all(v[1] is False for v in results.values()) and called["n"] == 0
        checks["cloud_read_endpoints_mutually_excluded"] = conflict_ok
        if reservation.operation is not None:
            httpd.bridge._operations.finish(reservation.operation, status="success")
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)

    print("\n===== 汇总 =====")
    for key, value in checks.items():
        print(f"  {key}: {'OK' if value else 'FAIL'}")
    if args.mutate_open_gates:
        print("  [mutation] 期望 cloud_read_endpoints_mutually_excluded=FAIL")
        return 0 if checks.get("cloud_read_endpoints_mutually_excluded") is False else 1
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
