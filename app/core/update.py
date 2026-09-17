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

#: 发布仓库。与 README / git remote 保持一致。
REPOSITORY = "zimu5683/yikou-light-food-server"
RELEASES_URL = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"

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

    @property
    def version(self) -> str:
        """规范化版本号（去掉 ``v`` 前缀）。"""
        return self.tag_name.lstrip("v")

    @property
    def release_url(self) -> str:
        return self.html_url or f"https://github.com/{REPOSITORY}/releases"


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


def fetch_latest_release(*, timeout: float = 10.0) -> ReleaseInfo:
    """读取最新 Release。网络或解析失败抛 :class:`ReleaseCheckError`。"""
    request = Request(
        RELEASES_URL,
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
    )


def check_for_update(current_version: str = __version__, **kwargs: Any) -> ReleaseInfo | None:
    """有新版本返回 :class:`ReleaseInfo`，已是最新返回 ``None``。

    与旧实现一致的行为：**同版本不算更新**（不静默覆盖），**降级拒绝**。
    """
    if _version_tuple(current_version) is None:
        raise ReleaseCheckError(f"当前版本号不是 SemVer，无法比较：{current_version}")
    release = fetch_latest_release(**kwargs)
    if _version_tuple(release.tag_name) is None:
        raise ReleaseCheckError(f"Release 版本号不是 SemVer：{release.tag_name}")
    comparison = compare_versions(release.version, current_version)
    if comparison < 0:
        raise ReleaseCheckError(
            f"远端版本 {release.version} 低于当前版本 {current_version}，拒绝降级")
    if comparison == 0:
        return None
    return release
