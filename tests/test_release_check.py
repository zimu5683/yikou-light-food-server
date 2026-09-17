"""更新检查：网页版自己的版本轨道 vs 桌面版提示轨道。"""
from __future__ import annotations


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


def test_release_urls_point_to_two_distinct_repositories():
    assert update_mod.WEB_REPOSITORY != update_mod.DESKTOP_REPOSITORY
    assert update_mod.releases_url(update_mod.WEB_REPOSITORY).endswith(
        "/zimu5683/yikou-light-food-server/releases/latest")
    assert update_mod.releases_url(update_mod.DESKTOP_REPOSITORY).endswith(
        "/zimu5683/yikou-light-food-desktop/releases/latest")


def test_remote_older_version_is_not_an_update_and_not_an_error(monkeypatch):
    """网页仓库的 Release 版本如果比当前代码还旧，忽略即可，不应弹错误。"""
    monkeypatch.setattr(update_mod, "fetch_latest_release",
                        lambda **kwargs: ReleaseInfo(tag_name="v3.2.1",
                                                     repository=update_mod.WEB_REPOSITORY))
    assert check_for_update(current_version="3.5.0") is None


def test_remote_newer_version_is_an_update(monkeypatch):
    monkeypatch.setattr(update_mod, "fetch_latest_release",
                        lambda **kwargs: ReleaseInfo(tag_name="v3.6.0", body="new",
                                                     repository=update_mod.WEB_REPOSITORY))
    release = check_for_update(current_version="3.5.0")
    assert release is not None
    assert release.version == "3.6.0"


def test_check_for_update_can_query_desktop_repository(monkeypatch):
    seen: list[str] = []

    def fake_fetch(*, repository=update_mod.WEB_REPOSITORY, **kwargs):
        seen.append(repository)
        return ReleaseInfo(tag_name="v9.9.9", repository=repository)

    monkeypatch.setattr(update_mod, "fetch_latest_release", fake_fetch)
    release = check_for_update(current_version="3.5.0",
                               repository=update_mod.DESKTOP_REPOSITORY)
    assert release is not None
    assert seen == [update_mod.DESKTOP_REPOSITORY]


def _bridge(tmp_path) -> Bridge:
    return Bridge(config_path=str(tmp_path / "config.json"), is_admin=True)


def test_worker_emits_web_and_desktop_events_separately(tmp_path, monkeypatch):
    def fake_check(current_version=__version__, *, repository=update_mod.WEB_REPOSITORY, **kwargs):
        if repository == update_mod.DESKTOP_REPOSITORY:
            return ReleaseInfo(tag_name="v9.9.9", body="desktop",
                               html_url="https://example.com/desktop",
                               repository=repository)
        if repository == update_mod.WEB_REPOSITORY:
            return ReleaseInfo(tag_name="v3.6.0", body="web",
                               html_url="https://example.com/web",
                               repository=repository)
        return None

    monkeypatch.setattr(bridge_module, "check_for_update", fake_check)
    bridge = _bridge(tmp_path)
    bridge._check_updates_worker(manual=True)

    events = bridge.drain_events(0)["events"]
    by_name = {event["event"]: event["payload"] for event in events}
    assert by_name["update:available"]["tag"] == "v3.6.0"
    assert by_name["update:available"]["html_url"] == "https://example.com/web"
    assert by_name["desktop_update:available"]["tag"] == "v9.9.9"
    assert by_name["desktop_update:available"]["html_url"] == "https://example.com/desktop"


def test_worker_logs_desktop_check_failure_without_breaking_web_update(tmp_path, monkeypatch):
    def fake_check(current_version=__version__, *, repository=update_mod.WEB_REPOSITORY, **kwargs):
        if repository == update_mod.DESKTOP_REPOSITORY:
            raise ReleaseCheckError("desktop network down")
        return None

    monkeypatch.setattr(bridge_module, "check_for_update", fake_check)
    bridge = _bridge(tmp_path)
    bridge._check_updates_worker(manual=False)

    events = bridge.drain_events(0)["events"]
    names = [event["event"] for event in events]
    assert "update:latest" not in names  # manual=False 时只写日志
    logs = [event["payload"]["msg"] for event in events if event["event"] == "log"]
    assert any("桌面版更新检查失败" in line for line in logs)
    assert bridge.status == "ready"
