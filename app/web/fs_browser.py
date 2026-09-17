"""服务器端文件浏览器：列出运行主机上的目录与 Excel 文件。"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

#: 文件浏览器默认展示的根目录候选（按存在与否过滤）。
BROWSE_ROOTS = (
    "/sdcard",
    "/storage/emulated/0",
    "/storage/emulated/0/Download",
    "/storage/emulated/0/Documents",
)

#: Excel 相关后缀，供文件浏览器过滤。
EXCEL_SUFFIXES = frozenset({".xlsx", ".xlsm", ".xls"})


def browse_root(raw: str) -> Path:
    """把前端传入路径解析成待浏览目录；空路径回退到第一个存在的根目录。"""
    raw = (raw or "").strip()
    if not raw:
        return next((Path(p) for p in BROWSE_ROOTS if Path(p).is_dir()), Path.home())
    return Path(os.path.expanduser(raw))


def list_dir(target: Path) -> dict[str, Any]:
    """列出目录内容；权限/无效路径等情况返回带 ``error`` 的结构，不抛异常。"""
    try:
        target = target.resolve()
    except (OSError, RuntimeError) as exc:
        return {"path": str(target), "error": f"路径无效：{exc}", "entries": []}
    if not target.is_dir():
        return {"path": str(target), "error": "目录不存在或不是目录", "entries": []}

    entries: list[dict[str, Any]] = []
    try:
        for child in sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
            if child.name.startswith("."):
                continue
            try:
                is_dir = child.is_dir()
                size = 0 if is_dir else child.stat().st_size
            except OSError:
                # 单个条目不可读（权限/符号链接断开）时跳过，不让整页失败。
                continue
            if not is_dir and child.suffix.lower() not in EXCEL_SUFFIXES:
                continue
            entries.append({
                "name": child.name,
                "path": str(child),
                "is_dir": is_dir,
                "size": size,
            })
    except PermissionError:
        # Android 11+ 未授权存储时 /sdcard 不可读，给出可执行的修复指引。
        return {
            "path": str(target),
            "error": "没有权限读取该目录。若要访问手机存储，请先在 Termux 执行 "
                     "termux-setup-storage 并允许权限。",
            "entries": [],
        }
    except OSError as exc:
        return {"path": str(target), "error": f"读取目录失败：{exc}", "entries": []}

    home = Path.home()
    shortcuts = [{"name": p, "path": p} for p in BROWSE_ROOTS if Path(p).is_dir()]
    if home.is_dir():
        shortcuts.append({"name": "Termux 主目录", "path": str(home)})
    return {
        "path": str(target),
        "parent": str(target.parent) if target.parent != target else "",
        "entries": entries,
        "shortcuts": shortcuts,
        "error": "",
    }
