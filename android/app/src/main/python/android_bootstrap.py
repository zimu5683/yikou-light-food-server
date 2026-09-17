"""Chaquopy 启动入口：把 Python 后端跑在 Android App 进程内。

Kotlin ``PythonRuntime`` 的调用顺序固定为：

1. ``configure(filesDir, distDir, cacheDir)``：设置 ``YIKOU_*`` / ``HOME`` /
   ``XDG_CONFIG_HOME`` / ``TMPDIR``，这些变量必须在 import ``app`` 之前写好；
2. ``start_http_server(port=0)``：在 ``127.0.0.1`` 随机端口启动现有
   :mod:`app.web.server`，返回 WebView 需要的 URL/token；
3. 任务由线程内的现有业务代码执行；前台 Service 负责进程优先级。

本文件只做「进程内引导」，不复制 ``app/`` 下任何业务逻辑。
"""
from __future__ import annotations

import json
import os
import sys
import threading
import traceback
from pathlib import Path
from typing import Any

_SERVER_LOCK = threading.RLock()
_STATE: dict[str, Any] = {
    "httpd": None,
    "thread": None,
    "token": "",
    "host": "",
    "port": 0,
    "files_dir": "",
    "dist_dir": "",
}


def _json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _ensure_list(values: Any) -> list[str]:
    if isinstance(values, str):
        values = [values]
    return [str(item) for item in (values or []) if str(item or "").strip()]


def configure(files_dir: str, dist_dir: str = "", cache_dir: str = "",
              extra_python_paths: Any = None) -> str:
    """配置 app 私有目录环境变量；幂等，可被 Kotlin 每次启动调用。

    返回 JSON（不含任何凭据），Kotlin 只需在失败时把 ``message`` 显示在诊断页。
    """
    root = Path(str(files_dir)).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    dist = Path(str(dist_dir)).expanduser() if dist_dir else root / "dist"
    cache = Path(str(cache_dir)).expanduser() if cache_dir else root / "cache"

    # 注意顺序：YIKOU_DATA_DIR 必须优先于 XDG_CONFIG_HOME；两者都指向
    # filesDir 内部，卸载即清除，不会进入 Android 自动备份（allowBackup=false）。
    env = {
        "YIKOU_APP_MODE": "android",
        "YIKOU_FILES_DIR": str(root),
        "YIKOU_DATA_DIR": str(root / "config"),
        "YIKOU_DIST_DIR": str(dist),
        "YIKOU_CACHE_DIR": str(cache),
        "HOME": str(root / "home"),
        "XDG_CONFIG_HOME": str(root / "config"),
        "TMPDIR": str(root / "tmp"),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
    }
    for key, value in env.items():
        os.environ[key] = value
    for directory in (dist, cache, root / "home", root / "config", root / "tmp"):
        directory.mkdir(parents=True, exist_ok=True)

    # Chaquopy 把 src/main/python 与仓库根都放进 sys.path；这里只补充 Kotlin
    # 指定的额外路径（例如解压后的 runtime 目录），并从末尾去重。
    for path in _ensure_list(extra_python_paths):
        if path not in sys.path:
            sys.path.append(path)

    _STATE["files_dir"] = str(root)
    _STATE["dist_dir"] = str(dist)
    return _json({
        "ok": True,
        "filesDir": str(root),
        "dataDir": str(root / "config"),
        "distDir": str(dist),
        "tmpDir": str(root / "tmp"),
    })


def start_http_server(port: int = 0, host: str = "127.0.0.1") -> str:
    """启动本地 HTTP 服务并返回 ``{url, token, port}``；重复调用返回既有实例。"""
    with _SERVER_LOCK:
        existing = _STATE.get("httpd")
        if existing is not None:
            return _json(_server_info())

        from app.web.server import create_server, load_or_create_token

        token = str(_STATE.get("token") or load_or_create_token())
        httpd = create_server(host, int(port), token=token)
        _STATE["httpd"] = httpd
        _STATE["token"] = token
        _STATE["host"] = host
        _STATE["port"] = int(httpd.server_address[1])
        thread = threading.Thread(
            target=httpd.serve_forever,
            kwargs={"poll_interval": 0.4},
            name="yikou-http",
            daemon=True,
        )
        _STATE["thread"] = thread
        thread.start()
        return _json(_server_info())


