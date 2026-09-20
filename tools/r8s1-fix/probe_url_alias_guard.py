"""R8-S1 修复验收探针：URL 写法不得绕过未确认订单保护（端到端，离线）。

同一份探针可在**修复前快照**与**修复后工作区**运行，直接对比行为：

| 场景 | 修复前（预期） | 修复后（验收） |
| --- | --- | --- |
| 第 1 轮 canonical 进入 uncertain | 1 次 POST / unconfirmed | 同 |
| 第 2 轮同写法重跑 | 0 次 POST / blocked_uncertain | 同（对照） |
| 第 2 轮只改写法（尾点） | **1 次 POST（重复提交）** | **0 次 POST / 配置错误** |
| 旧尾点 journal 存在时用规范写法启动 | **1 次 POST（绕过）** | **0 次 POST / 跨作用域阻断** |
| 进程重启后再跑 | 视场景重复 | 0 次 POST |
| 并发两次启动 | 可能重复 | ≤1 次 POST |
| 无历史记录的正常配置 | 1 次 POST / confirmed | 同（正向对照） |
| 本地 HTTPS 链路 | — | 1 次 POST / confirmed；尾点写法 0 次 |

所有请求只发给 127.0.0.1 的本地模拟平台；网络守卫拒绝非回环连接；数据目录、
权威根、锁根、临时目录全部在隔离临时目录内；账号/密码/名单均为合成值。

    PYTHONPATH=<repo> python3 tools/r8s1-fix/probe_url_alias_guard.py
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.integrations.api_client import origin_from_url  # noqa: E402
from tests.r8s1_url_guard_harness import (  # noqa: E402
    ACCOUNT,
    MockPlatform,
    active_records,
    child_result,
    loopback_guard,
    make_config,
    run_job,
    spawn_child,
    write_test_certs,
)
from app.ordering import uncertain as sss_uncertain  # noqa: E402

ALIAS_HOST = "sss.example.invalid."
SCENARIOS: list[tuple[str, str, object]] = []


def _scenario(name: str, expectation: str):
    def decorator(func):
        SCENARIOS.append((name, expectation, func))
        return func
    return decorator


def _legacy_alias_journal_path(root: Path, alias_url: str) -> Path:
    """复刻修复前的 authority 命名规则（尾点写法不会被归一）。"""
    legacy_origin = origin_from_url(alias_url)
    digest = hashlib.sha256(
        f"{legacy_origin}|{ACCOUNT}".encode("utf-8")).hexdigest()[:24]
    return root / f"{digest}.json"


def _write_legacy_journal(path: Path, platform: str, journal_id: str) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "version": 1,
        "records": [{
            "journal_id": journal_id,
            "identifier": "第 3 行 张三",
            "batch_key": f"2026-09-16|{ACCOUNT}",
            "delivery_date": "2026-09-16",
            "source": "excel",
            "account": ACCOUNT,
            "platform": platform,
            "fingerprint": {},
            "status": "unresolved",
            "error": "ReadTimeout",
            "created_at": "2026-09-16T10:00:00",
        }],
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    return path.read_bytes()


def _scope_of(config) -> str:
    try:
        return sss_uncertain.authority_scope_key(config)
    except Exception as exc:  # noqa: BLE001 - 探针要如实记录修复后的拒绝
        return f"<{type(exc).__name__}>"


def _fixed() -> bool:
    try:
        from app.integrations.sss_url import canonical_sss_origin  # noqa: F401
    except ImportError:
        return False
    return True


def _round(work: Path, url: str) -> tuple[dict, str]:
    """跑一轮真实 run_sss_job；返回 (结果, 记录的状态字符串)。"""
    config = make_config(work, url)
    scope = _scope_of(config)
    try:
        result = run_job(config)
        status = str(result.get("status"))
    except Exception as exc:  # noqa: BLE001
        status = f"EXCEPTION {type(exc).__name__}"
        result = {"status": status, "error": str(exc),
                  "error_type": type(exc).__name__}
    return result, f"{status} @ {scope}"


@_scenario("normal_no_history",
           "无旧未确认记录：1 次 POST、confirmed（正向对照）")
def scenario_normal_no_history(base: Path) -> dict:
    platform = MockPlatform(list_visible=True).start()
    try:
        work = base / "normal"
        url = f"http://sss.example.invalid:{platform.port}"
        with loopback_guard(platform.port):
            result, status = _round(work, url)
            config = make_config(work, url)
            journal = sss_uncertain.authoritative_uncertain_path(config)
            active = len(active_records(journal))
        return {"status": status, "posts": platform.count,
                "active_unresolved": active,
                "ok": platform.count == 1
                and result.get("status") == "confirmed" and active == 0}
    finally:
        platform.stop()


@_scenario("http_uncertain_then_alias",
           "第 1 轮 uncertain；第 2 轮只改尾点写法：修复后 0 次 POST")
def scenario_http_uncertain_then_alias(base: Path) -> dict:
    platform = MockPlatform(list_visible=False).start()
    try:
        port = platform.port
        work = base / "http-alias"
        canonical = f"http://sss.example.invalid:{port}"
        alias = f"http://{ALIAS_HOST}:{port}"
        with loopback_guard(port):
            first, first_status = _round(work, canonical)
            posts_after_first = platform.count
            config = make_config(work, canonical)
            journal = sss_uncertain.authoritative_uncertain_path(config)
            active_after_first = len(active_records(journal))

            second, second_status = _round(work, canonical)  # 同写法对照
            posts_after_second = platform.count

            third, third_status = _round(work, alias)  # 只改写法
            posts_after_third = platform.count
        return {
            "round1": {"status": first_status, "posts": posts_after_first,
                       "active_unresolved": active_after_first},
            "round2_same_writing": {"status": second_status,
                                    "posts_delta": posts_after_second
                                    - posts_after_first},
            "round3_alias_writing": {"status": third_status,
                                     "posts_delta": posts_after_third
                                     - posts_after_second,
                                     "error": third.get("error", "")},
            "ok": (posts_after_first == 1
                   and active_after_first == 1
                   and second.get("status") == "blocked_uncertain"
                   and posts_after_second - posts_after_first == 0
                   and posts_after_third - posts_after_second == 0),
        }
    finally:
        platform.stop()


@_scenario("legacy_alias_journal_then_canonical",
           "修复前遗留的尾点 scope unresolved：修复后规范写法启动 0 次 POST")
def scenario_legacy_alias_journal(base: Path) -> dict:
    platform = MockPlatform(list_visible=True).start()
    try:
        port = platform.port
        work = base / "legacy-alias"
        canonical = f"http://sss.example.invalid:{port}"
        alias = f"http://{ALIAS_HOST}:{port}"
        root = Path(os.environ["YIKOU_SSS_AUTHORITATIVE_ROOT"])
        legacy = _legacy_alias_journal_path(root, alias)
        before = _write_legacy_journal(legacy, origin_from_url(alias),
                                       "legacy-alias-1")
        config = make_config(work, canonical)
        with loopback_guard(port):
            result, status = _round(work, canonical)
        after = legacy.read_bytes()
        payload = json.loads(after.decode("utf-8"))
        return {
            "status": status,
            "posts": platform.count,
            "semantics": result.get("semantics"),
            "cross_scope_records": result.get("cross_scope_records")
            or (result.get("summary") or {}).get("cross_scope_records"),
            "legacy_journal_unchanged": after == before,
            "legacy_record_status": payload["records"][0].get("status"),
            "scope": _scope_of(config),
            "ok": platform.count == 0
            and str(result.get("status")) == "blocked_uncertain"
            and after == before
            and payload["records"][0].get("status") == "unresolved",
        }
    finally:
        platform.stop()


@_scenario("restart_then_alias",
           "进程重启后：同写法 0 次 POST；尾点写法配置错误 0 次 POST")
def scenario_restart(base: Path) -> dict:
    platform = MockPlatform(list_visible=False).start()
    try:
        port = platform.port
        work = base / "restart"
        canonical = f"http://sss.example.invalid:{port}"
        alias = f"http://{ALIAS_HOST}:{port}"
        rounds = []
        for url in (canonical, canonical):
            process, result_path = spawn_child(work=work, url=url, port=port)
            payload = child_result(process, result_path)
            rounds.append({"url": url, "status": payload.get("status"),
                           "semantics": payload.get("semantics"),
                           "returncode": payload.get("returncode"),
                           "posts_total": platform.count})
        process, result_path = spawn_child(work=work, url=alias, port=port)
        alias_payload = child_result(process, result_path)
        return {
            "rounds": rounds,
            "alias": {"status": alias_payload.get("status"),
                      "error": alias_payload.get("error", ""),
                      "returncode": alias_payload.get("returncode"),
                      "posts_total": platform.count},
            "ok": (platform.count == 1
                   and rounds[1].get("status") == "blocked_uncertain"
                   and "SssUrlConfigError" in str(alias_payload.get("error"))),
        }
    finally:
        platform.stop()


@_scenario("concurrent_two_starts",
           "同一批次并发两次启动：总 POST ≤ 1")
def scenario_concurrent(base: Path) -> dict:
    platform = MockPlatform(list_visible=False).start()
    try:
        port = platform.port
        work = base / "concurrent"
        url = f"http://sss.example.invalid:{port}"
        first_proc, first_result = spawn_child(work=work, url=url, port=port)
        second_proc, second_result = spawn_child(work=work, url=url, port=port)
        first = child_result(first_proc, first_result)
        second = child_result(second_proc, second_result)
        statuses = [first.get("status"), second.get("status")]
        return {
            "posts": platform.count,
            "statuses": statuses,
            "ok": platform.count <= 1
            and any(status in {"blocked_concurrent", "blocked_uncertain"}
                    for status in statuses),
        }
    finally:
        platform.stop()


@_scenario("https_local_chain",
           "本地 HTTPS：规范写法 1 次 POST confirmed；尾点写法 0 次 POST")
def scenario_https(base: Path) -> dict:
    certs = write_test_certs(base / "certs")
    os.environ["REQUESTS_CA_BUNDLE"] = certs["ca"]
    platform = MockPlatform(list_visible=True)
    platform.start(tls=True, certfile=certs["cert"], keyfile=certs["key"])
    try:
        port = platform.port
        canonical = f"https://sss.example.invalid:{port}"
        alias = f"https://{ALIAS_HOST}:{port}"
        with loopback_guard(port):
            # 1) 规范写法：完整走通本地 HTTPS 链路并确认 1 单
            first, first_status = _round(base / "https-canonical", canonical)
            posts_after_first = platform.count
            # 2) 尾点写法：独立工作目录，基线会另开 scope 再 POST；修复后配置错误
            second, second_status = _round(base / "https-alias", alias)
            posts_after_second = platform.count
        return {
            "canonical": {"status": first_status, "posts": posts_after_first},
            "alias": {"status": second_status,
                      "posts_delta": posts_after_second - posts_after_first,
                      "error_type": second.get("error_type", ""),
                      "error": second.get("error", "")},
            "ok": (first.get("status") == "confirmed"
                   and posts_after_first == 1
                   and posts_after_second - posts_after_first == 0
                   and second.get("error_type") == "SssUrlConfigError"),
        }
    finally:
        platform.stop()
        os.environ.pop("REQUESTS_CA_BUNDLE", None)


def main() -> int:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out_path = Path(os.environ.get("R8S1FIX_OUT")
                    or f"/tmp/r8s1fix/probe-{stamp}.json")
    work = Path(tempfile.mkdtemp(prefix="r8s1fix-probe-"))
    os.environ.update({
        "YIKOU_DATA_DIR": str(work / "userdata"),
        "YIKOU_SSS_AUTHORITATIVE_ROOT": str(work / "authority-root"),
        "YIKOU_SSS_LOCK_ROOT": str(work / "locks"),
        "TMPDIR": str(work / "tmp"),
    })
    os.environ.pop("YIKOU_SSS_AUTHORITATIVE_PATH", None)
    (work / "tmp").mkdir(parents=True, exist_ok=True)

    fix_present = _fixed()
    print(f"[setup] 隔离目录 {work}")
    print(f"[setup] 修复存在（app.integrations.sss_url）= {fix_present}")
    results: dict[str, object] = {}
    failures: list[str] = []
    for name, expectation, func in SCENARIOS:
        # 每个场景独立权威根 + 独立部署数据目录（R8-S3 起，部署数据目录里还有
        # “权威位置登记”锚点；共享它会让前面场景的旧记录按设计阻断后面的正向对照）
        scenario_root = work / "authority-roots" / name
        scenario_root.mkdir(parents=True, exist_ok=True)
        os.environ["YIKOU_SSS_AUTHORITATIVE_ROOT"] = str(scenario_root)
        scenario_data = work / "data-roots" / name
        scenario_data.mkdir(parents=True, exist_ok=True)
        os.environ["YIKOU_DATA_DIR"] = str(scenario_data)
        try:
            payload = func(work)
        except Exception as exc:  # noqa: BLE001 - 单个场景失败不影响其它场景
            payload = {"ok": False, "exception": f"{type(exc).__name__}: {exc}"}
        results[name] = payload
        mark = "PASS" if payload.get("ok") else "FAIL"
        print(f"\n[{mark}] {name} —— {expectation}")
        print("  " + json.dumps(payload, ensure_ascii=False, default=str))
        if not payload.get("ok"):
            failures.append(name)

    summary = {
        "fix_present": fix_present,
        "scenarios": results,
        "failed": failures,
        "conclusion": ("修复后行为全部符合验收期望"
                       if not failures else f"未达验收期望：{failures}"),
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2,
                                   default=str), encoding="utf-8")
    print(f"\n[汇总] fix_present={fix_present} failed={failures or '无'}")
    print(f"[证据] {out_path}")
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
