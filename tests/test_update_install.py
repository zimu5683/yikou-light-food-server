"""应用内更新：Release 资产选择、下载校验、Bridge 安装编排。"""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from app.api import bridge as bridge_module
from app.api.bridge import Bridge
from app.core import update as update_mod
from app.core.update import (
    ReleaseAsset,
    ReleaseCheckError,
    ReleaseInfo,
    UpdateCancelled,
    download_asset,
    parse_sha256,
    select_android_apk,
    select_sha256_asset,
)


def _release() -> ReleaseInfo:
    return ReleaseInfo(
        tag_name="v3.6.5",
        body="new",
        html_url="https://example.com/release",
        assets=(
            ReleaseAsset("notes.txt", "https://example.com/notes.txt", 10),
            ReleaseAsset("yikou-3.6.5-arm64.apk",
                         "https://example.com/yikou.apk", 1234),
            ReleaseAsset("yikou-3.6.5-arm64.apk.sha256",
                         "https://example.com/yikou.apk.sha256", 64),
        ),
    )


def test_select_android_apk_ignores_sha256_and_notes():
    apk = select_android_apk(_release())
    assert apk is not None
    assert apk.name == "yikou-3.6.5-arm64.apk"
    sha = select_sha256_asset(_release(), apk)
    assert sha is not None
    assert sha.name.endswith(".apk.sha256")


def test_select_android_apk_returns_none_when_only_web_assets():
    release = ReleaseInfo(tag_name="v3.6.5",
                          assets=(ReleaseAsset("source.zip", "https://example.com/s.zip"),))
    assert select_android_apk(release) is None


def test_parse_sha256_accepts_hash_with_filename():
    digest = "a" * 64
    assert parse_sha256(f"{digest}  yikou.apk\n") == digest


def test_parse_sha256_rejects_garbage():
    with pytest.raises(ReleaseCheckError):
        parse_sha256("not a hash")


class _FakeResponse:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks)
        self.headers = {"Content-Length": str(sum(len(c) for c in chunks))}

    def read(self, _size: int = -1) -> bytes:
        if not self._chunks:
            return b""
        return self._chunks.pop(0)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _patch_urlopen(monkeypatch, chunks: list[bytes]):
    def fake_urlopen(_request, timeout=0):
        return _FakeResponse(chunks)

    monkeypatch.setattr(update_mod, "urlopen", fake_urlopen)


def test_download_asset_verifies_sha256_and_progress(monkeypatch, tmp_path):
    payload = b"fake apk" * 100
    digest = hashlib.sha256(payload).hexdigest()
    _patch_urlopen(monkeypatch, [payload[i:i + 64] for i in range(0, len(payload), 64)])
    progress: list[tuple[int, int]] = []
    dest = tmp_path / "updates" / "a.apk"
    result = download_asset(
        ReleaseAsset("a.apk", "https://example.com/a.apk", len(payload)),
        dest,
        expected_sha256=digest,
        expected_size=len(payload),
        on_progress=lambda done, total: progress.append((done, total)),
    )
    assert result.read_bytes() == payload
    assert progress[-1][0] == len(payload)
    assert not (tmp_path / "updates" / "a.apk.part").exists()


def test_download_asset_removes_part_on_hash_mismatch(monkeypatch, tmp_path):
    payload = b"fake apk"
    _patch_urlopen(monkeypatch, [payload])
    dest = tmp_path / "a.apk"
    with pytest.raises(ReleaseCheckError, match="SHA-256"):
        download_asset(
            ReleaseAsset("a.apk", "https://example.com/a.apk", len(payload)),
            dest,
            expected_sha256="0" * 64,
        )
    assert not dest.exists()
    assert not (tmp_path / "a.apk.part").exists()


def test_download_asset_can_be_cancelled(monkeypatch, tmp_path):
    payload = b"fake apk"
    _patch_urlopen(monkeypatch, [payload])
    dest = tmp_path / "a.apk"
    with pytest.raises(UpdateCancelled):
        download_asset(
            ReleaseAsset("a.apk", "https://example.com/a.apk", len(payload)),
            dest,
            cancel_check=lambda: True,
        )
    assert not dest.exists()
    assert not (tmp_path / "a.apk.part").exists()


def _bridge(tmp_path) -> Bridge:
    return Bridge(config_path=str(tmp_path / "config.json"), is_admin=True)


def test_install_update_rejects_non_android(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge_module.android_runtime, "is_android", lambda: False)
    result = _bridge(tmp_path).install_update()
    assert result["reason"] == "unsupported_platform"


def test_install_update_rejects_running_task(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge_module.android_runtime, "is_android", lambda: True)
    bridge = _bridge(tmp_path)
    monkeypatch.setattr(bridge, "worker_alive", lambda: True)
    result = bridge.install_update()
    assert result["reason"] == "busy"


