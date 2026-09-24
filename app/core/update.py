"""版本更新：检查 GitHub Release，并下载、校验 Android 安装包。

职责边界
--------
* 检查更新只读 GitHub API，不修改本地代码；
* Android APK 模式下，把 Release 里的 ``arm64`` APK 下载到本地缓存目录，
  校验大小/SHA-256 后交给 Kotlin 原生层拉起系统安装器；
* **不包含** Termux/纯浏览器的服务端热更新、替换源码或重启进程逻辑。

下载流程只写到 ``*.part``，全部校验通过后才原子改名为正式文件；任何异常都会
清理临时文件，不会把半成品交给安装器。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from app import __version__

#: 应用自己的发布仓库：这里的新版本才代表“应用需要更新”。
WEB_REPOSITORY = "zimu5683/yikou-light-food-server"
#: 兼容旧名称；默认仍指应用自己。
REPOSITORY = WEB_REPOSITORY

_USER_AGENT = f"yikou-light-food/{__version__}"
_CHUNK_SIZE = 256 * 1024

#: 版本号形如 3.5.0 或 3.6.6.1（允许 v 前缀）；第四段用于补丁/热修版本。
_VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:\.(\d+))?$")


def releases_url() -> str:
    """返回发布仓库的 latest release API 地址。"""
    return f"https://api.github.com/repos/{WEB_REPOSITORY}/releases/latest"


class ReleaseCheckError(RuntimeError):
    """检查/下载更新失败，消息可直接展示给用户。"""


class UpdateCancelled(RuntimeError):
    """用户主动取消下载。"""


@dataclass(frozen=True)
class ReleaseAsset:
    """GitHub Release 里的一个附件。"""

    name: str
    download_url: str = ""
    size: int = 0

    @property
    def is_apk(self) -> bool:
        return self.name.lower().endswith(".apk")

    @property
    def is_sha256(self) -> bool:
        lower = self.name.lower()
        return lower.endswith(".sha256")


@dataclass
class ReleaseInfo:
    """一个 GitHub Release 的概要（含安装包资产）。"""

    tag_name: str
    name: str = ""
    body: str = ""
    html_url: str = ""
    repository: str = REPOSITORY
    assets: tuple[ReleaseAsset, ...] = field(default_factory=tuple)

    @property
    def version(self) -> str:
        """规范化版本号（去掉 ``v`` 前缀）。"""
        return self.tag_name.lstrip("v")

    @property
    def release_url(self) -> str:
        return self.html_url or f"https://github.com/{self.repository}/releases"


def _version_tuple(text: str) -> tuple[int, int, int, int] | None:
    match = _VERSION_RE.match((text or "").strip())
    if not match:
        return None
    # 第四段缺省按 0 处理：3.6.6 == 3.6.6.0，便于和热修版本比较。
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)),
            int(match.group(4) or 0))


def compare_versions(left: str, right: str) -> int:
    """比较版本号：left 大于 right 返回正数，相等返回 0，小于返回负数。

    非 SemVer 视为最小（返回 -1 表示「不可比」的保守处理）。
    """
    lhs, rhs = _version_tuple(left), _version_tuple(right)
    if lhs is None or rhs is None:
        return -1
    return (lhs > rhs) - (lhs < rhs)


def _parse_assets(payload: dict[str, Any]) -> tuple[ReleaseAsset, ...]:
    """解析 Release JSON 里的 ``assets``；非法条目直接忽略。"""
    raw_assets = payload.get("assets")
    if not isinstance(raw_assets, list):
        return ()
    assets: list[ReleaseAsset] = []
    for item in raw_assets:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        url = str(item.get("browser_download_url") or "").strip()
        if not name or not url:
            continue
        try:
            size = int(item.get("size") or 0)
        except (TypeError, ValueError):
            size = 0
        assets.append(ReleaseAsset(name=name, download_url=url, size=max(size, 0)))
    return tuple(assets)


def fetch_latest_release(*, timeout: float = 10.0) -> ReleaseInfo:
    """读取最新 Release；网络或解析失败抛 :class:`ReleaseCheckError`。"""
    request = Request(
        releases_url(),
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": _USER_AGENT,
        },
    )
    try:
        with urlopen(request, timeout=timeout) as response:  # noqa: S310 - 固定 https 地址
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        if exc.code == 404:
            raise ReleaseCheckError("仓库还没有发布任何 Release") from exc
        if exc.code == 403:
            raise ReleaseCheckError(
                "检查更新失败：GitHub 接口拒绝/限流（匿名 API 每小时 60 次），"
                "请稍后再试或在服务器设置 GITHUB_TOKEN") from exc
        raise ReleaseCheckError(f"检查更新失败（HTTP {exc.code}）") from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise ReleaseCheckError(f"检查更新失败（网络不通）：{exc}") from exc
    except (ValueError, UnicodeError) as exc:
        raise ReleaseCheckError(f"检查更新失败（返回内容异常）：{exc}") from exc

    if not isinstance(payload, dict):
        raise ReleaseCheckError("检查更新失败：返回内容不是 JSON 对象")
    tag = str(payload.get("tag_name") or "").strip()
    if not tag:
        raise ReleaseCheckError("检查更新失败：Release 里没有版本号")
    return ReleaseInfo(
        tag_name=tag,
        name=str(payload.get("name") or ""),
        body=str(payload.get("body") or ""),
        html_url=str(payload.get("html_url") or ""),
        assets=_parse_assets(payload),
    )


def _asset_rank(name: str) -> int:
    """APK 资产优先级：arm64/aarch64 > universal > 其它。"""
    lower = name.lower()
    if "arm64" in lower or "aarch64" in lower:
        return 0
    if "universal" in lower:
        return 1
    return 2


def select_android_apk(release: ReleaseInfo) -> ReleaseAsset | None:
    """从 Release 附件里选一个 Android APK。

    本项目只发 arm64；仍保留 universal/第一个 APK 作为兜底，便于旧 Release
    或手工发布时也能识别。
    """
    apks = [asset for asset in release.assets if asset.is_apk and asset.download_url]
    if not apks:
        return None
    return min(apks, key=lambda asset: (_asset_rank(asset.name), asset.name))


def select_sha256_asset(release: ReleaseInfo,
                        apk: ReleaseAsset) -> ReleaseAsset | None:
    """找 ``xxx.apk.sha256`` 附件；大小写不敏感。"""
    expected = f"{apk.name}.sha256"
    for asset in release.assets:
        if asset.name == expected:
            return asset
    for asset in release.assets:
        if asset.name.lower() == expected.lower():
            return asset
    return None


def read_asset_text(asset: ReleaseAsset, *, timeout: float = 10.0) -> str:
    """读取小的文本附件（目前用于 .sha256 校验文件）。"""
    request = Request(
        asset.download_url,
        headers={
            "Accept": "text/plain",
            "User-Agent": _USER_AGENT,
        },
    )
    try:
        with urlopen(request, timeout=timeout) as response:  # noqa: S310 - 固定 https 地址
            return response.read(64 * 1024).decode("utf-8", errors="replace")
    except HTTPError as exc:
        raise ReleaseCheckError(f"读取校验文件失败（HTTP {exc.code}）") from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise ReleaseCheckError(f"读取校验文件失败（网络异常）：{exc}") from exc


def parse_sha256(text: str) -> str:
    """从 `.sha256` 文件内容中提取 64 位十六进制摘要。"""
    for token in re.split(r"\s+", (text or "").strip()):
        token = token.strip().lower()
        if re.fullmatch(r"[0-9a-f]{64}", token):
            return token
    raise ReleaseCheckError("Release 的 sha256 校验文件格式异常")


def _file_sha256(path: Path, *, chunk_size: int = _CHUNK_SIZE) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def download_asset(
    asset: ReleaseAsset,
    dest: str | os.PathLike[str],
    *,
    expected_sha256: str = "",
    expected_size: int | None = None,
    on_progress: Callable[[int, int], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
    timeout: float = 60.0,
    chunk_size: int = _CHUNK_SIZE,
) -> Path:
    """下载 Release 附件到 ``dest``，校验通过后才落正式文件。

    - 先写 ``dest.part``，失败/取消时删除；
    - ``on_progress(downloaded, total)`` 会在开始和每个分块后回调；
    - ``expected_size`` 与 ``expected_sha256`` 非空时做强制校验。
    """
    destination = Path(dest)
    destination.parent.mkdir(parents=True, exist_ok=True)
    part = destination.with_name(f"{destination.name}.part")
    expected_size = expected_size if expected_size and expected_size > 0 else None

    def progress(downloaded: int, total: int) -> None:
        if on_progress is not None:
            try:
                on_progress(downloaded, total)
            except Exception:  # noqa: BLE001 - 进度回调失败不能中断下载
                pass

    progress(0, expected_size or max(asset.size, 0))
    try:
        request = Request(
            asset.download_url,
            headers={
                "Accept": "application/octet-stream",
                "User-Agent": _USER_AGENT,
            },
        )
        try:
            with urlopen(request, timeout=timeout) as response:  # noqa: S310 - 固定 https 地址
                header_length = str(response.headers.get("Content-Length") or "").strip()
                total = int(header_length) if header_length.isdigit() else (expected_size or asset.size or 0)
                digest = hashlib.sha256()
                downloaded = 0
                with part.open("wb") as output:
                    while True:
                        if cancel_check is not None and cancel_check():
                            raise UpdateCancelled("已取消下载")
                        chunk = response.read(chunk_size)
                        if not chunk:
                            break
                        output.write(chunk)
                        digest.update(chunk)
                        downloaded += len(chunk)
                        progress(downloaded, total)
                    output.flush()
                    os.fsync(output.fileno())
        except HTTPError as exc:
            raise ReleaseCheckError(f"下载安装包失败（HTTP {exc.code}）") from exc
        except UpdateCancelled:
            raise
        except (URLError, TimeoutError, OSError) as exc:
            raise ReleaseCheckError(f"下载安装包失败（网络异常）：{exc}") from exc

        if expected_size is not None and downloaded != expected_size:
            raise ReleaseCheckError(
                f"安装包不完整：期望 {expected_size} 字节，实际 {downloaded} 字节")
        if expected_sha256:
            actual = digest.hexdigest()
            if actual.lower() != expected_sha256.strip().lower():
                raise ReleaseCheckError("安装包 SHA-256 校验失败，已拒绝安装")
        os.replace(part, destination)
        progress(downloaded, downloaded or total)
        return destination
    except BaseException:
        try:
            part.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def check_for_update(current_version: str = __version__,
                     **kwargs: Any) -> ReleaseInfo | None:
    """检查是否有比当前版本更新的 Release。

    远端版本 **小于或等于** 当前版本都返回 ``None``：远端更旧或相同都不算“有更新”，
    不是错误，更不应该在启动时弹报错。
    """
    if _version_tuple(current_version) is None:
        raise ReleaseCheckError(f"当前版本号不是 SemVer，无法比较：{current_version}")
    release = fetch_latest_release(**kwargs)
    if _version_tuple(release.tag_name) is None:
        raise ReleaseCheckError(f"Release 版本号不是 SemVer：{release.tag_name}")
    comparison = compare_versions(release.version, current_version)
    if comparison < 0:
        # 远端更旧：不降级，也不算“有更新”。
        return None
    if comparison == 0:
        return None
    return release
