"""``android_bootstrap.py`` 的引导/自检回归锁。

这个文件不在 ``app/`` 包内，测试用 ``importlib`` 从 Android 源集加载，防止
Chaquopy 启动脚本被误删或环境变量设置顺序被改坏（例如先 import app 再设
``YIKOU_DATA_DIR``，会让用户配置落到错误目录）。
"""
from __future__ import annotations

import importlib.util
import json
import sys
import os
import urllib.request
from pathlib import Path
from unittest import mock

BOOTSTRAP_PATH = (Path(__file__).resolve().parent.parent / "android" / "app"
                  / "src" / "main" / "python" / "android_bootstrap.py")


def _load_bootstrap():
    spec = importlib.util.spec_from_file_location("android_bootstrap_under_test",
                                                  BOOTSTRAP_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(spec.name, None)
        raise
    return module


def test_configure_sets_private_directories_before_app_import(tmp_path):
    module = _load_bootstrap()
    files_dir = tmp_path / "files"
    dist_dir = tmp_path / "files" / "dist"
    dist_dir.mkdir(parents=True)
    (dist_dir / "index.html").write_text("<html>ok</html>", encoding="utf-8")

    # patch.dict 整体快照并恢复 os.environ，因为 configure() 会直接 setenv；
    # pytest.MonkeyPatch.delenv 不会撤销之后的直接写值。
    with mock.patch.dict(os.environ, {}, clear=False):
        for key in ("YIKOU_APP_MODE", "YIKOU_FILES_DIR", "YIKOU_DATA_DIR",
                    "YIKOU_DIST_DIR", "YIKOU_CACHE_DIR", "HOME", "XDG_CONFIG_HOME",
                    "TMPDIR", "LANG", "LC_ALL"):
            os.environ.pop(key, None)

        payload = json.loads(module.configure(
            str(files_dir), dist_dir=str(dist_dir), cache_dir=str(files_dir / "cache")))

        assert payload["ok"] is True
        assert os.environ["YIKOU_APP_MODE"] == "android"
        assert os.environ["YIKOU_DATA_DIR"] == str(files_dir / "config")
        assert os.environ["YIKOU_DIST_DIR"] == str(dist_dir)
        assert os.environ["HOME"] == str(files_dir / "home")
        assert os.environ["XDG_CONFIG_HOME"] == str(files_dir / "config")
        assert os.environ["TMPDIR"] == str(files_dir / "tmp")
        assert (files_dir / "config").is_dir()
        assert (files_dir / "tmp").is_dir()

        from app.core.config import user_data_dir

        assert user_data_dir() == files_dir / "config"


def test_start_and_stop_http_server_returns_webview_url(tmp_path):
    module = _load_bootstrap()
    files_dir = tmp_path / "files"
    dist_dir = files_dir / "dist"
    dist_dir.mkdir(parents=True)
    (dist_dir / "index.html").write_text("<html>app</html>", encoding="utf-8")

    # noqa: SIM117 - 两段式 with 让恢复范围明确
    with mock.patch.dict(os.environ, {}, clear=False):
        for key in ("YIKOU_APP_MODE", "YIKOU_FILES_DIR", "YIKOU_DATA_DIR",
                    "YIKOU_DIST_DIR", "YIKOU_CACHE_DIR", "HOME", "XDG_CONFIG_HOME",
                    "TMPDIR"):
            os.environ.pop(key, None)
        module.configure(str(files_dir), dist_dir=str(dist_dir))
        info = json.loads(module.start_http_server(0))

        assert info["ok"] is True
        assert info["host"] == "127.0.0.1"
        assert info["port"] > 0
        assert info["token"]
        assert info["url"].startswith(f"http://127.0.0.1:{info['port']}/?token=")

        with urllib.request.urlopen(
                f"http://127.0.0.1:{info['port']}/healthz", timeout=5) as response:
            assert response.status == 200
            assert json.loads(response.read().decode("utf-8")) == {"ok": True}

        # 重复调用必须返回同一个服务，而不是第二次绑定端口。
        again = json.loads(module.start_http_server(0))
        assert again["port"] == info["port"]

        stopped = json.loads(module.stop_http_server())
        assert stopped["ok"] is True
        assert json.loads(module.stop_http_server())["ok"] is True


def test_diagnostics_does_not_leak_token(tmp_path):
    module = _load_bootstrap()
    files_dir = tmp_path / "files"
    dist_dir = files_dir / "dist"
    dist_dir.mkdir(parents=True)
    (dist_dir / "index.html").write_text("<html>app</html>", encoding="utf-8")

    # noqa: SIM117 - 两段式 with 让恢复范围明确
    with mock.patch.dict(os.environ, {}, clear=False):
        for key in ("YIKOU_APP_MODE", "YIKOU_FILES_DIR", "YIKOU_DATA_DIR",
                    "YIKOU_DIST_DIR", "YIKOU_CACHE_DIR", "HOME", "XDG_CONFIG_HOME",
                    "TMPDIR"):
            os.environ.pop(key, None)
        module.configure(str(files_dir), dist_dir=str(dist_dir))
        info = json.loads(module.start_http_server(0))
        try:
            text = module.diagnostics()
            assert info["token"] not in text
            data = json.loads(text)
            assert data["ok"] is True
            assert data["distReady"] is True
            assert data["imports"]["app.web.server"] == "ok"
        finally:
            module.stop_http_server()
