"""更新检查：单一发布仓库（应用自己的 Release）。

桌面版更新轨道已删除：``check_for_update`` / ``fetch_latest_release`` 不再接受
``repository`` 参数，Bridge 也只发 ``update:available`` / ``update:latest``，
不再有 ``desktop_update:available``。
"""
from __future__ import annotations

import inspect

from app import __version__
from app.api import bridge as bridge_module
from app.api.bridge import Bridge
from app.core import update as update_mod
from app.core.update import ReleaseInfo, ReleaseCheckError, check_for_update


def test_compare_versions_basic():
    assert update_mod.compare_versions("3.6.0", "3.5.0") > 0
    assert update_mod.compare_versions("3.5.0", "3.5.0") == 0
    assert update_mod.compare_versions("3.4.9", "3.5.0") < 0
    assert update_mod.compare_versions("not-semver", "3.5.0") == -1


def test_compare_versions_accepts_four_segment_hotfix():
    assert update_mod.compare_versions("3.6.6.1", "3.6.6") > 0
    assert update_mod.compare_versions("3.6.6", "3.6.6.0") == 0
    assert update_mod.compare_versions("3.6.6.1", "3.6.6.2") < 0
    assert update_mod.compare_versions("v3.6.6.1", "3.6.6.1") == 0


def test_release_url_points_to_the_single_app_repository():
    assert update_mod.WEB_REPOSITORY == "zimu5683/yikou-light-food-server"
    assert update_mod.REPOSITORY == update_mod.WEB_REPOSITORY
    assert update_mod.releases_url() == (
        "https://api.github.com/repos/zimu5683/yikou-light-food-server/releases/latest")
    # 桌面版轨道已删除：模块里不再有第二个仓库常量。
    assert not hasattr(update_mod, "DESKTOP_REPOSITORY")


def test_release_checkers_no_longer_take_a_repository_argument():
    """单轨道契约：没有 ``repository`` 参数，调用方无法指向别的仓库。"""
    for func in (update_mod.fetch_latest_release, check_for_update,
                 update_mod.releases_url):
        assert "repository" not in inspect.signature(func).parameters, func.__name__


def test_fetch_latest_release_queries_the_app_repository(monkeypatch):
    seen: list[str] = []

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self) -> bytes:
            return b'{"tag_name": "v9.9.9", "body": "note"}'

    def fake_urlopen(request, timeout=0.0):
        seen.append(request.full_url)
        return _Response()

    monkeypatch.setattr(update_mod, "urlopen", fake_urlopen)

    release = update_mod.fetch_latest_release()

    assert release.tag_name == "v9.9.9"
    assert release.repository == update_mod.WEB_REPOSITORY
    assert seen == [
        "https://api.github.com/repos/zimu5683/yikou-light-food-server/releases/latest"]


def test_remote_older_version_is_not_an_update_and_not_an_error(monkeypatch):
    """发布仓库的 Release 版本如果比当前代码还旧，忽略即可，不应弹错误。"""
    monkeypatch.setattr(update_mod, "fetch_latest_release",
                        lambda **kwargs: ReleaseInfo(tag_name="v3.2.1"))
    assert check_for_update(current_version="3.5.0") is None


def test_remote_same_version_is_not_an_update(monkeypatch):
    monkeypatch.setattr(update_mod, "fetch_latest_release",
                        lambda **kwargs: ReleaseInfo(tag_name="v3.5.0"))
    assert check_for_update(current_version="3.5.0") is None


def test_remote_newer_version_is_an_update(monkeypatch):
    monkeypatch.setattr(update_mod, "fetch_latest_release",
                        lambda **kwargs: ReleaseInfo(tag_name="v3.6.0", body="new"))
    release = check_for_update(current_version="3.5.0")
    assert release is not None
    assert release.version == "3.6.0"


def _bridge(tmp_path) -> Bridge:
    return Bridge(config_path=str(tmp_path / "config.json"), is_admin=True)


def test_worker_checks_the_release_exactly_once(tmp_path, monkeypatch):
    """只剩一条轨道：每次检查只查一次 Release，不会再去查桌面版仓库。"""
    calls: list[dict] = []

    def fake_check(**kwargs):
        calls.append(kwargs)
        return None

    monkeypatch.setattr(bridge_module, "check_for_update", fake_check)
    bridge = _bridge(tmp_path)
    bridge._check_updates_worker(manual=False)

    assert calls == [{"current_version": __version__}]
    names = [event["event"] for event in bridge.drain_events(0)["events"]]
    assert "desktop_update:available" not in names


def test_worker_emits_update_available_for_a_newer_release(tmp_path, monkeypatch):
    def fake_check(current_version=__version__, **kwargs):
        return ReleaseInfo(tag_name="v3.6.0", body="web",
                           html_url="https://example.com/web")

    monkeypatch.setattr(bridge_module, "check_for_update", fake_check)
    bridge = _bridge(tmp_path)
    bridge._check_updates_worker(manual=True)

    events = bridge.drain_events(0)["events"]
    by_name = {event["event"]: event["payload"] for event in events}
    assert by_name["update:available"]["tag"] == "v3.6.0"
    assert by_name["update:available"]["html_url"] == "https://example.com/web"
    assert "desktop_update:available" not in by_name


def test_worker_emits_latest_only_when_manual_and_up_to_date(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge_module, "check_for_update", lambda **kwargs: None)

    manual_bridge = _bridge(tmp_path / "manual")
    manual_bridge._check_updates_worker(manual=True)
    manual_names = [event["event"] for event in manual_bridge.drain_events(0)["events"]]
    assert "update:latest" in manual_names

    auto_bridge = _bridge(tmp_path / "auto")
    auto_bridge._check_updates_worker(manual=False)
    auto_events = auto_bridge.drain_events(0)["events"]
    assert "update:latest" not in [event["event"] for event in auto_events]
    logs = [event["payload"]["msg"] for event in auto_events if event["event"] == "log"]
    assert any("当前已是最新版本" in line for line in logs)


def test_worker_reports_check_failure_as_error(tmp_path, monkeypatch):
    def boom(**kwargs):
        raise ReleaseCheckError("network down")

    monkeypatch.setattr(bridge_module, "check_for_update", boom)
    bridge = _bridge(tmp_path)
    bridge._check_updates_worker(manual=True)

    events = bridge.drain_events(0)["events"]
    errors = [event["payload"] for event in events if event["event"] == "update:error"]
    assert errors and errors[0]["code"] == "check_failed"
    assert "network down" in errors[0]["message"]
    assert bridge.status == "error"
