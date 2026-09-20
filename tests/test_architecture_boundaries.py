"""架构边界回归锁：领域层不得反向依赖交付层。

分层（依赖只能向右/向下，不能反向）：

    app.web  →  app.api  →  {app.order, app.ordering, app.wps}
                              ↘      ↓       ↙
                               app.integrations / app.core

本文件用 AST 静态扫描 ``app/``，不导入、不执行生产代码。以后如果有人把
``web`` import 塞进领域模块，或让 ``core`` 依赖 ``requests``/HTTP 交付层，
这里会先红。

R6-7 不变量（本文件的核心约定）：

* 模块名与相对 import 目标一律由「**被检查文件的绝对路径** + 仓库根目录」推导；
* ``_iter_imports`` 接收**真实文件路径**，不再把 AST 节点 ``str()`` 成路径
  （旧写法 ``_module_name(Path(str(node)).resolve())`` 在仓库根目录下会把相对
  import 静默解析成无意义模块名，在其它 CWD 下直接抛 ``ValueError``）；
* 扫描过程不读 ``Path.cwd()``，因此同一份代码在仓库根、``/tmp`` 或任意目录下
  运行，收集与断言结果完全一致（见文件末尾的 R6-7 回归测试）。

R8 子模块粒度（本文件第二组约定）：

* ``from pkg import sub`` / ``from .. import sub``（含 ``as`` 别名）会**额外**解析出
  ``pkg.sub``——前提是 ``sub`` 在**被扫描的树里真实存在**为一个模块或包；
* 模块集合由文件树静态推导（``_known_modules``），**绝不 import 任何生产模块**；
* 因此 ``from pkg import Thing`` / ``from pkg import SOME_CONSTANT`` 这类普通类、函数、
  常量不会被误判成子模块依赖（``pkg.Thing`` 不在已知模块集合里）；
* 保守之处：若包属性与同名子模块并存，静态上按子模块处理（Python 的
  ``from pkg import name`` 也仍会导入该子模块，除非属性已先行绑定）。
"""
from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import textwrap
from collections.abc import Collection, Iterator, Mapping, Sequence
from pathlib import Path

#: 仓库根目录：只由本测试文件自身位置推导（``__file__``），从不使用
#: ``Path.cwd()``，所以扫描结果与运行目录无关。
REPO_ROOT = Path(__file__).resolve().parent.parent
APP_ROOT = REPO_ROOT / "app"