def test_install_update_requires_unknown_sources_permission(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge_module.android_runtime, "is_android", lambda: True)
    monkeypatch.setattr(bridge_module.android_runtime, "update_capabilities",
                        lambda: {"ok": True, "canInstall": False,
                                 "versionName": "3.6.4", "versionCode": 30604})
    bridge = _bridge(tmp_path)
    monkeypatch.setattr(bridge, "worker_alive", lambda: False)
    result = bridge.install_update()
    assert result["reason"] == "permission_required"


def test_install_worker_happy_path_emits_progress_and_does_not_open_browser(
        tmp_path, monkeypatch):
    release = _release()
    manifest = f"{'b' * 64}  yikou.apk\n"
    calls: list[str] = []

    monkeypatch.setattr(bridge_module, "check_for_update", lambda **kwargs: release)
    monkeypatch.setattr(bridge_module.android_runtime, "cache_dir",
                        lambda: tmp_path / "cache")
    monkeypatch.setattr(bridge_module.android_runtime, "verify_update_apk",
                        lambda path: {"ok": True, "sameSignature": True,
                                      "versionCode": 30605, "versionName": "3.6.5"})
    monkeypatch.setattr(bridge_module.android_runtime, "install_apk",
                        lambda path: calls.append(path) or {"ok": True, "message": "已打开"})

    def fake_read_asset(_asset, **kwargs):
        return manifest

    def fake_download(asset, dest, **kwargs):
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        Path(dest).write_bytes(b"apk")
        on_progress = kwargs.get("on_progress")
        if on_progress:
            on_progress(3, 3)
        return Path(dest)

    monkeypatch.setattr(bridge_module, "read_asset_text", fake_read_asset)
    monkeypatch.setattr(bridge_module, "download_asset", fake_download)

    bridge = _bridge(tmp_path)
    bridge._update_installing = True
    bridge._install_update_worker({"versionName": "3.6.4", "versionCode": 30604})

    events = [item["event"] for item in bridge.drain_events(0)["events"]]
    phases = [item["payload"]["phase"] for item in bridge.drain_events(0)["events"]
              if item["event"] == "update:progress"]
    assert "update:progress" in events
    assert "update:error" not in events
    assert "installing" in phases
    assert calls and calls[0].endswith("yikou-3.6.5-arm64.apk")
    assert bridge._update_installing is False


def test_install_worker_permission_required_stops_before_install(tmp_path, monkeypatch):
    release = _release()
    monkeypatch.setattr(bridge_module, "check_for_update", lambda **kwargs: release)
    monkeypatch.setattr(bridge_module.android_runtime, "cache_dir",
                        lambda: tmp_path / "cache")
    monkeypatch.setattr(bridge_module, "read_asset_text", lambda *a, **k: f"{'c' * 64}\n")
    monkeypatch.setattr(bridge_module.android_runtime, "verify_update_apk",
                        lambda path: {"ok": True, "sameSignature": True,
                                      "versionCode": 30605})
    monkeypatch.setattr(bridge_module.android_runtime, "install_apk",
                        lambda path: {"ok": False, "code": "permission_required",
                                      "message": "请先授权"})

    def fake_download(asset, dest, **kwargs):
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        Path(dest).write_bytes(b"apk")
        return Path(dest)

    monkeypatch.setattr(bridge_module, "download_asset", fake_download)
    bridge = _bridge(tmp_path)
    bridge._update_installing = True
    bridge._install_update_worker({"versionName": "3.6.4", "versionCode": 30604})

    events = bridge.drain_events(0)["events"]
    assert any(item["event"] == "update:permission_required" for item in events)
    assert not any(item["event"] == "update:error" for item in events)


def test_install_worker_blocks_signature_mismatch(tmp_path, monkeypatch):
    release = _release()
    monkeypatch.setattr(bridge_module, "check_for_update", lambda **kwargs: release)
    monkeypatch.setattr(bridge_module.android_runtime, "cache_dir",
                        lambda: tmp_path / "cache")
    monkeypatch.setattr(bridge_module, "read_asset_text", lambda *a, **k: f"{'d' * 64}\n")
    monkeypatch.setattr(bridge_module.android_runtime, "verify_update_apk",
                        lambda path: {"ok": True, "sameSignature": False,
                                      "versionCode": 30605})

    def fake_download(asset, dest, **kwargs):
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        Path(dest).write_bytes(b"apk")
        return Path(dest)

    monkeypatch.setattr(bridge_module, "download_asset", fake_download)
    bridge = _bridge(tmp_path)
    bridge._update_installing = True
    bridge._install_update_worker({"versionName": "3.6.4", "versionCode": 30604})

    events = bridge.drain_events(0)["events"]
    errors = [item for item in events if item["event"] == "update:error"]
    assert errors and errors[0]["payload"]["code"] == "signature_mismatch"
