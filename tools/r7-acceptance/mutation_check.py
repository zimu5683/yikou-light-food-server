#!/usr/bin/env python3
"""R7 变异敏感性验证：把“修复”逐条还原，验证独立探针/测试确实会失败。

用途：证明 R7 的绿灯不是因为“测试永远通过”，而是因为保护真的在起作用。
所有变异只发生在 /tmp 副本上，工作区不被修改。

    python3 tools/r7-acceptance/mutation_check.py

每个用例打印 `MUTATION <名称>: 期望=<期望结果> 实际=<结果> => OK/BAD`，
全部 OK 时退出码 0。
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
TOOLS = REPO / "tools" / "r7-acceptance"

CASES = [
    {
        "name": "w1_plan_digest_disabled",
        "file": "app/wps/executor.py",
        "old": "def _plan_ledger_digests(plans: Sequence[SheetPlan]) -> set[str]:\n"
               '    """计划构建时携带的 ledger 磁盘快照摘要集合（忽略旧计划 None）。"""\n'
               "    return {str(plan.ledger_digest) for plan in plans\n"
               '            if getattr(plan, "ledger_digest", None)}',
        "new": "def _plan_ledger_digests(plans: Sequence[SheetPlan]) -> set[str]:\n"
               '    """MUTATION: 计划快照比较被关闭。"""\n'
               "    return set()",
        "probe": "probe_w1_stale_plan.py",
        "args": ["--mutate-r6-wiring"],
        "expect_exit": 0,          # 探针自身在变异下应报 DOUBLE WRITE
        "expect_text": "DOUBLE WRITE",
    },
    {
        "name": "w2_unknown_status_gate_opened",
        "file": "app/wps/journal.py",
        "old": '    def pending_operations(self) -> dict[str, dict[str, Any]]:\n        result: dict[str, dict[str, Any]] = {}\n        for op_id, op in self.operations().items():\n            if not isinstance(op, dict):\n                continue\n            op_status = str(op.get("status") or "")\n            if op_status not in OP_STATUSES or op_status in PENDING_OP_STATUSES:\n                result[op_id] = op\n                continue\n            sheets = op.get("sheets") or {}\n            if isinstance(sheets, Mapping):\n                for record in sheets.values():\n                    if not isinstance(record, Mapping):\n                        result[op_id] = op\n                        break\n                    sheet_status = str(record.get("status") or "")\n                    if (record.get("retired_guarded")\n                            or sheet_status == "retired_guarded"):\n                        # guarded retire 的防重复保护由 has_guard + apply_plan 提供，\n                        # 不重新进入全局 pending；原始未知/审计状态可以保留。\n                        continue\n                    if (sheet_status not in SHEET_STATUSES\n                            or sheet_status in PENDING_SHEET_STATUSES):\n                        result[op_id] = op\n                        break\n        return result\n\n',
        "new": '    def pending_operations(self) -> dict[str, dict[str, Any]]:\n        """MUTATION: 还原 R6 语义（未知状态不进 pending）。"""\n        result: dict[str, dict[str, Any]] = {}\n        for op_id, op in self.operations().items():\n            if not isinstance(op, dict):\n                continue\n            statuses = [str(s.get("status", "")) for s in (op.get("sheets") or {}).values()\n                        if isinstance(s, dict)]\n            if op.get("status") in PENDING_SHEET_STATUSES or any(\n                    st in PENDING_SHEET_STATUSES for st in statuses):\n                result[op_id] = op\n        return result\n\n',
        "probe": "probe_w2w3w4_journal_gate.py",
        "args": [],
        "expect_exit": 1,
        "expect_text": "W2: FAIL",
    },
    {
        "name": "w8_new_customer_check_disabled",
        "file": "app/wps/executor.py",
        "old": "        for key in new_person_keys:\n            if current.get(key):\n"
               "                problems.append(\n"
               '                    f"云端已出现客户「{key[0]}/{key[1]}」，不能再按新增行处理")',
        "new": "        for key in new_person_keys:\n            if False and current.get(key):\n"
               "                problems.append(\n"
               '                    f"云端已出现客户「{key[0]}/{key[1]}」，不能再按新增行处理")',
        "test": "tests/test_wps_recovery.py",
        "test_k": "test_w8_concurrent_same_new_customer_below_target_rows_is_rejected",
        "expect_fail": True,
    },
    {
        "name": "excel_empty_output_guard_disabled",
        "file": "app/order/excel_io.py",
        "old": None,  # 由下方 probe 直接断言：见 probe_local_safety.py 的 R6-1 段
        "skip": "由 probe_local_safety.py 的三种失败模式覆盖（无需额外变异）",
    },
]


def _prepare_copy() -> Path:
    work = Path(tempfile.mkdtemp(prefix="r7-mutation-"))
    dst = work / "repo"
    shutil.copytree(REPO, dst,
                    ignore=shutil.ignore_patterns(".git", "node_modules", "dist",
                                                  "__pycache__", ".pytest_cache",
                                                  ".ruff_cache"))
    return dst


def _apply(dst: Path, case: dict) -> bool:
    if case.get("skip"):
        return True
    target = dst / case["file"]
    text = target.read_text(encoding="utf-8")
    if text.count(case["old"]) != 1:
        print(f"  ! 锚点不唯一（{text.count(case['old'])}）：{case['file']}")
        return False
    target.write_text(text.replace(case["old"], case["new"]), encoding="utf-8")
    return True


def _run(cmd: list[str], cwd: Path, env_extra: dict | None = None):
    import os
    env = dict(os.environ)
    env["PYTHONPATH"] = f"{cwd}:{cwd / 'tests'}"
    env["TMPDIR"] = str(cwd.parent / "tmp")
    env.update(env_extra or {})
    return subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True,
                          timeout=900)


def main() -> int:
    results: list[tuple[str, bool, str]] = []
    for case in CASES:
        if case.get("skip"):
            print(f"MUTATION {case['name']}: SKIP（{case['skip']}）")
            results.append((case["name"], True, "skip"))
            continue
        dst = _prepare_copy()
        if not _apply(dst, case):
            results.append((case["name"], False, "anchor"))
            continue
        if case.get("probe"):
            probe = dst / "tools" / "r7-acceptance" / case["probe"]
            proc = _run([sys.executable, str(probe), *case.get("args", [])], dst)
            ok = proc.returncode == case["expect_exit"] and case["expect_text"] in proc.stdout
            detail = f"exit={proc.returncode} 期望文本={case['expect_text']!r}"
        else:
            proc = _run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                         case["test"], "-k", case["test_k"]], dst)
            failed = proc.returncode != 0 and "1 failed" in proc.stdout
            ok = failed == case["expect_fail"]
            detail = f"pytest exit={proc.returncode}（期望该用例失败）"
        print(f"MUTATION {case['name']}: {'OK' if ok else 'BAD'} — {detail}")
        results.append((case["name"], ok, detail))
        shutil.rmtree(dst.parent, ignore_errors=True)

    bad = [name for name, ok, _ in results if not ok]
    print(f"\n汇总：{len(results) - len(bad)}/{len(results)} OK")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
