#!/usr/bin/env python3
"""R6-4 变异敏感性验证：M4 / M6 / M8 三个盲区必须能被独立探针抓到。

对应 ``docs/OPTIMIZATION-FINAL-ACCEPTANCE-R7.md`` §5.2 的任务 T2。三个变异都
只注入到 **本次临时副本** 的 ``app/`` 源码上，工作区业务代码保持字节不变
（脚本结束前会复核 SHA256）。每个变异严格跑三段：

1. **before**   —— 正常实现：探针必须 exit 0 且不出现 DEFECT；
2. **injected** —— 注入错误（把修复还原成缺陷形态）：探针必须 exit ≠ 0 且
   报告对应场景的 DEFECT；
3. **restored** —— 在同一个临时目录里把被改文件恢复为原始字节：探针必须再次
   exit 0。

每个变异使用**独立临时目录**（副本 + TMPDIR + 批次锁根 + 用户数据根全部隔离），
注入前做**锚点唯一性检查**（原文必须恰好出现 1 次，且失败形态原本不存在）。

    python3 tools/r7-acceptance/mutation_check_r6_4.py
    python3 tools/r7-acceptance/mutation_check_r6_4.py --only m4 --json /tmp/r64.json
    python3 tools/r7-acceptance/mutation_check_r6_4.py --list

退出码：0 = 所有选中变异三段全部符合预期；1 = 任一阶段不符合（含锚点不唯一）。
不会真实 POST、不写真实 WPS、不读真实凭据、不修改工作区业务代码。
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PROBE = "tests/independent_final_counterexample_probe.py"
DEFAULT_LOG_DIR = REPO / "tools" / "r7-acceptance" / "logs" / "mutation-r6-4"

CASES: list[dict] = [
    {
        "id": "m4",
        "title": "M4：load_journal 把 JSON 语法损坏当成空 journal",
        "scenario": "JSON 语法损坏 journal 被当作空状态放行",
        "aliases": ["m4-json-corrupt"],
        "expect_details": [
            'authoritative_syntax_garbage:rc=0,post=1',
            'bytes_unchanged=False',
        ],
        "edits": [{
            "file": "app/ordering/uncertain.py",
            "old": (
                "    try:\n"
                "        payload = json.loads(target.read_text(encoding=\"utf-8\"))\n"
                "    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:\n"
                "        raise UncertainJournalError(f\"本地不确定记录损坏，拒绝继续下单：{target}（{exc}）\") from exc\n"
                "    return _validate_journal_payload(payload, target)\n"
            ),
            "new": (
                "    try:\n"
                "        payload = json.loads(target.read_text(encoding=\"utf-8\"))\n"
                "    except (OSError, UnicodeDecodeError, json.JSONDecodeError):\n"
                "        # MUTATION(R6-4/M4)：语法损坏被当作空 journal 放行。\n"
                "        return {\"version\": 1, \"records\": []}\n"
                "    return _validate_journal_payload(payload, target)\n"
            ),
            "catches": (
                "损坏/半截/非 UTF-8 的 journal 不再 fail-closed：探针 6 个变体里"
                "会出现 POST=1（合法订单被真实发出），且损坏文件被覆盖。"
            ),
        }],
    },
    {
        "id": "m6",
        "title": "M6：POST 超时/断线被当成“明确失败、允许直接重发”",
        "scenario": "POST 超时/断线被当作明确失败并允许盲目重发",
        "aliases": ["m6-post-timeout"],
        "expect_details": [
            'timeout:journal_lost_active_uncertain_record',
            'timeout:blind_resend_post=2',
            'connection_reset:blind_resend_post=2',
        ],
        "edits": [{
            "file": "app/ordering/submission.py",
            "old": (
                "    except ApiError as exc:\n"
                "        if _is_auth_expired(exc):\n"
                "            raise _AuthExpired(str(exc)) from exc\n"
                "        raise _SubmissionUncertain(str(exc)) from exc\n"
                "    except Exception as exc:\n"
                "        detail = str(exc) or type(exc).__name__\n"
                "        raise _SubmissionUncertain(detail) from exc\n"
            ),
            "new": (
                "    except ApiError as exc:\n"
                "        if _is_auth_expired(exc):\n"
                "            raise _AuthExpired(str(exc)) from exc\n"
                "        # MUTATION(R6-4/M6)：断线/超时当成“明确失败”，可重发。\n"
                "        raise _ExplicitRejection(str(exc)) from exc\n"
                "    except Exception as exc:\n"
                "        detail = str(exc) or type(exc).__name__\n"
                "        # MUTATION(R6-4/M6)：同上。\n"
                "        raise _ExplicitRejection(detail) from exc\n"
            ),
            "catches": (
                "超时/断线被降级为显式失败后，本地记录被 discard，第二次运行不再"
                "阻断而是真的又发了一次 POST（合成平台计数 1 → 2）。"
            ),
        }],
    },
    {
        "id": "m8-always-true",
        "title": "M8a：批次匹配函数恒 True（完全忽略批次范围）",
        "scenario": "批次范围（日期/账号/平台）隔离失效",
        "aliases": ["m8-batch-scope"],
        "expect_details": [
            'other_scope_should_not_block:rc=0,post=0',
        ],
        "edits": [{
            "file": "app/ordering/uncertain.py",
            "old": (
                "    raw_batch = str(record.get(\"batch_key\") or \"\")\n"
                "    key_text = str(key or \"\")\n"
                "    if raw_batch and raw_batch == key_text:\n"
                "        return True\n"
                "    parts = key_text.split(\"|\")\n"
                "    if len(parts) == 2:\n"
                "        date = parts[0]\n"
                "        account = normalise_account(parts[1])\n"
                "    elif len(parts) == 3:  # 调用方仍传旧键\n"
                "        date = parts[0]\n"
                "        account = normalise_account(parts[2])\n"
                "    else:\n"
                "        return False\n"
                "    if not date or not account:\n"
                "        return False\n"
                "    stored_date = str(record.get(\"delivery_date\") or \"\")\n"
                "    stored_account = normalise_account(record.get(\"account\"))\n"
                "    if stored_date and stored_account and stored_date == date and stored_account == account:\n"
                "        return True\n"
                "    old_parts = raw_batch.split(\"|\")\n"
                "    return (len(old_parts) == 3 and old_parts[0] == date\n"
                "            and normalise_account(old_parts[2]) == account)\n"
            ),
            "new": (
                "    # MUTATION(R6-4/M8a)：批次匹配恒真，其他账号/日期一并命中。\n"
                "    return True\n"
            ),
            "catches": (
                "其他账号/其他日期的未决记录被当成当前批次 → 合法新订单被错误"
                "阻断（探针 A 场景 POST=1 变 POST=0）。"
            ),
        }],
    },
    {
        "id": "m8-ignore-range",
        "title": "M8b：批次匹配只按日期或账号任一命中（忽略完整批次范围）",
        "scenario": "批次范围（日期/账号/平台）隔离失效",
        "aliases": ["m8-batch-scope"],
        "expect_details": [
            'other_scope_should_not_block:rc=0,post=0',
        ],
        "edits": [{
            "file": "app/ordering/uncertain.py",
            "old": (
                "    raw_batch = str(record.get(\"batch_key\") or \"\")\n"
                "    key_text = str(key or \"\")\n"
                "    if raw_batch and raw_batch == key_text:\n"
                "        return True\n"
                "    parts = key_text.split(\"|\")\n"
                "    if len(parts) == 2:\n"
                "        date = parts[0]\n"
                "        account = normalise_account(parts[1])\n"
                "    elif len(parts) == 3:  # 调用方仍传旧键\n"
                "        date = parts[0]\n"
                "        account = normalise_account(parts[2])\n"
                "    else:\n"
                "        return False\n"
                "    if not date or not account:\n"
                "        return False\n"
                "    stored_date = str(record.get(\"delivery_date\") or \"\")\n"
                "    stored_account = normalise_account(record.get(\"account\"))\n"
                "    if stored_date and stored_account and stored_date == date and stored_account == account:\n"
                "        return True\n"
                "    old_parts = raw_batch.split(\"|\")\n"
                "    return (len(old_parts) == 3 and old_parts[0] == date\n"
                "            and normalise_account(old_parts[2]) == account)\n"
            ),
            "new": (
                "    # MUTATION(R6-4/M8b)：日期或账号任一命中即算同批。\n"
                "    parts = str(key or \"\").split(\"|\")\n"
                "    if not parts:\n"
                "        return False\n"
                "    date = parts[0]\n"
                "    account = normalise_account(parts[-1])\n"
                "    stored_date = str(record.get(\"delivery_date\") or \"\")\n"
                "    stored_account = normalise_account(record.get(\"account\"))\n"
                "    old_parts = str(record.get(\"batch_key\") or \"\").split(\"|\")\n"
                "    return bool((date and date in {stored_date, old_parts[0] if old_parts else ''})\n"
                "                or (account and account in {\n"
                "                    stored_account,\n"
                "                    normalise_account(old_parts[-1]) if old_parts else ''}))\n"
            ),
            "catches": (
                "只要日期或账号任一相同就命中：其他账号（同日期）或其他日期"
                "（同账号）的记录都会错误阻断本批合法新订单。"
            ),
        }],
    },
    {
        "id": "m8-always-false",
        "title": "M8c：批次匹配函数恒 False（反而漏掉同批未决记录）",
        "scenario": "批次范围（日期/账号/平台）隔离失效",
        "aliases": ["m8-batch-scope"],
        "expect_details": [
            'same_batch_legacy_key_should_block:rc=0,post=1',
        ],
        "edits": [{
            "file": "app/ordering/uncertain.py",
            "old": (
                "    raw_batch = str(record.get(\"batch_key\") or \"\")\n"
                "    key_text = str(key or \"\")\n"
                "    if raw_batch and raw_batch == key_text:\n"
                "        return True\n"
                "    parts = key_text.split(\"|\")\n"
                "    if len(parts) == 2:\n"
                "        date = parts[0]\n"
                "        account = normalise_account(parts[1])\n"
                "    elif len(parts) == 3:  # 调用方仍传旧键\n"
                "        date = parts[0]\n"
                "        account = normalise_account(parts[2])\n"
                "    else:\n"
                "        return False\n"
                "    if not date or not account:\n"
                "        return False\n"
                "    stored_date = str(record.get(\"delivery_date\") or \"\")\n"
                "    stored_account = normalise_account(record.get(\"account\"))\n"
                "    if stored_date and stored_account and stored_date == date and stored_account == account:\n"
                "        return True\n"
                "    old_parts = raw_batch.split(\"|\")\n"
                "    return (len(old_parts) == 3 and old_parts[0] == date\n"
                "            and normalise_account(old_parts[2]) == account)\n"
            ),
            "new": (
                "    # MUTATION(R6-4/M8c)：批次匹配恒假，同批未决记录被漏掉。\n"
                "    return False\n"
            ),
            "catches": (
                "同账号同日期的未决记录（含旧三段 batch_key）不再命中 → 本批"
                "直接放行并真的又发了一次 POST（探针 B 场景 POST=0 变 1）。"
            ),
        }],
    },
]

_IGNORED = shutil.ignore_patterns(
    ".git", "node_modules", "dist", "__pycache__", ".pytest_cache",
    ".ruff_cache", ".venv", "venv")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _copy_repo(dest: Path) -> None:
    shutil.copytree(REPO, dest, ignore=_IGNORED)


def _apply_edits(copy_dir: Path, case: dict) -> list[dict]:
    """按锚点注入变异；锚点必须唯一，且失败形态原本不存在。"""
    applied: list[dict] = []
    for edit in case["edits"]:
        target = copy_dir / edit["file"]
        original = target.read_text(encoding="utf-8")
        count = original.count(edit["old"])
        if count != 1:
            raise AssertionError(
                f"{case['id']}:{edit['file']} 锚点不唯一（出现 {count} 次），"
                "拒绝注入")
        if edit["new"] in original:
            raise AssertionError(
                f"{case['id']}:{edit['file']} 变异形态原本已存在，变异无意义")
        mutated = original.replace(edit["old"], edit["new"], 1)
        if mutated == original or edit["old"] in mutated:
            raise AssertionError(f"{case['id']}:{edit['file']} 变异未生效")
        target.write_text(mutated, encoding="utf-8")
        applied.append({
            "file": edit["file"],
            "anchor_line": original[:original.index(edit["old"])].count("\n") + 1,
            "anchor_occurrences": count,
            "original_sha256": hashlib.sha256(
                original.encode("utf-8")).hexdigest(),
            "mutated_sha256": hashlib.sha256(
                mutated.encode("utf-8")).hexdigest(),
            "original_text": original,
        })
    return applied


def _restore_edits(copy_dir: Path, applied: list[dict]) -> None:
    for item in applied:
        (copy_dir / item["file"]).write_text(item["original_text"],
                                             encoding="utf-8")
        del item["original_text"]


def _probe_env(case_root: Path, copy_dir: Path) -> dict[str, str]:
    """每个变异独立的 TMPDIR / 锁根 / 用户数据根 / XDG 状态根。"""
    env = dict(os.environ)
    env.update({
        "PYTHONPATH": f"{copy_dir}{os.pathsep}{copy_dir / 'tests'}",
        "TMPDIR": str(case_root / "tmp"),
        "YIKOU_SSS_LOCK_ROOT": str(case_root / "locks"),
        "YIKOU_SSS_AUTHORITATIVE_ROOT": str(case_root / "authoritative-root"),
        "YIKOU_DATA_DIR": str(case_root / "userdata"),
        "XDG_STATE_HOME": str(case_root / "state"),
        "PYTHONDONTWRITEBYTECODE": "1",
    })
    return env


def _run_probe(copy_dir: Path, case: dict, case_root: Path) -> subprocess.CompletedProcess:
    env = _probe_env(case_root, copy_dir)
    for key in ("TMPDIR", "YIKOU_SSS_LOCK_ROOT", "YIKOU_DATA_DIR",
                "XDG_STATE_HOME"):
        Path(env[key]).mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, str(copy_dir / PROBE)]
    for alias in case["aliases"]:
        cmd += ["--only", alias]
    return subprocess.run(cmd, cwd=str(copy_dir), env=env, capture_output=True,
                          text=True, timeout=900)


def _defect_lines(stdout: str) -> list[str]:
    return [line for line in stdout.splitlines() if line.startswith("DEFECT")]


def _write_log(log_dir: Path, case_id: str, phase: str,
               proc: subprocess.CompletedProcess) -> str:
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / f"{case_id}.{phase}.log"
    path.write_text(
        f"$ # phase={phase} exit={proc.returncode}\n"
        f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}\n",
        encoding="utf-8")
    return str(path)


def run_case(case: dict, log_dir: Path | None, keep: bool) -> dict:
    case_root = Path(tempfile.mkdtemp(prefix=f"r6-4-{case['id']}-"))
    result: dict = {
        "id": case["id"], "title": case["title"],
        "scenario": case["scenario"], "aliases": case["aliases"],
        "temp_dir": str(case_root),
        "case_root": str(case_root),
        "edits": [{"file": edit["file"]} for edit in case["edits"]],
        "expect_details": list(case.get("expect_details") or []),
        "phases": {}, "anchor_check": {}, "ok": False,
    }
    try:
        copy_dir = case_root / "repo"
        _copy_repo(copy_dir)
        workspace_hashes = {edit["file"]: _sha256(REPO / edit["file"])
                            for edit in case["edits"]}

        before = _run_probe(copy_dir, case, case_root)
        result["phases"]["before"] = {
            "exit": before.returncode,
            "defect_lines": _defect_lines(before.stdout),
            "log": _write_log(log_dir, case["id"], "before", before)
            if log_dir else "",
        }

        applied = _apply_edits(copy_dir, case)
        result["anchor_check"] = {
            f"{item['file']}:{item['anchor_line']}": item["anchor_occurrences"]
            for item in applied}
        result["mutation_sites"] = [
            {"file": item["file"], "anchor_line": item["anchor_line"],
             "original_sha256": item["original_sha256"],
             "mutated_sha256": item["mutated_sha256"]}
            for item in applied]
        injected = _run_probe(copy_dir, case, case_root)
        details_found = [detail for detail in result["expect_details"]
                         if detail in injected.stdout]
        result["phases"]["injected"] = {
            "exit": injected.returncode,
            "defect_lines": _defect_lines(injected.stdout),
            "details_found": details_found,
            "log": _write_log(log_dir, case["id"], "injected", injected)
            if log_dir else "",
        }

        _restore_edits(copy_dir, applied)
        result["restore_check"] = {
            item["file"]: _sha256(copy_dir / item["file"]) for item in applied}
        restored = _run_probe(copy_dir, case, case_root)
        result["phases"]["restored"] = {
            "exit": restored.returncode,
            "defect_lines": _defect_lines(restored.stdout),
            "log": _write_log(log_dir, case["id"], "restored", restored)
            if log_dir else "",
        }

        result["workspace_unchanged"] = all(
            _sha256(REPO / path) == digest
            for path, digest in workspace_hashes.items())

        expected_defect = f"DEFECT   {case['scenario']}"
        result["expected_defect"] = expected_defect
        ok = True
        reasons: list[str] = []
        if result["phases"]["before"]["exit"] != 0 \
                or result["phases"]["before"]["defect_lines"]:
            ok = False
            reasons.append("before 阶段正常实现未通过")
        if result["phases"]["injected"]["exit"] == 0:
            ok = False
            reasons.append("injected 阶段退出码仍为 0")
        if expected_defect not in result["phases"]["injected"]["defect_lines"]:
            ok = False
            reasons.append("injected 阶段未报告预期 DEFECT")
        missing_details = [detail for detail in result["expect_details"]
                           if detail not in details_found]
        if missing_details:
            ok = False
            reasons.append(f"injected 阶段缺少行为学差异证据：{missing_details}")
        if result["phases"]["restored"]["exit"] != 0 \
                or result["phases"]["restored"]["defect_lines"]:
            ok = False
            reasons.append("restored 阶段未恢复通过")
        if not result["workspace_unchanged"]:
            ok = False
            reasons.append("工作区业务文件被改动")
        result["reasons"] = reasons
        result["ok"] = ok
    finally:
        if not keep:
            shutil.rmtree(case_root, ignore_errors=True)
            result["case_root"] = ""
    return result


def _select(cases: list[dict], only: list[str]) -> list[dict]:
    if not only:
        return cases
    selected: list[dict] = []
    for case in cases:
        for token in only:
            if case["id"] == token or case["id"].startswith(f"{token}-") \
                    or token in case["aliases"]:
                selected.append(case)
                break
    return selected


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="R6-4 M4/M6/M8 变异敏感性验证（只改临时副本）")
    parser.add_argument("--only", default="",
                        help="逗号分隔的变异 id/别名，例如 m4,m6,m8")
    parser.add_argument("--json", default="",
                        help="把机器可读的汇总写入该路径")
    parser.add_argument("--log-dir", default=str(DEFAULT_LOG_DIR),
                        help="每个阶段 stdout/stderr 的日志目录")
    parser.add_argument("--no-logs", action="store_true",
                        help="不写日志文件")
    parser.add_argument("--keep", action="store_true",
                        help="保留临时副本以便排查")
    parser.add_argument("--list", action="store_true", help="列出变异用例")
    args = parser.parse_args(argv)

    if args.list:
        for case in CASES:
            print(f"{case['id']}\t{case['title']}\t场景={case['scenario']}")
        return 0

    only = [token.strip() for token in args.only.split(",") if token.strip()]
    selected = _select(CASES, only)
    if not selected:
        print(f"没有匹配的变异：{only}")
        return 1
    log_dir = None if args.no_logs else Path(args.log_dir)

    print("=== R6-4 变异敏感性验证（M4/M6/M8） ===")
    print(f"工作区只读：{REPO}；每个变异独立临时目录 + 独立 TMPDIR/锁根/数据根")
    results: list[dict] = []
    for case in selected:
        print(f"\n--- {case['id']}: {case['title']} ---")
        result = run_case(case, log_dir, args.keep)
        results.append(result)
        anchors = ", ".join(f"{path}×{count}"
                            for path, count in result["anchor_check"].items())
        sites = ", ".join(f"{site['file']}:{site['anchor_line']}"
                          for site in result["mutation_sites"])
        print(f"  变异位置：{sites}")
        print(f"  锚点唯一性：{anchors}")
        for phase in ("before", "injected", "restored"):
            info = result["phases"][phase]
            print(f"  {phase:<8} exit={info['exit']} "
                  f"defects={info['defect_lines']}")
        print(f"  注入后行为学差异证据："
              f"{result['phases']['injected']['details_found']}")
        print(f"  期望 DEFECT：{result['expected_defect']}")
        print(f"  工作区未改动：{result['workspace_unchanged']}")
        print(f"  判定：{'OK' if result['ok'] else 'BAD'} "
              f"{'；'.join(result['reasons'])}")

    bad = [result["id"] for result in results if not result["ok"]]
    summary = {
        "generated_at": _dt.datetime.now().isoformat(timespec="seconds"),
        "workspace": str(REPO),
        "probe": PROBE,
        "cases": results,
        "ok": not bad,
    }
    if args.json:
        Path(args.json).write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n汇总：{len(results) - len(bad)}/{len(results)} OK"
          + (f"；失败：{bad}" if bad else ""))
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
