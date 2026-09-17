"""``Bridge`` 杂项 js_api 回归锁：文件路径校验、外链白名单与更新检查。

网页版没有原生文件对话框，``choose_excel`` / ``new_template`` 改为只接受
服务器端文件浏览器回填的路径；这里的用例全部离线。
"""
from __future__ import annotations

import pytest

from app.api import bridge as bridge_module
from app.api.bridge import Bridge


def _bridge(tmp_path) -> Bridge:
    return Bridge(config_path=str(tmp_path / "config.json"), is_admin=True)


@pytest.fixture
def no_threads(monkeypatch):
    """拦住后台线程，只记录「有没有起线程」。"""
    started: list[dict] = []

    class _FakeThread:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.daemon = bool(kwargs.get("daemon"))

        def start(self):
            started.append(self.kwargs)

    monkeypatch.setattr(bridge_module.threading, "Thread", _FakeThread)
    return started


# ----------------------------------------------------------------------
# choose_excel：只校验服务端路径
# ----------------------------------------------------------------------
def test_choose_excel_accepts_an_existing_workbook(tmp_path):
    excel = tmp_path / "排单.xlsx"
    excel.write_bytes(b"x")

    got = _bridge(tmp_path).choose_excel("order", str(excel))

    assert got == {"path": str(excel), "error": ""}


def test_choose_excel_requires_a_path(tmp_path):
    got = _bridge(tmp_path).choose_excel("order")

    assert got["path"] == ""
    assert "文件浏览器" in got["error"]


def test_choose_excel_rejects_a_missing_file(tmp_path):
    got = _bridge(tmp_path).choose_excel("order", str(tmp_path / "不存在.xlsx"))

    assert got["path"].endswith("不存在.xlsx")
    assert got["error"] == "请选择存在的 Excel 文件"


def test_choose_excel_rejects_a_non_excel_file(tmp_path):
    other = tmp_path / "名单.csv"
    other.write_bytes(b"x")

    got = _bridge(tmp_path).choose_excel("order", str(other))

    assert got["path"] == str(other)
    assert "xlsx" in got["error"]


# ----------------------------------------------------------------------
# new_template：只写入服务端浏览器选中的位置
# ----------------------------------------------------------------------
def test_new_template_writes_the_selected_path(tmp_path, monkeypatch):
    written: list[tuple[str, str]] = []
    monkeypatch.setattr(bridge_module, "write_order_template",
                        lambda dest: written.append(("order", str(dest))))
    monkeypatch.setattr(bridge_module, "write_sss_template",
                        lambda dest: written.append(("sss", str(dest))))

    bridge = _bridge(tmp_path)
    assert bridge.new_template("order", str(tmp_path / "排单.xlsx"))["error"] == ""
    assert bridge.new_template("sss", str(tmp_path / "闪时送.xlsx"))["error"] == ""

    assert [kind for kind, _ in written] == ["order", "sss"]


def test_new_template_appends_xlsx_when_suffix_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge_module, "write_order_template", lambda dest: None)

    got = _bridge(tmp_path).new_template("order", str(tmp_path / "新表"))

    assert got["path"].endswith("新表.xlsx"), "没有 Excel 后缀时要补上"


def test_new_template_requires_a_path(tmp_path):
    got = _bridge(tmp_path).new_template("order")

    assert got["path"] == ""
    assert "文件浏览器" in got["error"]


def test_new_template_write_failure_is_reported(tmp_path, monkeypatch):
    def boom(_dest):
        raise OSError("磁盘只读")

    monkeypatch.setattr(bridge_module, "write_order_template", boom)

    got = _bridge(tmp_path).new_template("order", str(tmp_path / "排单.xlsx"))

    assert got["path"] == ""
    assert "无法写入模板文件" in got["error"] and "磁盘只读" in got["error"]


def test_new_template_logs_the_generated_path(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge_module, "write_sss_template", lambda dest: None)

    bridge = _bridge(tmp_path)
    bridge.new_template("sss", str(tmp_path / "闪时送.xlsx"))

    logs = [e["payload"]["msg"] for e in bridge.drain_events(0)["events"]
            if e["event"] == "log"]
    assert any("已生成闪时送模板" in line for line in logs)


# ----------------------------------------------------------------------
# open_external：只放行 http(s)
# ----------------------------------------------------------------------
@pytest.mark.parametrize("url", ["https://github.com/x", "http://example.com/a?b=c"])
def test_open_external_opens_http_and_https(tmp_path, monkeypatch, url):
    opened: list[str] = []
    monkeypatch.setattr(bridge_module.webbrowser, "open", opened.append)

    assert _bridge(tmp_path).open_external(url) == {"ok": True}

    assert opened == [url]


@pytest.mark.parametrize("url", [
    "file:///etc/passwd", "javascript:alert(1)", "ftp://x/y",
    "mailto:a@b.c", "", "github.com", "  https://x  ",
])
def test_open_external_ignores_anything_else(tmp_path, monkeypatch, url):
    """白名单之外的协议一律不交给系统打开（每次都返回 ok，不报错也不执行）。"""
    opened: list[str] = []
    monkeypatch.setattr(bridge_module.webbrowser, "open", opened.append)

    assert _bridge(tmp_path).open_external(url) == {"ok": True}

    assert opened == [], f"{url!r} 不该被打开"


def test_open_external_tolerates_non_string(tmp_path, monkeypatch):
    opened: list[str] = []
    monkeypatch.setattr(bridge_module.webbrowser, "open", opened.append)
    assert _bridge(tmp_path).open_external(None) == {"ok": True}  # type: ignore[arg-type]
    assert opened == []


# ----------------------------------------------------------------------
# check_updates：后台线程 + 防重入
# ----------------------------------------------------------------------
def test_check_updates_starts_a_worker_with_the_manual_flag(tmp_path, no_threads):
    bridge = _bridge(tmp_path)

    assert bridge.check_updates(manual=True) == {"ok": True}

    assert bridge.status == "updating"
    assert no_threads[0]["args"] == (True,)


def test_check_updates_refuses_when_already_checking(tmp_path, no_threads):
    """防重入：正在检查时再点必须被挡掉，而不是起第二个线程。"""
    bridge = _bridge(tmp_path)
    bridge.check_updates()

    got = bridge.check_updates()

    assert got == {"ok": False, "reason": "already_checking"}
    assert len(no_threads) == 1, "不该起第二个线程"


def test_check_updates_defaults_to_not_manual(tmp_path, no_threads):
    bridge = _bridge(tmp_path)
    bridge.check_updates()
    assert no_threads[0]["args"] == (False,)
