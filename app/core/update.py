"""只做一件事：去 GitHub 看有没有新版本。

为什么单独一个模块
------------------
本项目只做一件事：查 GitHub Release 的 tag，与 ``__version__`` 比较，
在界面上提示「有新版本」。

**明确不做的**：不下载、不校验签名、不替换文件、不重启进程。
要在手机上升级，方式是对着仓库 ``git pull`` 后 ``sv restart yikou-light-food``。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from app import __version__

#: 网页版服务端自己的发布仓库：这里的新版本才代表“网页版需要更新”。
WEB_REPOSITORY = "zimu5683/yikou-light-food-server"
#: 桌面版仓库：只在网页版里做“桌面端有更新”的提示，不参与网页版版本比较。
DESKTOP_REPOSITORY = "zimu5683/yikou-light-food-desktop"
#: 兼容旧名称；默认仍指网页版自己。
REPOSITORY = WEB_REPOSITORY


def releases_url(repository: str) -> str:
    """返回某个 GitHub 仓库的 latest release API 地址。"""
    return f"https://api.github.com/repos/{repository}/releases/latest"

#: 版本号必须形如 3.5.0（允许 v 前缀）。非 SemVer 一律拒绝比较，避免误判。
_VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")


class ReleaseCheckError(RuntimeError):
    """检查更新失败（网络不通、返回异常等），消息可直接展示给用户。"""


@dataclass
class ReleaseInfo:
    """一个 GitHub Release 的概要。"""

    tag_name: str
    name: str = ""
    body: str = ""
    html_url: str = ""
    repository: str = REPOSITORY

    @property
    def version(self) -> str:
        """规范化版本号（去掉 ``v`` 前缀）。"""
        return self.tag_name.lstrip("v")

    @property
    def release_url(self) -> str:
        return self.html_url or f"https://github.com/{self.repository}/releases"


def _version_tuple(text: str) -> tuple[int, int, int] | None:
    match = _VERSION_RE.match((text or "").strip())
    if not match:
        return None
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


def compare_versions(left: str, right: str) -> int:
    """比较版本号：left 大于 right 返回正数，相等返回 0，小于返回负数。

    非 SemVer 视为最小（返回 -1 表示「不可比」的保守处理）。
    """
    lhs, rhs = _version_tuple(left), _version_tuple(right)
    if lhs is None or rhs is None:
        return -1
    return (lhs > rhs) - (lhs < rhs)


def fetch_latest_release(*, repository: str = WEB_REPOSITORY,
                         timeout: float = 10.0) -> ReleaseInfo:
    """读取指定仓库的最新 Release；网络或解析失败抛 :class:`ReleaseCheckError`。"""
    repository = str(repository or WEB_REPOSITORY).strip() or WEB_REPOSITORY
    request = Request(
        releases_url(repository),
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": f"yikou-light-food/{__version__}",
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

    tag = str(payload.get("tag_name") or "").strip()
    if not tag:
        raise ReleaseCheckError("检查更新失败：Release 里没有版本号")
    return ReleaseInfo(
        tag_name=tag,
        name=str(payload.get("name") or ""),
        body=str(payload.get("body") or ""),
        html_url=str(payload.get("html_url") or ""),
        repository=repository,
    )


def check_for_update(current_version: str = __version__, *,
                     repository: str = WEB_REPOSITORY,
                     **kwargs: Any) -> ReleaseInfo | None:
    """检查指定仓库是否有比当前版本更新的 Release。

    远端版本 **小于或等于** 当前版本都返回 ``None``：远端更旧只说明两个仓库的
    版本轨道还没对齐，不是错误，更不应该在启动时弹报错。
    """
    if _version_tuple(current_version) is None:
        raise ReleaseCheckError(f"当前版本号不是 SemVer，无法比较：{current_version}")
    release = fetch_latest_release(repository=repository, **kwargs)
    if _version_tuple(release.tag_name) is None:
        raise ReleaseCheckError(f"Release 版本号不是 SemVer：{release.tag_name}")
    comparison = compare_versions(release.version, current_version)
    if comparison < 0:
        # 远端更旧：不降级，也不算“有更新”。
        return None
    if comparison == 0:
        return None
    return release
