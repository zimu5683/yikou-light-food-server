"""前端静态资源的解析：路由 -> 文件 -> MIME / 缓存策略。"""

from __future__ import annotations

import mimetypes
from pathlib import Path
from urllib.parse import unquote


class StaticFileError(Exception):
    """静态资源无法安全提供；字段可直接转成 JSON 错误响应。"""

    def __init__(self, status: int, message: str, code: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.code = code


def read_static(route: str, dist_dir: Path) -> tuple[bytes, str, str]:
    """解析并读取静态文件，返回 ``(body, content_type, cache_control)``。

    任何目录穿越、路径非法、文件缺失或读取失败都抛 :class:`StaticFileError`。
    """
    rel = unquote(route).lstrip("/") or "index.html"
    dist = dist_dir.resolve()
    try:
        candidate = (dist / rel).resolve()
    except (OSError, RuntimeError) as exc:
        raise StaticFileError(400, "非法路径", "bad_path") from exc
    # 目录穿越防护：解析后必须仍在 dist 之内。
    if candidate != dist and dist not in candidate.parents:
        raise StaticFileError(403, "越权路径", "forbidden")
    if candidate.is_dir():
        candidate = candidate / "index.html"
    if not candidate.is_file():
        raise StaticFileError(404, "文件不存在", "not_found")
    try:
        body = candidate.read_bytes()
    except OSError as exc:
        raise StaticFileError(500, f"读取失败：{exc}", "read_failed") from exc
    ctype = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
    if ctype.startswith("text/") or ctype in {"application/javascript", "application/json"}:
        ctype += "; charset=utf-8"
    # index.html 不缓存：重新构建前端后刷新即可生效。
    cache = "no-store" if candidate.name == "index.html" else "public, max-age=300"
    return body, ctype, cache
