"""架构边界回归锁：领域层不得反向依赖交付层。

分层（依赖只能向右/向下，不能反向）：

    app.web  →  app.api  →  {app.order, app.ordering, app.wps}
                              ↘      ↓       ↙
                               app.integrations / app.core

本文件用 AST 静态扫描 ``app/``，不导入、不执行生产代码。以后如果有人把
``web`` import 塞进领域模块，或让 ``core`` 依赖 ``requests``/HTTP 交付层，
这里会先红。
"""
from __future__ import annotations

import ast
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parent.parent / "app"

#: 包前缀 -> 禁止它依赖的内部包前缀。
#: 只约束方向，不做白名单穷举；新增领域包时按同一原则补规则。
FORBIDDEN_DEPENDENCIES = {
    "app.core": ("app.api", "app.order", "app.ordering", "app.wps",
                 "app.web", "app.integrations"),
    "app.integrations": ("app.core", "app.api", "app.order", "app.ordering",
                         "app.wps", "app.web"),
    "app.order": ("app.api", "app.web", "app.ordering", "app.wps"),
    "app.ordering": ("app.api", "app.web"),
    "app.wps": ("app.api", "app.web", "app.order", "app.ordering",
                "app.integrations"),
    "app.api": ("app.web",),
    "app.web": ("app.order", "app.ordering", "app.wps", "app.integrations"),
}


def _module_name(path: Path) -> str:
    rel = path.relative_to(APP_ROOT.parent).with_suffix("")
    return ".".join(rel.parts)


def _iter_modules() -> list[Path]:
    return sorted(p for p in APP_ROOT.rglob("*.py") if "__pycache__" not in p.parts)


def _iter_imports(tree: ast.AST):
    """Yield (node, target_module) for every import, including function-level ones."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield node, alias.name
        elif isinstance(node, ast.ImportFrom):
            parts = node.module.split(".") if node.module else []
            if node.level:
                package = _module_name(Path(str(node)).resolve()).split(".")[:-1]
                parts = package[:len(package) - node.level + 1] + parts
            if parts:
                yield node, ".".join(parts)


def _matches(target: str, prefix: str) -> bool:
    return target == prefix or target.startswith(prefix + ".")


def test_domain_packages_do_not_depend_on_outer_layers():
    violations: list[str] = []
    for path in _iter_modules():
        owner = _module_name(path)
        for package, forbidden in FORBIDDEN_DEPENDENCIES.items():
            if not _matches(owner, package):
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node, target in _iter_imports(tree):
                for blocked in forbidden:
                    if _matches(target, blocked):
                        violations.append(
                            f"{owner}:{getattr(node, 'lineno', '?')} 禁止依赖 {target}")
    assert not violations, "依赖方向被破坏：\n" + "\n".join(violations)


def test_no_desktop_or_browser_framework_imports_in_server_code():
    banned = ("playwright", "webview", "selenium", "pyautogui")
    found: list[str] = []
    for path in _iter_modules():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node, target in _iter_imports(tree):
            if target.split(".")[0] in banned:
                found.append(f"{_module_name(path)}:{node.lineno} -> {target}")
    assert not found, "服务端代码混入了浏览器/桌面框架依赖：\n" + "\n".join(found)


def test_internal_import_graph_is_acyclic():
    """领域包之间不允许循环 import；有环会让 rope 式机械重构越改越脆。"""
    internal = {_module_name(p) for p in _iter_modules()}
    edges: dict[str, set[str]] = {name: set() for name in internal}
    for path in _iter_modules():
        owner = _module_name(path)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for _node, target in _iter_imports(tree):
            # 只对“精确存在的模块”建边；`from app.order import runner` 会得到
            # app.order（包本身），不会误报成 sss -> runner 的伪边。
            if target in internal and target != owner:
                edges[owner].add(target)

    visiting: set[str] = set()
    done: set[str] = set()
    cycles: list[str] = []

    def visit(node: str, stack: list[str]) -> None:
        if node in done:
            return
        if node in visiting:
            cycles.append(" -> ".join(stack + [node]))
            return
        visiting.add(node)
        for nxt in sorted(edges.get(node, ())):
            visit(nxt, stack + [node])
        visiting.discard(node)
        done.add(node)

    for name in sorted(internal):
        visit(name, [])
    assert not cycles, "内部 import 出现环：\n" + "\n".join(cycles)