def stop_http_server() -> str:
    """停止 HTTP 服务（Activity 真正退出 / 测试清理时调用）。"""
    with _SERVER_LOCK:
        httpd = _STATE.pop("httpd", None)
        thread = _STATE.pop("thread", None)
        if httpd is not None:
            try:
                httpd.shutdown()
            finally:
                httpd.server_close()
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)
        _STATE["token"] = ""
        _STATE["port"] = 0
    return _json({"ok": True})


def _server_info() -> dict[str, Any]:
    host = _STATE.get("host") or "127.0.0.1"
    port = int(_STATE.get("port") or 0)
    token = str(_STATE.get("token") or "")
    return {
        "ok": bool(port),
        "host": host,
        "port": port,
        "token": token,
        # WebView 首次加载直接使用带 token 的 URL；token 只在本机进程间传递。
        "url": f"http://{host}:{port}/?token={token}" if port else "",
    }


def diagnostics() -> str:
    """返回环境与目录诊断（不含 token/密码），失败也不抛异常。"""
    result: dict[str, Any] = {
        "ok": True,
        "python": sys.version,
        "executable": sys.executable,
        "cwd": os.getcwd(),
        "appMode": os.environ.get("YIKOU_APP_MODE", ""),
        "filesDir": os.environ.get("YIKOU_FILES_DIR", ""),
        "dataDir": os.environ.get("YIKOU_DATA_DIR", ""),
        "distDir": os.environ.get("YIKOU_DIST_DIR", ""),
        "distReady": False,
        "serverRunning": _STATE.get("httpd") is not None,
        "serverPort": int(_STATE.get("port") or 0),
        "imports": {},
    }
    dist = Path(os.environ.get("YIKOU_DIST_DIR", ""))
    result["distReady"] = bool(dist and (dist / "index.html").is_file())
    import importlib

    for module in ("openpyxl", "requests", "keyring", "cryptography",
                   "app.order.runner", "app.wps.sync", "app.web.server"):
        try:
            importlib.import_module(module)
        except Exception as exc:  # noqa: BLE001
            result["imports"][module] = f"{type(exc).__name__}: {exc}"
        else:
            result["imports"][module] = "ok"
    return _json(result)


def self_check() -> str:
    """部署/升级后自检；不联网、不读取用户数据。"""
    result: dict[str, Any] = {"ok": True, "checks": {}}
    for name, fn in (
            ("configure", lambda: _STATE.get("files_dir")),
            ("imports", lambda: _import_business_modules()),
    ):
        try:
            value = fn()
        except Exception as exc:  # noqa: BLE001
            result["ok"] = False
            result["checks"][name] = f"{type(exc).__name__}: {exc}"
        else:
            result["checks"][name] = value or "ok"
    return _json(result)


def _import_business_modules() -> str:
    import importlib

    modules = (
        "app.order.runner",
        "app.order.templates",
        "app.api.bridge",
        "app.ordering.sss",
        "app.ordering.cloud_import",
        "app.wps.sync",
        "app.web.auth",
        "app.web.server",
    )
    for module in modules:
        importlib.import_module(module)
    return f"{len(modules)} modules"


def main() -> int:
    """便于开发者用 ``python android_bootstrap.py --start <filesDir>`` 手工验证。"""
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--start", metavar="FILES_DIR", default="")
    args = parser.parse_args()
    if not args.start:
        print(diagnostics())
        return 0
    print(configure(args.start))
    print(start_http_server(0), flush=True)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
    finally:
        stop_http_server()
    return 0


def _unexpected_error() -> str:  # pragma: no cover - 仅人工排障
    return _json({"ok": False, "message": traceback.format_exc()})


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
