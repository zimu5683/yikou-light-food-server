"""文档引用存在性门禁：仓库里的相对路径引用必须指向真实存在的文件。

扫描 `git ls-files`（含未忽略的未跟踪文件）里的这几类文件：
  - `*.md` 的 Markdown 相对链接 `](path)`；
  - 上述 `.md` 与 `app/**/*.py`、`frontend/src/**/*.{ts,tsx}`、
    `frontend/scripts/*.mjs` 里反引号包住的路径（以 `docs/`、`design/`、
    `frontend/`、`scripts/`、`app/`、`tests/` 开头）。

范围与豁免（2026-09-21 与 lead 确认，改之前先读这里）：
  - 排除 `design/archive/**`：归档资料，不再维护引用；
  - **Markdown 链接全仓检查，包含 `design/**`**：链接就是导航，坏链接必须修；
  - **反引号里的仓库内路径只在「当前面」检查**：`README.md`、`docs/**`、`app/**`、
    `frontend/**`、`tests/**`、`scripts/**`，以及本目录索引 `design/README.md`。
    `design/**` 其余部分整体豁免：design/ 是历史过程资料（`design/README.md` 自己声明
    「文中 app/xxx.py 多为重构前路径」），迭代日志里记的是重构前的 app/sss.py、
    app/wps_cloud.py 之类，强制这些历史文本里的路径存在等于篡改历史。
  - 白名单只有两类：外部 URL（`http(s)://` / `mailto:` / 锚点 / 绝对路径，抽取阶段即跳过），
    以及 `design/references/**` 引用的上游设计文档路径（EXTERNAL_REFERENCE_WHITELIST）。
  - **被 .gitignore 覆盖的路径不检查**：`frontend/dist` 这类构建产物、运行时文件本就不入库，
    在全新 checkout 里必然不存在 —— 它们不是坏引用。判据交给 git 自己
    （`git check-ignore`），改了 .gitignore 判据跟着变，**不是白名单**。
    这条是 3.6.14 出包时踩出来的：APK 流水线的「Python test baseline」跑在
    「Build frontend」之前，当时把 `frontend/dist` 判成了坏引用，直接挡住了出包。

离线、只依赖 git 与标准库，运行 <2s。失败信息形如 `文件:行号 引用`，照着改即可。
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# 扫描范围
EXCLUDED_PREFIXES = ("design/archive/",)
# design/ 的反引号豁免（例外：索引与外部参考仍然检查）
BACKTICK_EXEMPT_PREFIXES = ("design/",)
BACKTICK_EXEMPT_EXCEPTIONS = ("design/README.md", "design/references/")
# 白名单：只放 design/references/** 引用的上游（外部）设计文档路径
EXTERNAL_REFERENCE_WHITELIST = ("scripts/derive-examples-block.mjs", "design-md/")
PATH_PREFIXES = ("docs/", "design/", "frontend/", "scripts/", "app/", "tests/")
# 已删除/已归档的文档：不许在旧路径复活
FORBIDDEN_PATHS = ("design/交接文档-WPS云同步.md", "docs/DESKTOP-PARITY.md")
MIN_SCANNED_FILES = 50

_BT = chr(96)
_MD_LINK = re.compile(r"\]\(([^)\n]+)\)")
_BACKTICK = re.compile(_BT + "([^" + _BT + r"\n]+)" + _BT)
_SKIP_CHARS = set("?*\\|<>$= ") | {_BT}
_SYMBOL_SUFFIX = re.compile(r"::[A-Za-z_][\w.]*(?:\(\))?$")
_LINE_SUFFIX = re.compile(r":\d+(?:-\d+)?$")
_BRACE = re.compile(r"\{([^{}]*)\}")
_TRAILING = "。，,.;；:："


def _repo_files() -> list[str]:
    result = subprocess.run(
        ["git", "-c", "core.quotePath=false", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return sorted(name for name in result.stdout.split("\0") if name)


def _is_scanned(name: str) -> bool:
    if name.startswith(EXCLUDED_PREFIXES):
        return False
    if name.endswith(".md"):
        return True
    if name.startswith("app/") and name.endswith(".py"):
        return True
    if name.startswith("frontend/src/") and name.endswith((".ts", ".tsx")):
        return True
    return name.startswith("frontend/scripts/") and name.endswith(".mjs") and name.count("/") == 2


def scan_targets() -> list[str]:
    return [name for name in _repo_files() if _is_scanned(name)]


def _resolves(target: str, source: str) -> bool:
    candidates = [REPO_ROOT / target]
    if not target.startswith("/"):
        candidates.append(REPO_ROOT / os.path.dirname(source) / target)
    return any(candidate.exists() for candidate in candidates)


def _expand_braces(target: str) -> list[str]:
    match = _BRACE.search(target)
    if not match:
        return [target]
    expanded: list[str] = []
    for part in match.group(1).split(","):
        expanded.extend(_expand_braces(target[: match.start()] + part + target[match.end():]))
    return expanded


def _normalise(token: str) -> str:
    token = token.split("#", 1)[0]
    token = _SYMBOL_SUFFIX.sub("", token)
    token = _LINE_SUFFIX.sub("", token)
    return token.strip().rstrip(_TRAILING)


def _is_external(target: str) -> bool:
    if not target:
        return True
    if target.startswith(("http://", "https://", "mailto:", "#", "/", "data:")):
        return True
    return "://" in target


def _whitelisted(target: str) -> bool:
    return any(target == entry or target.startswith(entry) for entry in EXTERNAL_REFERENCE_WHITELIST)


_IGNORED_CACHE: dict[str, bool] = {}


def _is_git_ignored(target: str) -> bool:
    """``target`` 是否被 .gitignore 覆盖（构建产物 / 运行时文件）。

    判据由 git 自己给（`git check-ignore -q`），不在这里维护任何路径清单：
    `frontend/dist`、`backups/` 之类在全新 checkout 里必然不存在，但它们
    **不是坏引用**，而是刻意不入库的产物。
    """
    if target in _IGNORED_CACHE:
        return _IGNORED_CACHE[target]
    result = subprocess.run(
        ["git", "-c", "core.quotePath=false", "check-ignore", "-q", "--", target],
        cwd=REPO_ROOT,
        capture_output=True,
        check=False,
    )
    ignored = result.returncode == 0
    _IGNORED_CACHE[target] = ignored
    return ignored


def _checks_backticks(source: str) -> bool:
    if not source.startswith(BACKTICK_EXEMPT_PREFIXES):
        return True
    return source.startswith(BACKTICK_EXEMPT_EXCEPTIONS)


def find_broken_references() -> list[str]:
    """返回形如 `文件:行号 引用` 的失效引用列表。"""
    broken: list[str] = []
    for name in scan_targets():
        text = (REPO_ROOT / name).read_text(encoding="utf-8")
        if name.endswith(".md"):
            for match in _MD_LINK.finditer(text):
                raw = match.group(1).strip()
                target = raw.split("#", 1)[0].strip().rstrip(_TRAILING)
                if _is_external(target) or any(char in target for char in _SKIP_CHARS):
                    continue
                if _whitelisted(target) or _is_git_ignored(target):
                    continue
                if not _resolves(target, name):
                    line = text[: match.start()].count("\n") + 1
                    broken.append(f"{name}:{line} ]({raw})")
        if not _checks_backticks(name):
            continue
        for match in _BACKTICK.finditer(text):
            raw = match.group(1).strip()
            target = _normalise(raw)
            if not target.startswith(PATH_PREFIXES) or any(char in target for char in _SKIP_CHARS):
                continue
            if _whitelisted(target):
                continue
            expanded_targets = _expand_braces(target)
            if all(_is_git_ignored(item) for item in expanded_targets):
                continue
            if not any(_resolves(expanded, name) for expanded in expanded_targets):
                line = text[: match.start()].count("\n") + 1
                broken.append(f"{name}:{line} {raw}")
    return broken


def test_scan_set_is_not_empty() -> None:
    targets = scan_targets()
    assert len(targets) >= MIN_SCANNED_FILES, f"门禁只扫到 {len(targets)} 个文件，git ls-files 可能失效"


def test_doc_references_resolve() -> None:
    broken = find_broken_references()
    assert not broken, "发现指向不存在文件的引用：\n" + "\n".join(broken)


def test_gitignored_build_outputs_are_out_of_scope() -> None:
    """构建产物（frontend/dist）在全新 checkout 里不存在，但不算坏引用。

    3.6.14 出包时这里判错过：APK 流水线的 pytest 跑在 pnpm build 之前，
    把 `frontend/dist` 判成坏引用直接挡住了出包。
    """
    assert _is_git_ignored("frontend/dist")
    assert _is_git_ignored("frontend/dist/index.html")
    assert not _is_git_ignored("frontend/src/App.tsx")


def test_removed_docs_stay_removed() -> None:
    resurrected = [path for path in FORBIDDEN_PATHS if (REPO_ROOT / path).exists()]
    assert not resurrected, "这些文档已删除/归档，不要在旧路径复活：" + "、".join(resurrected)
