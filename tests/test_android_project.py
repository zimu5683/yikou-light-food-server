"""Android APK 工程的结构/版本回归，防止 IDE 或重构把关键文件删掉。

这些不是 Android 编译测试；真正的 Kotlin 编译与真机验证在
``.github/workflows/android.yml`` / ``./gradlew`` 完成。本文件负责钉死
「APK 版本与 app.__version__ 一致」「隐私/前台服务等 manifest 关键属性」。
"""
from __future__ import annotations

from pathlib import Path

from app import __version__

ROOT = Path(__file__).resolve().parent.parent
ANDROID = ROOT / "android"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _version_properties() -> dict[str, str]:
    payload: dict[str, str] = {}
    for line in _read(ANDROID / "version.properties").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        payload[key.strip()] = value.strip()
    return payload


def test_apk_version_matches_python_version():
    props = _version_properties()
    assert props["versionName"] == __version__, (
        "android/version.properties 与 app/__init__.py 版本不一致；"
        "发版时必须一起改，否则应用内更新会误判")
    parts = [int(part) for part in __version__.split(".")]
    assert len(parts) in (3, 4), f"版本号必须是 x.y.z 或 x.y.z.w：{__version__}"
    major, minor, patch = parts[0], parts[1], parts[2]
    build = parts[3] if len(parts) == 4 else 0
    assert int(props["versionCode"]) == major * 1_000_000 + minor * 10_000 + patch * 100 + build


def test_gradle_targets_only_arm64_and_python_313():
    build = _read(ANDROID / "app" / "build.gradle.kts")
    assert 'abiFilters.add("arm64-v8a")' in build
    assert 'version = "3.13"' in build
    assert "targetSdk = overrideTargetSdk ?: 35" in build
    assert 'install("openpyxl==3.1.5")' in build
    assert 'install("requests==2.34.2")' in build
    assert 'install("cryptography==42.0.8")' in build


def test_manifest_keeps_private_data_and_foreground_service_contract():
    manifest = _read(ANDROID / "app" / "src" / "main" / "AndroidManifest.xml")
    assert 'android:allowBackup="false"' in manifest
    assert 'android:foregroundServiceType="dataSync"' in manifest
    assert 'android.permission.MANAGE_EXTERNAL_STORAGE' in manifest
    assert 'android.permission.POST_NOTIFICATIONS' in manifest
    assert 'android.permission.REQUEST_INSTALL_PACKAGES' in manifest
    assert 'android:usesCleartextTraffic="false"' in manifest
    # FileProvider 的 paths 配置必须是 android:resource，否则安装更新时 Uri 拿不到文件。
    assert 'android:resource="@xml/file_paths"' in manifest


def test_network_security_allows_only_local_http():
    config = _read(ANDROID / "app" / "src" / "main" / "res" / "xml"
                   / "network_security_config.xml")
    assert 'cleartextTrafficPermitted="false"' in config
    assert "127.0.0.1" in config
    assert "localhost" in config


def test_frozen_wpsruntime_json_interface_exists():
    runtime = _read(ANDROID / "app" / "src" / "main" / "java" / "com"
                    / "yikou" / "lightfood" / "WpsRuntime.kt")
    for method in ("initialize", "runJson", "authStatusJson", "beginAuthorizeJson",
                   "authorizationStateJson", "cancelAuthorizeJson", "diagnosticsJson",
                   "openExternal"):
        assert method in runtime, f"WpsRuntime 对外接口缺失：{method}"


def test_python_adapter_and_xdgopen_shim_are_present():
    adapter = ROOT / "app" / "wps" / "android_runtime.py"
    shim = ANDROID / "app" / "src" / "main" / "cpp" / "xdgopen_shim.c"
    fetcher = ROOT / "scripts" / "fetch_android_runtime.py"
    assert adapter.is_file()
    assert 'RUNTIME_MARKER = "@android-runtime"' in _read(adapter)
    assert shim.is_file() and "YIKOU_AUTH_URL_FILE" in _read(shim)
    assert fetcher.is_file()


def test_android_updater_has_native_json_bridge_and_no_browser_fallback():
    """Python 只能通过 @JvmStatic JSON 接口调用原生更新器；更新失败不得跳浏览器。"""
    updater = _read(ANDROID / "app" / "src" / "main" / "java" / "com"
                    / "yikou" / "lightfood" / "AppUpdater.kt")
    assert '@JvmStatic' in updater
    assert 'fun updateCapabilitiesJson' in updater
    assert 'fun verifyUpdateApkJson' in updater
    assert 'fun installApkJson' in updater
    assert 'fun openInstallPermissionSettingsJson' in updater
    assert 'openExternal' not in updater
    manifest = _read(ANDROID / "app" / "src" / "main" / "AndroidManifest.xml")
    assert 'android.permission.REQUEST_INSTALL_PACKAGES' in manifest


def test_main_activity_does_not_duplicate_update_dialog():
    activity = _read(ANDROID / "app" / "src" / "main" / "java" / "com"
                     / "yikou" / "lightfood" / "MainActivity.kt")
    assert 'showUpdateDialog' not in activity
    assert 'checkForUpdates' not in activity


def test_bridge_exposes_in_app_install_methods():
    bridge = _read(ROOT / "app" / "api" / "bridge.py")
    assert 'def install_update' in bridge
    assert 'def cancel_update' in bridge
    assert 'def open_install_settings' in bridge
    assert 'update:progress' in bridge
    assert 'open_external' not in bridge.split('def __init__')[0]  # 仅确保模块头没有旧兜底
