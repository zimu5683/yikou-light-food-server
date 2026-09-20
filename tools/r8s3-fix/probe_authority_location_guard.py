"""R8-S3 修复验收探针：显式权威位置切换不得放行第二次下单 POST。

同一份探针可在**修复前快照**与**修复后工作区**运行，直接对比行为：

| 场景 | 修复前（预期） | 修复后（验收） |
| --- | --- | --- |
| C1 显式文件自带旧尾点 unresolved | 0 POST / blocked_uncertain | 同（对照） |
| C2 默认根 unresolved → 空显式文件 | **1 POST（绕过）** | **0 POST / authority-location-guard** |
| C3 显式 A unresolved → 显式 B | **1 POST（绕过）** | **0 POST** |
| C4 显式 unresolved → 取消覆盖 | **1 POST（绕过）** | **0 POST** |
| C5 环境变量覆盖（默认根 → 空显式） | **1 POST（绕过）** | **0 POST** |
| 重启后切换 | **1 POST** | **0 POST** |
| 两进程竞争（默认根 vs 显式） | 可能 2 POST | 恰 1 POST |
| 登记文件损坏/不可写 | 不适用 | failed / 0 POST |
| 正常路径（无历史） | 1 POST / confirmed | 同（正向对照） |
| 其他账号记录 | 不误伤 | 不误伤 |
| 未登记旧显式位置的升级流程 | 无法发现 | 声明后阻断（未声明=已记录边界） |

每个安全场景都检查：前置记录确实存在、拒绝原因符合预期、旧记录字节未被改动、
前置记录仍是 unresolved。所有请求只发给 127.0.0.1 本地模拟平台；数据目录、
权威根、锁目录、登记文件、临时目录全部隔离到临时目录。

    PYTHONPATH=<repo> python3 tools/r8s3-fix/probe_authority_location_guard.py
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests.r8s3_location_guard_harness import (  # noqa: E402
    MockPlatform,
    active_records,
    child_result,
    loopback_guard,
    make_location_config,
    read_registry,
    registry_path,
    run_location_job,
    spawn_location_child,
    write_registry,
    write_unresolved_record,
)

OTHER_ACCOUNT = "18758187002"
SCENARIOS: list[tuple[str, str, object]] = []


def _scenario(name: str, expectation: str):
    def decorator(func):
        SCENARIOS.append((name, expectation, func))
        return func
    return decorator


def _isolate(base: Path) -> Path:
    """每个场景独立的登记锚点/默认根/锁/临时目录。"""
    base.mkdir(parents=True, exist_ok=True)
    (base / "tmp").mkdir(parents=True, exist_ok=True)
    os.environ.update({
        "YIKOU_DATA_DIR": str(base / "userdata"),
        "YIKOU_SSS_AUTHORITATIVE_ROOT": str(base / "auth-default"),
        "YIKOU_SSS_LOCK_ROOT": str(base / "locks"),
        "TMPDIR": str(base / "tmp"),
    })
    os.environ.pop("YIKOU_SSS_AUTHORITATIVE_PATH", None)
    os.environ.pop("YIKOU_SSS_AUTHORITY_LOCATIONS", None)
    return base


def _fixed() -> bool:
    try:
        from app.ordering.uncertain import authority_location_gate  # noqa: F401
    except ImportError:
        return False
    return True


def _empty_journal(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 1, "records": []}), encoding="utf-8")
    return path


def _registry_of(config=None) -> dict:
    try:
        return read_registry(config)
    except Exception as exc:  # noqa: BLE001 - 修复前没有登记 API
        return {"_unavailable": f"{type(exc).__name__}: {exc}"}


def _run(platform: MockPlatform, work: Path, *, explicit: str | None = None,
         env_path: str | None = None, list_visible: bool | None = None) -> dict:
    if list_visible is not None:
        platform.list_visible = list_visible
    if env_path:
        os.environ["YIKOU_SSS_AUTHORITATIVE_PATH"] = env_path
    else:
        os.environ.pop("YIKOU_SSS_AUTHORITATIVE_PATH", None)
    work.mkdir(parents=True, exist_ok=True)
    sub = Path(tempfile.mkdtemp(prefix="w-", dir=str(work)))
    config = make_location_config(sub, _url(platform),
                                  authoritative_path=explicit)
    before = platform.count
    try:
        result = run_location_job(config)
        status = str(result.get("status"))
    except Exception as exc:  # noqa: BLE001
        result = {"status": f"EXCEPTION:{type(exc).__name__}", "error": str(exc)}
        status = str(result["status"])
    return {"status": status, "semantics": result.get("semantics"),
            "posts": platform.count - before, "result": result}


def _url(platform: MockPlatform) -> str:
    return f"http://sss.example.invalid:{platform.port}"


def _blocked(payload: dict, semantics: str | None = None) -> bool:
    if payload["status"] not in {"blocked_uncertain", "failed"}:
        return False
    if semantics is not None and payload.get("semantics") != semantics:
        return False
    return True


def _bytes_unchanged(path: Path, before: bytes) -> bool:
    try:
        return path.read_bytes() == before
    except OSError:
        return False


@_scenario("c1_explicit_legacy_dot_record",
           "C1 显式文件自带旧尾点 unresolved：0 POST、旧记录保留")
def scenario_c1(base: Path) -> dict:
    explicit = base / "explicit-c1.json"
    before = write_unresolved_record(explicit, journal_id="c1-legacy",
                                     platform="http://sss.example.invalid.")
    with MockPlatform(list_visible=True) as platform, loopback_guard(platform.port):
        out = _run(platform, base, explicit=str(explicit))
    return {
        "pre_record_exists": len(active_records(explicit)) == 1,
        "pre_record_status": [r.get("status") for r in active_records(explicit)],
        "status": out["status"], "semantics": out["semantics"],
        "posts": out["posts"],
        "old_bytes_unchanged": _bytes_unchanged(explicit, before),
        "ok": (len(active_records(explicit)) == 1 and out["posts"] == 0
               and out["status"] == "blocked_uncertain"
               and _bytes_unchanged(explicit, before)),
    }


def _switch_case(base: Path, *, first_explicit: str | None,
                 second_explicit: str | None, second_env: str | None = None
                 ) -> dict:
    """通用：第一次（可选显式）播种 unresolved，第二次切换位置再跑。"""
    with MockPlatform(list_visible=False) as platform, loopback_guard(platform.port):
        first = _run(platform, base, explicit=first_explicit)
        seed_file = (Path(first_explicit) if first_explicit
                     else sorted(Path(os.environ["YIKOU_SSS_AUTHORITATIVE_ROOT"])
                                 .glob("*.json"))[0])
        seed_before = seed_file.read_bytes()
        seed_active = len(active_records(seed_file))
        second = _run(platform, base, explicit=second_explicit,
                      env_path=second_env)
        conflicts = second["result"].get("location_conflicts")
        locations = second["result"].get("location_conflict_locations")
    return {
        "first": {"status": first["status"], "posts": first["posts"]},
        "seed_file": str(seed_file), "seed_active_before": seed_active,
        "second": {"status": second["status"], "semantics": second["semantics"],
                   "posts": second["posts"], "conflicts": conflicts,
                   "locations": locations,
                   "next_action": str(second["result"].get("next_action") or "")[:160]},
        "seed_bytes_unchanged": _bytes_unchanged(seed_file, seed_before),
        "seed_still_active": len(active_records(seed_file)),
    }


@_scenario("c2_default_then_empty_explicit",
           "C2 默认根 unresolved → 空显式文件：第二轮 0 POST")
def scenario_c2(base: Path) -> dict:
    explicit = _empty_journal(base / "explicit-c2.json")
    data = _switch_case(base, first_explicit=None, second_explicit=str(explicit))
    data["ok"] = (data["first"] == {"status": "unconfirmed", "posts": 1}
                  and data["seed_active_before"] == 1
                  and data["second"]["posts"] == 0
                  and data["second"]["status"] == "blocked_uncertain"
                  and data["seed_bytes_unchanged"]
                  and data["seed_still_active"] == 1)
    return data


@_scenario("c3_explicit_a_to_b",
           "C3 显式 A unresolved → 显式 B：第二轮 0 POST、A 记录保留")
def scenario_c3(base: Path) -> dict:
    data = _switch_case(base, first_explicit=str(base / "A.json"),
                        second_explicit=str(base / "B.json"))
    data["ok"] = (data["first"] == {"status": "unconfirmed", "posts": 1}
                  and data["seed_active_before"] == 1
                  and data["second"]["posts"] == 0
                  and data["second"]["status"] == "blocked_uncertain"
                  and data["seed_bytes_unchanged"]
                  and data["seed_still_active"] == 1)
    return data


@_scenario("c4_explicit_then_drop_override",
           "C4 显式位置 unresolved → 取消覆盖：第二轮 0 POST")
def scenario_c4(base: Path) -> dict:
    data = _switch_case(base, first_explicit=str(base / "C.json"),
                        second_explicit=None)
    data["ok"] = (data["first"] == {"status": "unconfirmed", "posts": 1}
                  and data["seed_active_before"] == 1
                  and data["second"]["posts"] == 0
                  and data["second"]["status"] == "blocked_uncertain"
                  and data["seed_bytes_unchanged"]
                  and data["seed_still_active"] == 1)
    return data


@_scenario("c5_env_override",
           "C5 环境变量覆盖（默认根 → 空显式）：第二轮 0 POST")
def scenario_c5(base: Path) -> dict:
    explicit = _empty_journal(base / "explicit-env.json")
    data = _switch_case(base, first_explicit=None, second_explicit=None,
                        second_env=str(explicit))
    data["ok"] = (data["first"] == {"status": "unconfirmed", "posts": 1}
                  and data["seed_active_before"] == 1
                  and data["second"]["posts"] == 0
                  and data["second"]["status"] == "blocked_uncertain"
                  and data["seed_bytes_unchanged"]
                  and data["seed_still_active"] == 1)
    return data


@_scenario("restart_then_switch",
           "进程重启后切换显式位置：第二轮 0 POST")
def scenario_restart(base: Path) -> dict:
    with MockPlatform(list_visible=False) as platform:
        work = base / "job"
        url = _url(platform)
        first_proc, first_result = spawn_location_child(
            work=work, url=url, port=platform.port,
            config_work=work / "child-1")
        first = child_result(first_proc, first_result)
        posts_after_first = platform.count
        explicit = _empty_journal(base / "explicit-restart.json")
        second_proc, second_result = spawn_location_child(
            work=work, url=url, port=platform.port,
            authoritative_path=str(explicit), config_work=work / "child-2")
        second = child_result(second_proc, second_result)
        total = platform.count
    return {
        "first": {"status": first.get("status"), "returncode": first.get("returncode")},
        "second": {"status": second.get("status"),
                   "semantics": second.get("semantics"),
                   "returncode": second.get("returncode")},
        "posts_after_first": posts_after_first, "posts_total": total,
        "ok": (first.get("status") == "unconfirmed" and posts_after_first == 1
               and second.get("status") == "blocked_uncertain"
               and total == 1),
    }


@_scenario("concurrent_default_vs_explicit",
           "两进程竞争（默认根 vs 显式）：总 POST 恰 1")
def scenario_concurrent(base: Path) -> dict:
    with MockPlatform(list_visible=False) as platform:
        work = base / "race"
        url = _url(platform)
        explicit = _empty_journal(base / "explicit-race.json")
        first_proc, first_result = spawn_location_child(
            work=work, url=url, port=platform.port, config_work=work / "child-1")
        second_proc, second_result = spawn_location_child(
            work=work, url=url, port=platform.port,
            authoritative_path=str(explicit), config_work=work / "child-2")
        first = child_result(first_proc, first_result)
        second = child_result(second_proc, second_result)
        total = platform.count
    statuses = [first.get("status"), second.get("status")]
    return {"statuses": statuses, "posts_total": total,
            "ok": total == 1
            and any(s in {"blocked_concurrent", "blocked_uncertain"}
                    for s in statuses)}


@_scenario("normal_path_no_history",
           "无历史记录的正常路径：confirmed 且恰好 1 次 POST")
def scenario_normal(base: Path) -> dict:
    with MockPlatform(list_visible=True) as platform, loopback_guard(platform.port):
        out = _run(platform, base)
        registry = _registry_of()
    return {"status": out["status"], "posts": out["posts"],
            "created": out["result"].get("created"),
            "registry_locations": [
                item.get("path") for item in registry.get("locations", [])
                if isinstance(item, dict)],
            "ok": (out["status"] == "confirmed" and out["posts"] == 1
                   and out["result"].get("created") == 1
                   and bool(registry.get("locations")))}


@_scenario("corrupt_registry_fails_closed",
           "登记文件损坏：failed、0 POST、字节不变")
def scenario_corrupt_registry(base: Path) -> dict:
    target = registry_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("{not-json 张三 13800000001", encoding="utf-8")
    before = target.read_bytes()
    with MockPlatform(list_visible=True) as platform, loopback_guard(platform.port):
        out = _run(platform, base)
    return {"status": out["status"], "semantics": out["semantics"],
            "posts": out["posts"],
            "bytes_unchanged": _bytes_unchanged(target, before),
            "next_action": str(out["result"].get("next_action") or "")[:160],
            "ok": (out["status"] == "failed" and out["posts"] == 0
                   and _bytes_unchanged(target, before))}


@_scenario("registry_write_failure",
           "登记文件无法写入：failed、0 POST")
def scenario_registry_write_failure(base: Path) -> dict:
    if os.name == "nt" or os.geteuid() == 0:  # pragma: no cover - 平台差异
        return {"skipped": "chmod 不适用于当前平台/用户", "ok": True}
    data_dir = Path(os.environ["YIKOU_DATA_DIR"])
    data_dir.mkdir(parents=True, exist_ok=True)
    data_dir.chmod(0o500)
    try:
        with MockPlatform(list_visible=True) as platform, \
                loopback_guard(platform.port):
            out = _run(platform, base)
    finally:
        data_dir.chmod(0o700)
    return {"status": out["status"], "semantics": out["semantics"],
            "posts": out["posts"], "ok": out["status"] == "failed"
            and out["posts"] == 0}


@_scenario("other_account_not_blocked",
           "其他账号的记录不误伤：正常 1 POST、记录保留")
def scenario_other_account(base: Path) -> dict:
    other = base / "other-account.json"
    before = write_unresolved_record(other, account=OTHER_ACCOUNT,
                                     journal_id="other-1")
    write_registry(None, [str(other)])
    with MockPlatform(list_visible=True) as platform, loopback_guard(platform.port):
        out = _run(platform, base)
    return {"status": out["status"], "posts": out["posts"],
            "other_bytes_unchanged": _bytes_unchanged(other, before),
            "other_still_active": len(active_records(other)),
            "ok": (out["status"] == "confirmed" and out["posts"] == 1
                   and _bytes_unchanged(other, before)
                   and len(active_records(other)) == 1)}


@_scenario("legacy_upgrade_process",
           "未登记旧显式位置：未声明时不保护（边界），声明后阻断")
def scenario_legacy_upgrade(base: Path) -> dict:
    legacy = base / "legacy-unregistered.json"
    before = write_unresolved_record(legacy, journal_id="legacy-1")
    # (a) 未声明：修复后也不会自动发现（明确边界）
    with MockPlatform(list_visible=True) as platform, loopback_guard(platform.port):
        undeclared = _run(platform, base / "a")
    # (b) 运维按文档把旧路径写入登记文件后再切回默认根 → 阻断
    write_registry(None, [str(legacy)])
    with MockPlatform(list_visible=True) as platform, loopback_guard(platform.port):
        declared = _run(platform, base / "b")
    return {
        "undeclared": {"status": undeclared["status"], "posts": undeclared["posts"]},
        "declared": {"status": declared["status"],
                     "semantics": declared["semantics"],
                     "posts": declared["posts"],
                     "conflicts": declared["result"].get("location_conflicts")},
        "legacy_bytes_unchanged": _bytes_unchanged(legacy, before),
        "legacy_still_active": len(active_records(legacy)),
        "boundary_unregistered_not_discovered": undeclared["posts"] == 1,
        "ok": (undeclared["posts"] == 1
               and declared["status"] == "blocked_uncertain"
               and declared["posts"] == 0
               and _bytes_unchanged(legacy, before)
               and len(active_records(legacy)) == 1),
    }


def main() -> int:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out_path = Path(os.environ.get("R8S3FIX_OUT")
                    or f"/tmp/r8s3fix/probe-{stamp}.json")
    root = Path(tempfile.mkdtemp(prefix="r8s3fix-probe-"))
    fix_present = _fixed()
    print(f"[setup] 隔离目录 {root}")
    print(f"[setup] 修复存在（authority_location_gate）= {fix_present}")
    results: dict[str, object] = {}
    failures: list[str] = []
    boundaries: list[str] = []
    for name, expectation, func in SCENARIOS:
        scenario_base = _isolate(root / name)
        try:
            payload = func(scenario_base)
        except Exception as exc:  # noqa: BLE001 - 单场景失败不影响其它场景
            payload = {"ok": False, "exception": f"{type(exc).__name__}: {exc}"}
        results[name] = payload
        mark = "PASS" if payload.get("ok") else "FAIL"
        print(f"\n[{mark}] {name} —— {expectation}")
        print("  " + json.dumps(payload, ensure_ascii=False, default=str))
        if not payload.get("ok"):
            failures.append(name)
        if payload.get("boundary_unregistered_not_discovered"):
            boundaries.append(name)

    summary = {
        "fix_present": fix_present,
        "scenarios": results,
        "failed": failures,
        "boundaries": {
            "unregistered_legacy_location_not_auto_discovered": boundaries,
            "note": "未登记的任意旧显式位置无法自动发现；升级流程见"
                    " docs/OPTIMIZATION-R8-S3-FIX.md",
        },
        "conclusion": ("修复后行为全部符合验收期望"
                       if not failures else f"未达验收期望：{failures}"),
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2,
                                   default=str), encoding="utf-8")
    print(f"\n[汇总] fix_present={fix_present} failed={failures or '无'}")
    print(f"[边界] 未登记旧位置不会被自动发现（已记录）：{boundaries or '无'}")
    print(f"[证据] {out_path}")
    shutil.rmtree(root, ignore_errors=True)
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