#: 包前缀 -> 禁止它依赖的内部包前缀。
#: 只约束方向，不做白名单穷举；新增领域包时按同一原则补规则。
FORBIDDEN_DEPENDENCIES: Mapping[str, Sequence[str]] = {
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

#: 服务端代码里不允许出现的桌面/浏览器框架顶层包名。
BANNED_DESKTOP_FRAMEWORKS: Sequence[str] = (
    "playwright", "webview", "selenium", "pyautogui")


def _iter_modules(app_root: Path = APP_ROOT) -> list[Path]:
    """``app_root`` 下的全部 Python 文件（绝对路径、确定性排序）。"""
    root = Path(app_root).resolve()
    return sorted(p for p in root.rglob("*.py") if "__pycache__" not in p.parts)


def _known_modules(app_root: Path = APP_ROOT) -> frozenset[str]:
    """扫描树里**真实存在**的模块/包名集合（纯文件树静态推导，不 import 任何东西）。

    ``app/web/__init__.py`` → ``app.web``（包），``app/web/server.py`` →
    ``app.web.server``（模块）。同时把每个模块名的所有父前缀算进来
    （``app.api.bridge`` → ``app``、``app.api``），这样没有 ``__init__.py``
    的命名空间包也能被识别。集合只用于判断 ``from pkg import name`` 里的
    ``name`` 究竟是子模块还是普通类/函数/常量。
    """
    names: set[str] = set()
    for path in _iter_modules(app_root):
        parts = _module_name(path, app_root).split(".")
        names.update(".".join(parts[:i]) for i in range(1, len(parts) + 1))
    return frozenset(names)


def _module_name(path: Path, app_root: Path = APP_ROOT) -> str:
    """真实文件路径 -> 点分模块名；``__init__.py`` 归到包名本身。

    基准是「文件的绝对路径 + ``app_root``」，与进程 CWD 无关。
    """
    root = Path(app_root).resolve()
    rel = Path(path).resolve().relative_to(root.parent).with_suffix("")
    parts = list(rel.parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _resolve_relative_target(file_path: Path, node: ast.ImportFrom,
                             app_root: Path = APP_ROOT) -> str | None:
    """把一条 ``ImportFrom`` 解析成绝对模块名。

    ``file_path`` 必须是解析出 ``node`` 的那个**真实 Python 文件**：

    * ``node.level == 0``：绝对 import，直接返回 ``node.module``；
    * ``node.level >= 1``：以该文件自己的包为基准逐级上行
      （``__init__.py`` 的包就是它自身的模块名），绝不使用 CWD。

    上行越过顶层包（例如 ``app/main.py`` 里的 ``from .. import x``）时 Python
    自身会 ``ImportError``，不存在可解析的绝对目标，返回 ``None``。
    判定用 ``ascend >= len(parts)``：即使 ``node.module`` 非空
    （如 ``app/core/x.py`` 里的 ``from ...app.web import server``，CPython 会抛
    "attempted relative import beyond top-level package"）也返回 ``None``，
    否则会把不可执行代码里的 ``app.web`` 误报成依赖。
    """
    if not node.level:
        return node.module

    path = Path(file_path)
    module = _module_name(path, app_root)
    package = module if path.name == "__init__.py" else module.rpartition(".")[0]
    parts = [part for part in package.split(".") if part]

    ascend = node.level - 1
    if ascend >= len(parts):
        return None
    base = parts[: len(parts) - ascend] if ascend else parts
    if node.module:
        base = base + node.module.split(".")
    return ".".join(base) or None


def _iter_imports(file_path: Path,
                  app_root: Path = APP_ROOT,
                  known_modules: Collection[str] | None = None
                  ) -> Iterator[tuple[ast.AST, str]]:
    """遍历 ``file_path`` 中的每条 import，产出 ``(node, 绝对目标模块名)``。

    参数是**被检查的真实文件路径**，不是 AST 节点：只有拿得到文件自身位置，
    相对 import 才能正确解析。函数级/类级 import 由 ``ast.walk`` 一并覆盖。

    ``from pkg import sub``（含 ``from .. import sub`` 与 ``as`` 别名）会在
    ``pkg`` 之外**额外**产出一条 ``pkg.sub``——仅当 ``sub`` 出现在
    ``known_modules``（扫描树里真实存在的模块/包）里。默认 ``None`` 表示按
    ``app_root`` 现算；扫描入口会算一次后传入，避免每个文件重复遍历。
    普通类/函数/常量（``pkg.Thing``、``pkg.SOME_CONSTANT`` 等）不在集合里，
    因此不会被误判成子模块依赖。
    """
    path = Path(file_path)
    if known_modules is None:
        known_modules = _known_modules(app_root)
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield node, alias.name
        elif isinstance(node, ast.ImportFrom):
            target = _resolve_relative_target(path, node, app_root)
            if not target:
                continue
            yield node, target
            seen: set[str] = set()
            for alias in node.names:
                candidate = f"{target}.{alias.name}"
                if candidate in known_modules and candidate not in seen:
                    seen.add(candidate)
                    yield node, candidate


def _matches(target: str, prefix: str) -> bool:
    return target == prefix or target.startswith(prefix + ".")


def _forbidden_dependency_violations(
    app_root: Path = APP_ROOT,
    rules: Mapping[str, Sequence[str]] = FORBIDDEN_DEPENDENCIES,
) -> list[str]:
    """返回全部「反向依赖」违规（绝对路径扫描，与 CWD 无关）。"""
    violations: list[str] = []
    known_modules = _known_modules(app_root)
    for path in _iter_modules(app_root):
        owner = _module_name(path, app_root)
        for package, forbidden in rules.items():
            if not _matches(owner, package):
                continue
            for node, target in _iter_imports(path, app_root, known_modules):
                for blocked in forbidden:
                    if _matches(target, blocked):
                        violations.append(
                            f"{owner}:{getattr(node, 'lineno', '?')} 禁止依赖 {target}")
    return violations


def _banned_framework_imports(
    app_root: Path = APP_ROOT,
    banned: Sequence[str] = BANNED_DESKTOP_FRAMEWORKS,
) -> list[str]:
    """返回服务端代码里出现的桌面/浏览器框架依赖。"""
    found: list[str] = []
    known_modules = _known_modules(app_root)
    for path in _iter_modules(app_root):
        for node, target in _iter_imports(path, app_root, known_modules):
            if target.split(".")[0] in banned:
                found.append(f"{_module_name(path, app_root)}:{node.lineno} -> {target}")
    return found


def _internal_import_cycles(app_root: Path = APP_ROOT) -> list[str]:
    """领域包之间不允许循环 import；有环会让 rope 式机械重构越改越脆。"""
    known_modules = _known_modules(app_root)
    internal = {_module_name(p, app_root) for p in _iter_modules(app_root)}
    edges: dict[str, set[str]] = {name: set() for name in internal}
    for path in _iter_modules(app_root):
        owner = _module_name(path, app_root)
        for _node, target in _iter_imports(path, app_root, known_modules):
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
    return cycles


def _assert_real_app_tree_missing_is_not_a_pass() -> None:
    """扫描目标不存在时**显式失败**，而不是空转变绿。

    ``APP_ROOT`` 由 ``__file__`` 推导；如果有人把本文件复制到别处运行，
    扫描集合会变成空集、三个测试都会“通过”。这里把它变成硬错误。
    """
    assert APP_ROOT.is_dir(), f"APP_ROOT 不存在：{APP_ROOT}（扫描会空转通过，拒绝）"
    assert _iter_modules(), f"{APP_ROOT} 下没有 Python 文件（扫描会空转通过，拒绝）"


def test_domain_packages_do_not_depend_on_outer_layers():
    _assert_real_app_tree_missing_is_not_a_pass()
    violations = _forbidden_dependency_violations()
    assert not violations, "依赖方向被破坏：\n" + "\n".join(violations)


def test_no_desktop_or_browser_framework_imports_in_server_code():
    _assert_real_app_tree_missing_is_not_a_pass()
    found = _banned_framework_imports()
    assert not found, "服务端代码混入了浏览器/桌面框架依赖：\n" + "\n".join(found)


def test_internal_import_graph_is_acyclic():
    _assert_real_app_tree_missing_is_not_a_pass()
    cycles = _internal_import_cycles()
    assert not cycles, "内部 import 出现环：\n" + "\n".join(cycles)


# ---------------------------------------------------------------------------
# R6-7 回归：相对 import 的解析基准是「被检查文件」而不是 AST 节点/CWD
# ---------------------------------------------------------------------------

def _write_module(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(body).lstrip("\n"), encoding="utf-8")
    return path


def _make_synthetic_app(root: Path) -> Path:
    """在 ``root`` 下造一棵最小 ``app/`` 包树，内含**相对形式**的跨层 import。

    包名与层名故意和真实 ``app/`` 一致（core/web/api/order），这样同一套
    ``FORBIDDEN_DEPENDENCIES`` 规则可以直接复用。非法/合法样例成对存在，
    保证测试不是“见到 import 就报违规”——尤其包含：
    ``from app import web`` / ``from .. import order as _order``（裸子模块 + as
    别名，必须拦）与 ``from app.core import SOME_CONSTANT`` / ``from .models import
    Thing``（常量/类，不是模块，不能拦）。
    """
    app_root = root / "app"
    _write_module(app_root / "__init__.py",
                  '__version__ = "0.0.0-synthetic"\n')
    _write_module(app_root / "core" / "__init__.py",
                  'SOME_CONSTANT = 1\n')
    _write_module(app_root / "core" / "models.py", "class Thing:\n    pass\n")
    _write_module(app_root / "core" / "leak.py", """
        from ..web import server            # 非法：core -> web（模块级）
        from . import models                # 合法：包自身
        from .models import Thing           # 合法：包内子模块

        def handler():
            from ..web.server import dispatch   # 非法：函数级 import 也在扫描范围内
            return dispatch
    """)
    _write_module(app_root / "core" / "safe.py", """
        from ..core import models           # 合法：同包
        from . import models as _models     # 合法：包自身
    """)
    _write_module(app_root / "core" / "leak_alias.py", """
        from app import web                 # 非法：绝对 + 裸子模块
        from app import api as _api         # 非法：绝对 + as 别名
        from .. import order as _order      # 非法：相对 + as 别名

        from app import __version__         # 合法：包属性，不是模块
        from app.core import SOME_CONSTANT  # 合法：常量，不是模块
        from .models import Thing           # 合法：类，不是模块
    """)
    _write_module(app_root / "web" / "__init__.py", """
        from ..order import excel_io        # 非法：web -> order（__init__.py 的相对 import）
    """)
    _write_module(app_root / "web" / "server.py", "def dispatch():\n    return None\n")
    _write_module(app_root / "web" / "safe_web.py", """
        from ..api import bridge            # 合法：web -> api
    """)
    _write_module(app_root / "web" / "safe_web_alias.py", """
        from app import api                 # 合法：web -> api（裸子模块）
        from .. import api as _api          # 合法：相对 + as 别名
        from app.core.models import Thing   # 合法：类
    """)
    _write_module(app_root / "api" / "__init__.py", '"""api package."""\n')
    _write_module(app_root / "api" / "bridge.py", "class Bridge:\n    pass\n")
    _write_module(app_root / "order" / "__init__.py", '"""order package."""\n')
    _write_module(app_root / "order" / "excel_io.py", "def load():\n    return None\n")
    return app_root


def _violation_pairs(violations: Sequence[str]) -> set[tuple[str, str]]:
    """把 ``"owner:lineno 禁止依赖 target"`` 归一成 ``(owner, target)`` 集合。"""
    pairs: set[tuple[str, str]] = set()
    for item in violations:
        owner, _, rest = item.partition(":")
        pairs.add((owner, rest.split("禁止依赖 ", 1)[1]))
    return pairs


#: ``_make_synthetic_app`` 那棵树的期望违规集合（owner, target）。
#: 注意 ``from ..web import server`` 会同时给出 ``app.web`` 与真实存在的
#: ``app.web.server``，一条 import 因此可能产生两行。
_EXPECTED_SYNTHETIC_VIOLATIONS: frozenset[tuple[str, str]] = frozenset({
    ("app.core.leak", "app.web"),
    ("app.core.leak", "app.web.server"),
    ("app.core.leak_alias", "app.web"),
    ("app.core.leak_alias", "app.api"),
    ("app.core.leak_alias", "app.order"),
    ("app.web", "app.order"),
    ("app.web", "app.order.excel_io"),
})


def _relative_import_node(source: str) -> ast.ImportFrom:
    node = ast.parse(textwrap.dedent(source).strip()).body[0]
    assert isinstance(node, ast.ImportFrom), node
    return node


def test_module_names_and_relative_targets_follow_python_rules(tmp_path):
    """锁定解析语义：绝对路径 -> 模块名、``__init__.py``、各级相对 import。"""
    app_root = _make_synthetic_app(tmp_path)
    leak = app_root / "core" / "leak.py"
    web_init = app_root / "web" / "__init__.py"
    app_init = app_root / "__init__.py"

    assert _module_name(leak, app_root) == "app.core.leak"
    # __init__.py 是包本身，不是 app.web.__init__。/tmp 或仓库根运行结果相同。
    assert _module_name(web_init, app_root) == "app.web"

    def resolve(path: Path, source: str) -> str | None:
        return _resolve_relative_target(
            path, _relative_import_node(source), app_root)

    assert resolve(leak, "from . import models") == "app.core"
    assert resolve(leak, "from .models import Thing") == "app.core.models"
    assert resolve(leak, "from ..web import server") == "app.web"
    assert resolve(leak, "from ..web.server import dispatch") == "app.web.server"
    assert resolve(leak, "from app.web import server") == "app.web"
    assert resolve(web_init, "from ..order import excel_io") == "app.order"
    assert resolve(app_init, "from . import core") == "app"
    # 越过顶层包：Python 自身会 ImportError（attempted relative import beyond
    # top-level package），没有可解析的绝对目标。带 module 的写法同样为 None，
    # 否则不可执行代码里的 app.web 会被误报成依赖（独立验证发现的 off-by-one）。
    assert resolve(leak, "from ... import x") is None
    assert resolve(leak, "from ...app.web import server") is None
    assert resolve(app_init, "from .. import x") is None
    assert resolve(app_init, "from ..x import y") is None


def test_relative_imports_are_checked_from_any_process_cwd(tmp_path, monkeypatch):
    """R6-7 回归（进程内）：从非仓库根目录执行时相对 import 仍被检查。

    旧实现 ``_module_name(Path(str(node)).resolve())``：CWD 在仓库根时得到
    无意义模块名 → 相对 import 全部静默放过；CWD 在别处时 ``relative_to``
    直接抛 ``ValueError``。这里同时断言「非法相对 import 被抓到」和
    「扫描结果与 CWD 无关」。
    """
    app_root = _make_synthetic_app(tmp_path)
    foreign_cwd = tmp_path / "foreign-cwd" / "empty"
    foreign_cwd.mkdir(parents=True)

    monkeypatch.chdir(foreign_cwd)
    assert Path.cwd() == foreign_cwd != REPO_ROOT
    from_foreign_cwd = _forbidden_dependency_violations(app_root)

    monkeypatch.chdir(REPO_ROOT)
    from_repo_root = _forbidden_dependency_violations(app_root)

    assert from_foreign_cwd == from_repo_root, (
        "扫描结果随进程 CWD 变化：\n"
        f"CWD={foreign_cwd}: {from_foreign_cwd}\n"
        f"CWD={REPO_ROOT}: {from_repo_root}")
    assert from_foreign_cwd, "相对 import 完全没被检查（回归）"

    # 精确锁定整棵合成树的期望违规集合（含裸子模块展开出的 pkg.sub 目标）。
    assert _violation_pairs(from_foreign_cwd) == _EXPECTED_SYNTHETIC_VIOLATIONS, (
        sorted(_violation_pairs(from_foreign_cwd)))
    assert not [v for v in from_foreign_cwd if "safe" in v], from_foreign_cwd


def test_bare_submodule_imports_are_checked_against_the_scanned_tree(tmp_path):
    """R8 缺口回归：``from pkg import sub`` / ``from .. import sub``（含 ``as``）按子模块拦。

    旧行为只产出 ``pkg``，``from app import order`` 这类写法直接绕过边界锁；
    现在只有在**扫描树里真实存在** ``pkg.sub`` 时才展开，且展开后同样走规则表。
    """
    app_root = _make_synthetic_app(tmp_path)
    pairs = _violation_pairs(_forbidden_dependency_violations(app_root))

    # 三种同形写法都必须被抓到（绝对、绝对+as、相对+as）。
    assert ("app.core.leak_alias", "app.web") in pairs, sorted(pairs)
    assert ("app.core.leak_alias", "app.api") in pairs, sorted(pairs)
    assert ("app.core.leak_alias", "app.order") in pairs, sorted(pairs)

    # 合法方向上的同形写法（web -> api、api 的子模块）不得误报。
    assert not [p for p in pairs if p[0] in
                ("app.web.safe_web_alias", "app.web.safe_web")], sorted(pairs)

    # 裸子模块必须解析成真实模块名，而不是停在包上。
    assert _known_modules(app_root) >= {
        "app", "app.core", "app.core.models", "app.web", "app.web.server",
        "app.api", "app.api.bridge", "app.order", "app.order.excel_io"}


def test_package_symbol_imports_are_not_treated_as_submodules(tmp_path):
    """要求 2：类/函数/常量不能被当成子模块；展开只认文件树里真实存在的模块。

    全程不 import 任何生产模块：``_known_modules`` 由 ``rglob('*.py')`` + 模块名
    前缀静态推导，``_iter_imports`` 只做 AST 解析。
    """
    app_root = _make_synthetic_app(tmp_path)
    targets = {t for _node, t in
               _iter_imports(app_root / "core" / "leak_alias.py", app_root)}

    assert targets == {"app", "app.web", "app.api", "app.order",
                       "app.core", "app.core.models"}, sorted(targets)
    # 下面几个名字都不是模块/包，不能出现在目标里。
    assert "app.__version__" not in targets
    assert "app.core.SOME_CONSTANT" not in targets
    assert "app.core.models.Thing" not in targets

    known = _known_modules(app_root)
    assert {"app", "app.core", "app.core.models", "app.web.server"} <= known
    assert not [name for name in known
                if name.endswith((".Thing", ".SOME_CONSTANT", ".__version__"))], sorted(known)


def test_relative_imports_are_checked_in_a_subprocess_with_foreign_cwd(tmp_path):
    """R6-7 回归（端到端）：真子进程 + 真非仓库 CWD 下仍能抓到相对跨层 import。

    子进程以空目录为 CWD，用绝对路径加载本测试文件，再扫描合成 ``app/`` 树。
    这正是验收命令 ``cd /tmp && pytest <abs path>`` 所覆盖的运行姿态，
    区别是这里不看“有没有报错”，而看“相对 import 到底有没有被检查”。
    """
    app_root = _make_synthetic_app(tmp_path)
    foreign_cwd = tmp_path / "subprocess-cwd"
    foreign_cwd.mkdir()
    module_path = Path(__file__).resolve()

    script = textwrap.dedent(f"""
        import importlib.util, json, pathlib, sys

        spec = importlib.util.spec_from_file_location(
            "_arch_boundaries_under_test", {str(module_path)!r})
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        print(json.dumps({{
            "cwd": str(pathlib.Path.cwd()),
            "violations": module._forbidden_dependency_violations(
                pathlib.Path({str(app_root)!r})),
        }}))
    """)
    env = dict(os.environ, PYTHONPATH=str(REPO_ROOT))
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=foreign_cwd, env=env, capture_output=True, text=True, check=False,
    )
    assert completed.returncode == 0, completed.stderr

    payload = json.loads(completed.stdout.strip().splitlines()[-1])
    assert payload["cwd"] == str(foreign_cwd), payload
    assert payload["cwd"] != str(REPO_ROOT), payload

    violations = payload["violations"]
    assert _violation_pairs(violations) == _EXPECTED_SYNTHETIC_VIOLATIONS, violations
    assert not [v for v in violations if "safe" in v], violations
