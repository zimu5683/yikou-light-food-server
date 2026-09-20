"""已知阻断项的独立只读探针（不进入 pytest 自动收集）。

用法::

    python3 tests/independent_final_counterexample_probe.py
    python3 tests/independent_final_counterexample_probe.py --only m4-json-corrupt
    python3 tests/independent_final_counterexample_probe.py --list

退出码：0 = 场景都显示“已阻断”；1 = 至少复现一个“仍存在”的缺陷。
全部使用临时目录/合成平台，不联网、不真实下单、不真实写 WPS、不读真实凭据。

R6-4 补充：新增 M4（JSON 语法损坏 journal 被当空状态）、M6（POST 超时/断线被
当明确失败可重发）、M8（批次匹配恒真/忽略批次范围）三个盲区场景；对应变异脚本
见 ``tools/r7-acceptance/mutation_check_r6_4.py``。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable

REPO_ROOT = Path(__file__).resolve().parents[1]
CHILD = REPO_ROOT / "tests" / "independent_sss_batch_child.py"
# R6-4 补充：同形合成子进程，但支持 INDEP_POST_FAULT（POST 落库后抛超时/断线）。
FAULTY_CHILD = (REPO_ROOT / "tools" / "r7-acceptance"
                / "indep_sss_faulty_post_child.py")
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# 只统计仍然“活跃”（未 resolved/discarded）的未决记录。
_INACTIVE_STATUSES = frozenset({"resolved", "discarded"})


def _spawn(work: Path, journal: Path, mode: str = "normal", *,
           account: str = "18758187837",
           shared_dir: Path | None = None,
           set_authority: bool = True,
           lock_timeout: str = "3",
           env_extra: dict[str, str] | None = None,
           child: Path | None = None) -> subprocess.Popen:
    data_dir = work / "deployment-data"
    data_dir.mkdir(parents=True, exist_ok=True)
    authority = work / "authoritative-uncertain.json"
    env = {
        **os.environ,
        "PYTHONPATH": str(REPO_ROOT),
        "YIKOU_DATA_DIR": str(data_dir),
        "YIKOU_SSS_JOURNAL_LOCK_TIMEOUT": lock_timeout,
    }
    root_base = shared_dir if shared_dir is not None else work
    env["YIKOU_SSS_AUTHORITATIVE_ROOT"] = str(root_base / "authoritative-root")
    if set_authority:
        env["YIKOU_SSS_AUTHORITATIVE_PATH"] = str(authority)
    if shared_dir is not None:
        env["INDEP_SHARED_DIR"] = str(shared_dir)
    if env_extra:
        env.update({str(key): str(value) for key, value in env_extra.items()})
    return subprocess.Popen(
        [sys.executable, str(child or CHILD), str(work), str(journal), mode,
         account],
        cwd=str(REPO_ROOT),
        env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )


def _child_result(proc: subprocess.Popen) -> tuple[int, dict]:
    """等待子进程结束并解析其最后一行 JSON 结果。"""
    out, err = proc.communicate(timeout=60)
    payload: dict = {}
    for line in reversed((out or "").strip().splitlines()):
        try:
            parsed = json.loads(line)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            payload = parsed
            break
    if not payload:
        payload = {"_raw_out": (out or "")[-400:], "_raw_err": (err or "")[-400:]}
    return proc.returncode, payload


def _active_records(path: Path) -> list[dict]:
    """读取 journal，返回仍然活跃（未 resolved/discarded）的记录。"""
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    records = payload.get("records") if isinstance(payload, dict) else None
    if not isinstance(records, list):
        return []
    return [record for record in records if isinstance(record, dict)
            and str(record.get("status") or "unresolved")
            not in _INACTIVE_STATUSES]


def _wait_signal(work: Path, timeout: float = 20.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if (work / "signal").exists():
            return
        time.sleep(0.02)
    raise AssertionError("等待合成平台 POST signal 超时")


def _platform_count(work: Path) -> int:
    state_path = work / "platform.json"
    if not state_path.exists():
        return 0
    return int(json.loads(state_path.read_text(encoding="utf-8"))["count"])


def _platform_count_shared(shared_dir: Path) -> int:
    return int(json.loads((shared_dir / "platform.json").read_text(
        encoding="utf-8"))["count"])


def _wait_signal_shared(shared_dir: Path, timeout: float = 20.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if (shared_dir / "signal").exists():
            return
        time.sleep(0.02)
    raise AssertionError("等待合成平台 POST signal 超时")


def probe_different_journal_paths_duplicate() -> bool:
    """两个真实子进程、同批同账号、各自 journal 路径 → 是否重复 POST。"""
    with tempfile.TemporaryDirectory() as td:
        work = Path(td)
        (work / "hidden").write_text("1", encoding="utf-8")
        first = _spawn(work, work / "journal-a.json")
        _wait_signal(work)
        second = _spawn(work, work / "journal-b.json")
        first.communicate(timeout=40)
        second.communicate(timeout=40)
        count = _platform_count(work)
        print(f"[probe] different-journal POST count = {count}（安全期望 1）")
        return count != 1


def probe_recovery_status_name_leak() -> bool:
    """非管理员/管理员恢复查询是否泄露 journal problems 中的客户原文。

    注意：非管理员可调用本身是设计；这里只攻击“危险返回是否出现原文”。
    """
    from app.api.bridge import Bridge
    from app.wps.journal import SyncJournal
    from app.wps.ledger import SyncLedger

    with tempfile.TemporaryDirectory() as td:
        old = os.environ.get("YIKOU_DATA_DIR")
        os.environ["YIKOU_DATA_DIR"] = td
        try:
            SyncLedger(Path(td) / "wps_sync_state.json").save()
            journal = SyncJournal(Path(td) / "wps_sync_state.json.journal")
            journal.create_operation("op-leak", {
                "k": {"sheet": "东湖中餐", "file_id": "SECRET_FILE",
                      "target_date": "2026-09-20", "status": "uncertain",
                      "problems": ["张三: 总餐次应为 9，实际 3",
                                   "电话 13800000001 地址 学3-101"],
                      "risk_reason": "客户 张三 13800000001 餐次异常",
                      "reason": "回读不一致", "next_action": "manual_reconcile"},
            }, target_date="2026-09-20")
            journal.save()
            markers = ("张三", "13800000001", "总餐次应为", "SECRET_FILE",
                       "东湖中餐", "学3-101", td)
            leaks: list[str] = []
            for is_admin in (False, True):
                bridge = Bridge(config_path=str(Path(td) / "config.json"),
                                is_admin=is_admin)
                bridge.set_request_identity("admin" if is_admin else "bob")
                got = bridge.wps_recovery_status()
                if not got.get("ok"):
                    leaks.append(f"authorized_query_rejected(admin={is_admin})")
                    continue
                if is_admin and not isinstance(got.get("operations"), list):
                    leaks.append("admin_query_missing_operations")
                text = json.dumps(got, ensure_ascii=False)
                hits = [marker for marker in markers if marker in text]
                if hits:
                    leaks.append(f"admin={is_admin}:markers={hits}")
                scope = got.get("scope")
                print(f"[probe] recovery query admin={is_admin} ok=true "
                      f"scope={scope} leaks={hits}")
            return bool(leaks)
        finally:
            if old is None:
                os.environ.pop("YIKOU_DATA_DIR", None)
            else:
                os.environ["YIKOU_DATA_DIR"] = old


def probe_recovery_exception_leak() -> bool:
    """损坏 journal 的异常响应是否回显夹带的客户原文。"""
    from app.api.bridge import Bridge

    with tempfile.TemporaryDirectory() as td:
        old = os.environ.get("YIKOU_DATA_DIR")
        os.environ["YIKOU_DATA_DIR"] = td
        try:
            Path(td, "wps_sync_state.json").write_text(
                json.dumps({"version": 1, "batches": {}}), encoding="utf-8")
            Path(td, "wps_sync_state.json.journal").write_text(
                "{not-json 张三 13800000001 总餐次应为9", encoding="utf-8")
            bridge = Bridge(config_path=str(Path(td) / "config.json"),
                            is_admin=False)
            bridge.set_request_identity("bob")
            got = bridge.wps_recovery_status()
            text = json.dumps(got, ensure_ascii=False)
            hits = [marker for marker in
                    ("张三", "13800000001", "总餐次应为", "not-json")
                    if marker in text]
            print(f"[probe] corrupt-journal error response hits = {hits}"
                  f"（安全期望 []）")
            return bool(hits)
        finally:
            if old is None:
                os.environ.pop("YIKOU_DATA_DIR", None)
            else:
                os.environ["YIKOU_DATA_DIR"] = old


def probe_legal_order_and_authorized_recovery_not_disabled() -> bool:
    """正向：合法新订单与授权恢复查询不能被一概禁用。"""
    import json as _json
    import tempfile as _tempfile
    from app.api.bridge import Bridge
    from app.wps.journal import SyncJournal
    from app.wps.ledger import SyncLedger

    defects: list[str] = []
    with _tempfile.TemporaryDirectory() as td:
        work = Path(td) / "positive-order"
        work.mkdir()
        first = _spawn(work, work / "journal-a.json")
        out, err = first.communicate(timeout=40)
        if first.returncode != 0:
            defects.append(f"legal_order_process_failed:{first.returncode}:{err}")
        else:
            try:
                result = _json.loads(out.strip().splitlines()[-1])
                if result.get("created") != 1:
                    defects.append(f"legal_order_not_created:{result.get('created')}")
                if _platform_count(work) != 1:
                    defects.append(f"legal_order_post_count:{_platform_count(work)}")
            except Exception as exc:  # noqa: BLE001
                defects.append(f"legal_order_parse_failed:{exc}")

    with _tempfile.TemporaryDirectory() as td:
        old = os.environ.get("YIKOU_DATA_DIR")
        os.environ["YIKOU_DATA_DIR"] = td
        try:
            SyncLedger(Path(td) / "wps_sync_state.json").save()
            journal = SyncJournal(Path(td) / "wps_sync_state.json.journal")
            journal.create_operation("op-authorized", {
                "k": {"sheet": "东湖中餐", "file_id": "F-SAFE",
                      "target_date": "2026-09-20", "status": "verified",
                      "reason": "", "next_action": "none"},
            }, target_date="2026-09-20")
            journal.save()
            bridge = Bridge(config_path=str(Path(td) / "config.json"),
                            is_admin=True)
            got = bridge.wps_recovery_status()
            if not got.get("ok"):
                defects.append("authorized_recovery_rejected")
            elif not isinstance(got.get("operations"), list):
                defects.append("authorized_recovery_missing_operations")
        finally:
            if old is None:
                os.environ.pop("YIKOU_DATA_DIR", None)
            else:
                os.environ["YIKOU_DATA_DIR"] = old
    print(f"[probe] positive paths defects = {defects}（安全期望 []）")
    return bool(defects)


def probe_config_change_different_workbook_duplicate() -> bool:
    """同一账号/日期，但本地工作簿路径变化（部署身份变化）时是否保守阻断。

    安全期望：同一业务批次应共享未决状态或拒绝并行；当前实现按 workbook 路径
    分 authoritative 文件，因此可能各自 POST。返回 True 表示复现重复。
    """
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        shared = base / "shared"
        shared.mkdir()
        (shared / "hidden").write_text("1", encoding="utf-8")
        work_a = base / "work-a"
        work_b = base / "work-b"
        work_a.mkdir()
        work_b.mkdir()

        first = _spawn(work_a, work_a / "journal-a.json",
                       shared_dir=shared, set_authority=False)
        _wait_signal_shared(shared)
        second = _spawn(work_b, work_b / "journal-b.json",
                        shared_dir=shared, set_authority=False)
        first.communicate(timeout=40)
        second.communicate(timeout=40)
        count = _platform_count_shared(shared)
        print(f"[probe] workbook-config-change POST count = {count}"
              f"（安全期望 1）")
        return count != 1


def probe_legacy_journal_blocks() -> bool:
    """旧 journal 路径存在 unresolved 时，是否保守阻断（安全期望 0 POST）。"""
    from app.ordering import uncertain as sss_uncertain

    with tempfile.TemporaryDirectory() as td:
        work = Path(td)
        (work / "hidden").write_text("1", encoding="utf-8")
        legacy = work / "legacy-uncertain.json"
        key = sss_uncertain.batch_key("2026-09-16", "excel", "18758187837")
        sss_uncertain.append_uncertain_records(
            legacy, key,
            [{"identifier": "legacy-1", "client_request_id": "cr-legacy-1",
              "fingerprint": {}, "error": "ReadTimeout", "status": "unresolved"}],
            meta={"delivery_date": "2026-09-16", "account": "18758187837",
                  "source": "excel"})
        child = _spawn(work, legacy)
        out, err = child.communicate(timeout=40)
        count = _platform_count(work)
        print(f"[probe] legacy-journal POST count = {count}（安全期望 0）"
              f" rc={child.returncode}")
        return count != 0 or child.returncode != 0


def probe_batch_lock_failure_blocks() -> bool:
    """共享批次锁被占用时，是否零 POST（安全期望 0）。"""
    from app.ordering import uncertain as sss_uncertain

    with tempfile.TemporaryDirectory() as td:
        work = Path(td)
        (work / "hidden").write_text("1", encoding="utf-8")
        key = sss_uncertain.batch_key("2026-09-16", "excel", "18758187837")
        blocker = sss_uncertain.batch_submission_lock(
            work / "unused", key, timeout=5.0)
        blocker.acquire()
        try:
            child = _spawn(work, work / "journal-a.json", lock_timeout="0.2")
            out, err = child.communicate(timeout=40)
        finally:
            blocker.release()
        count = _platform_count(work)
        blocked = "blocked_concurrent" in out
        print(f"[probe] batch-lock-failure POST count = {count}"
              f" blocked_status={blocked}（安全期望 0/True）")
        return count != 0 or not blocked


def probe_equivalent_url_blocks() -> bool:
    """等价平台 URL（尾斜杠/路径/fragment）必须落到同一权威批次。"""
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        shared = base / "shared"
        shared.mkdir()
        (shared / "hidden").write_text("1", encoding="utf-8")
        work_a = base / "work-a"
        work_b = base / "work-b"
        work_a.mkdir()
        work_b.mkdir()
        first = _spawn(work_a, work_a / "a.json", shared_dir=shared,
                       set_authority=False,
                       env_extra={"INDEP_SSS_URL": "http://local.invalid/a/b#frag"})
        _wait_signal_shared(shared)
        second = _spawn(work_b, work_b / "b.json", shared_dir=shared,
                        set_authority=False,
                        env_extra={"INDEP_SSS_URL": "http://local.invalid"})
        first.communicate(timeout=40)
        second.communicate(timeout=40)
        count = _platform_count_shared(shared)
        print(f"[probe] equivalent-url POST count = {count}（安全期望 1）")
        return count != 1


def probe_crash_then_config_change_blocks() -> bool:
    """POST 后崩溃，改 workbook/URL 等价写法再运行，仍不得重发。"""
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        shared = base / "shared"
        shared.mkdir()
        (shared / "hidden").write_text("1", encoding="utf-8")
        work_a = base / "work-a"
        work_b = base / "work-b"
        work_a.mkdir()
        work_b.mkdir()
        crash = _spawn(work_a, work_a / "a.json", "crash", shared_dir=shared,
                       set_authority=False,
                       env_extra={"INDEP_SSS_URL": "http://local.invalid/x#y"})
        out, err = crash.communicate(timeout=40)
        if crash.returncode != 17:
            print(f"[probe] crash-config first process rc={crash.returncode}")
            return True
        count_after_crash = _platform_count_shared(shared)
        second = _spawn(work_b, work_b / "b.json", shared_dir=shared,
                        set_authority=False,
                        env_extra={"INDEP_SSS_URL": "http://local.invalid"})
        second.communicate(timeout=40)
        count = _platform_count_shared(shared)
        print(f"[probe] crash+config-change POST count = {count}"
              f"（crash 后 {count_after_crash}，安全期望 1）")
        return count != 1


def probe_old_env_uncertain_path_blocks() -> bool:
    """旧 YIKOU_SSS_UNCERTAIN_PATH 中的 unresolved 必须合并并阻断。"""
    from app.ordering import uncertain as sss_uncertain

    with tempfile.TemporaryDirectory() as td:
        work = Path(td)
        (work / "hidden").write_text("1", encoding="utf-8")
        legacy_env = work / "old-env-journal.json"
        key = sss_uncertain.batch_key("2026-09-16", "excel", "18758187837")
        sss_uncertain.append_uncertain_records(
            legacy_env, key,
            [{"identifier": "old-env-1", "client_request_id": "cr-old-env-1",
              "fingerprint": {}, "error": "ReadTimeout",
              "status": "unresolved"}],
            meta={"delivery_date": "2026-09-16", "account": "18758187837",
                  "source": "excel", "platform": "http://local.invalid"})
        child = _spawn(work, work / "configured.json",
                       env_extra={"YIKOU_SSS_UNCERTAIN_PATH": str(legacy_env)})
        out, err = child.communicate(timeout=40)
        count = _platform_count(work)
        print(f"[probe] old-env-journal POST count = {count}（安全期望 0）"
              f" rc={child.returncode}")
        return count != 0 or child.returncode != 0


def probe_old_hash_authority_migration_blocks() -> bool:
    """旧规则生成的哈希权威文件必须迁移并阻断。"""
    from app.ordering import uncertain as sss_uncertain

    with tempfile.TemporaryDirectory() as td:
        data_dir = Path(td) / "userdata"
        old_dir = data_dir / "sss_authoritative"
        old_dir.mkdir(parents=True)
        old_file = old_dir / "legacy-hash-authority.json"
        key = sss_uncertain.batch_key("2026-09-16", "excel", "18758187837")
        old_file.write_text(json.dumps({
            "version": 1,
            "records": [{
                "journal_id": "cr-old-hash-1", "identifier": "old-hash-1",
                "batch_key": key, "delivery_date": "2026-09-16",
                "account": "18758187837", "platform": "http://local.invalid",
                "status": "unresolved", "fingerprint": {}, "error": "ReadTimeout",
            }],
        }, ensure_ascii=False), encoding="utf-8")
        work = Path(td) / "work"
        work.mkdir()
        (work / "hidden").write_text("1", encoding="utf-8")
        child = _spawn(work, work / "configured.json",
                       env_extra={"YIKOU_DATA_DIR": str(data_dir)})
        out, err = child.communicate(timeout=40)
        count = _platform_count(work)
        print(f"[probe] old-hash-authority POST count = {count}（安全期望 0）"
              f" rc={child.returncode}")
        return count != 0 or child.returncode != 0


def probe_malformed_journal_variants_block() -> bool:
    """records=[null]、缺字段、未知版本、混合坏记录都必须 fail-closed。"""
    variants = {
        "null_record": '{"version": 1, "records": [null]}',
        "missing_key": '{"version": 1, "records": [{"journal_id": "x"}]}',
        "unknown_version": '{"version": 99, "records": []}',
        "mixed_bad": (
            '{"version": 1, "records": ['
            '{"journal_id": "ok", "batch_key": "2026-09-16|18758187837",'
            ' "delivery_date": "2026-09-16", "account": "18758187837",'
            ' "platform": "http://local.invalid", "fingerprint": {},'
            ' "status": "unresolved"}, null]}'
        ),
    }
    defects: list[str] = []
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        for name, raw in variants.items():
            work = base / name
            work.mkdir()
            (work / "hidden").write_text("1", encoding="utf-8")
            journal = work / "configured.json"
            journal.write_text(raw, encoding="utf-8")
            child = _spawn(work, journal)
            out, err = child.communicate(timeout=40)
            count = _platform_count(work)
            if count != 0 or child.returncode != 0:
                defects.append(f"{name}:count={count},rc={child.returncode}")
    print(f"[probe] malformed-journal defects = {defects}（安全期望 []）")
    return bool(defects)


def probe_different_platform_and_account_are_not_overblocked() -> bool:
    """不同 origin/不同账号是不同安全域，必须仍能各自创建合法新订单。"""
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        shared = base / "shared"
        shared.mkdir()
        work_a = base / "a"
        work_b = base / "b"
        work_a.mkdir()
        work_b.mkdir()
        first = _spawn(work_a, work_a / "a.json", account="18758187837",
                       shared_dir=shared, set_authority=False,
                       env_extra={"INDEP_SSS_URL": "http://a.invalid"})
        second = _spawn(work_b, work_b / "b.json", account="18758187838",
                        shared_dir=shared, set_authority=False,
                        env_extra={"INDEP_SSS_URL": "http://b.invalid"})
        first.communicate(timeout=40)
        second.communicate(timeout=40)
        count = _platform_count_shared(shared)
        print(f"[probe] different origin/account POST count = {count}"
              f"（安全期望 2）")
        return count != 2


def _paired_origin_scenario(url_a: str, url_b: str, *, crash_first: bool = False
                            ) -> tuple[int, int]:
    """两个真实子进程使用等价/不同 URL，返回 (POST count, first rc)。

    不设置 YIKOU_SSS_AUTHORITATIVE_PATH，只由 _spawn 设置 ROOT。
    """
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        shared = base / "shared"
        shared.mkdir()
        (shared / "hidden").write_text("1", encoding="utf-8")
        work_a = base / "a"
        work_b = base / "b"
        work_a.mkdir()
        work_b.mkdir()
        if crash_first:
            first = _spawn(work_a, work_a / "a.json", "crash",
                           shared_dir=shared, set_authority=False,
                           env_extra={"INDEP_SSS_URL": url_a})
            out, err = first.communicate(timeout=40)
            first_rc = first.returncode
            second = _spawn(work_b, work_b / "b.json",
                            shared_dir=shared, set_authority=False,
                            env_extra={"INDEP_SSS_URL": url_b})
            second.communicate(timeout=40)
        else:
            first = _spawn(work_a, work_a / "a.json",
                           shared_dir=shared, set_authority=False,
                           env_extra={"INDEP_SSS_URL": url_a})
            second = _spawn(work_b, work_b / "b.json",
                            shared_dir=shared, set_authority=False,
                            env_extra={"INDEP_SSS_URL": url_b})
            first.communicate(timeout=40)
            second.communicate(timeout=40)
            first_rc = first.returncode
        return _platform_count_shared(shared), first_rc


def probe_equivalent_origin_variants_blocked() -> bool:
    """等价 origin 变体（大小写/默认端口/path/query/fragment/尾斜杠）只能 POST 一次。"""
    pairs = [
        ("https://example.invalid/path",
         "HTTPS://EXAMPLE.INVALID:443/other"),
        ("http://example.invalid",
         "http://EXAMPLE.INVALID:80"),
        ("https://example.invalid/a?b=c#d",
         "https://example.invalid/"),
        ("https://example.invalid:8443/a",
         "HTTPS://EXAMPLE.INVALID:8443/b"),
    ]
    defects: list[str] = []
    for url_a, url_b in pairs:
        count, rc = _paired_origin_scenario(url_a, url_b)
        print(f"[probe] equivalent-origin {url_a!r} vs {url_b!r}: "
              f"POST={count} rc={rc}（安全期望 1）")
        if count != 1:
            defects.append(f"{url_a}|{url_b}:post={count}")
    # 首次 POST 后崩溃，再用等价 URL 运行。
    count, rc = _paired_origin_scenario(
        "https://example.invalid/path",
        "HTTPS://EXAMPLE.INVALID:443/other", crash_first=True)
    print(f"[probe] crash+equivalent-origin POST={count} rc={rc}"
          f"（安全期望 1/17）")
    if count != 1 or rc != 17:
        defects.append(f"crash-equivalent:post={count},rc={rc}")
    return bool(defects)


def probe_distinct_origins_not_merged() -> bool:
    """不同 host、http/https、非默认端口、不同账号不得被错误合并。"""
    cases = [
        ("http://example.invalid", "http://other.invalid",
         "18758187837", "18758187837", 2),
        ("http://example.invalid", "https://example.invalid",
         "18758187837", "18758187837", 2),
        ("https://example.invalid:8443", "https://example.invalid:9443",
         "18758187837", "18758187837", 2),
        ("http://example.invalid", "http://example.invalid",
         "18758187837", "18758187838", 2),
    ]
    defects: list[str] = []
    for url_a, url_b, acc_a, acc_b, expected in cases:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            shared = base / "shared"
            shared.mkdir()
            (shared / "hidden").write_text("1", encoding="utf-8")
            work_a = base / "a"
            work_b = base / "b"
            work_a.mkdir()
            work_b.mkdir()
            first = _spawn(work_a, work_a / "a.json", account=acc_a,
                           shared_dir=shared, set_authority=False,
                           env_extra={"INDEP_SSS_URL": url_a})
            second = _spawn(work_b, work_b / "b.json", account=acc_b,
                            shared_dir=shared, set_authority=False,
                            env_extra={"INDEP_SSS_URL": url_b})
            first.communicate(timeout=40)
            second.communicate(timeout=40)
            count = _platform_count_shared(shared)
            print(f"[probe] distinct origin/account {url_a!r}|{acc_a} vs "
                  f"{url_b!r}|{acc_b}: POST={count}（安全期望 {expected}）")
            if count != expected:
                defects.append(f"{url_a}|{url_b}|{acc_a}|{acc_b}:post={count}")
    return bool(defects)


def _write_old_hash_record(path: Path, *, account: str, platform: str,
                           journal_id: str, status: str = "unresolved",
                           delivery_date: str = "2026-09-16") -> None:
    batch_key = f"{delivery_date}|{account}"
    path.write_text(json.dumps({
        "version": 1,
        "records": [{
            "journal_id": journal_id,
            "identifier": journal_id,
            "batch_key": batch_key,
            "delivery_date": delivery_date,
            "account": account,
            "platform": platform,
            "fingerprint": {},
            "status": status,
            "error": "ReadTimeout",
        }],
    }, ensure_ascii=False, indent=2), encoding="utf-8")


def _snapshot_bytes(root: Path) -> dict[str, bytes]:
    return {str(path.relative_to(root)): path.read_bytes()
            for path in sorted(root.rglob("*")) if path.is_file()}


def _authority_records(root: Path) -> list[dict]:
    records: list[dict] = []
    for path in sorted(root.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        records.extend(record for record in payload.get("records", [])
                       if isinstance(record, dict))
    return records


def probe_old_hash_cross_account_isolation() -> bool:
    """账号 A 运行不得覆盖 B/其他平台旧哈希文件；B 再次运行仍应零 POST 阻断。"""
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        data_dir = base / "userdata"
        old_dir = data_dir / "sss_authoritative"
        old_dir.mkdir(parents=True)
        platform = "http://local.invalid"
        _write_old_hash_record(old_dir / "a.json", account="18758187837",
                               platform=platform, journal_id="old-a")
        _write_old_hash_record(old_dir / "b.json", account="18758187838",
                               platform=platform, journal_id="old-b")
        _write_old_hash_record(old_dir / "other.json", account="18758187837",
                               platform="https://other.invalid",
                               journal_id="old-other")
        _write_old_hash_record(old_dir / "resolved.json",
                               account="18758187837", platform=platform,
                               journal_id="old-resolved",
                               status="resolved")
        before = _snapshot_bytes(old_dir)
        work = base / "work"
        work.mkdir()
        (work / "hidden").write_text("1", encoding="utf-8")
        root = work / "authoritative-root"

        child_a = _spawn(
            work, work / "configured-a.json", account="18758187837",
            set_authority=False,
            env_extra={"YIKOU_DATA_DIR": str(data_dir),
                       "INDEP_SSS_URL": platform})
        child_a.communicate(timeout=40)
        count = _platform_count(work)
        after_a = _snapshot_bytes(old_dir)
        records_a = _authority_records(root)

        child_b = _spawn(
            work, work / "configured-b.json", account="18758187838",
            set_authority=False,
            env_extra={"YIKOU_DATA_DIR": str(data_dir),
                       "INDEP_SSS_URL": platform})
        child_b.communicate(timeout=40)
        after_b = _snapshot_bytes(old_dir)

        b_unresolved = [r for r in _authority_records(root)
                        if r.get("account") == "18758187838"
                        and str(r.get("status") or "unresolved") == "unresolved"]
        defects: list[str] = []
        if count != 0:
            defects.append(f"post_count={count}")
        if after_a != before:
            defects.append("account_a_run_modified_old_hash_files")
        if after_b != before:
            defects.append("account_b_run_modified_old_hash_files")
        if not any(r.get("journal_id") == "old-b" for r in b_unresolved):
            defects.append("account_b_unresolved_lost_or_not_migrated")
        if not any(r.get("journal_id") == "old-a" for r in records_a):
            defects.append("account_a_unresolved_not_migrated")
        if any(r.get("journal_id") == "old-b" for r in records_a):
            defects.append("account_a_authority_contains_account_b")
        if any(r.get("journal_id") == "old-other" for r in records_a):
            defects.append("account_a_authority_contains_other_platform")
        print(f"[probe] old-hash cross-account POST={count} "
              f"bytes_unchanged={after_a == before and after_b == before} "
              f"b_migrated={bool(b_unresolved)} defects={defects}")
        return bool(defects)


def probe_old_hash_corrupt_unknown_fail_closed() -> bool:
    """损坏/归属未知旧哈希文件不得被静默覆盖或当空文件放行。"""
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        data_dir = base / "userdata"
        old_dir = data_dir / "sss_authoritative"
        old_dir.mkdir(parents=True)
        platform = "http://local.invalid"
        _write_old_hash_record(old_dir / "a.json", account="18758187837",
                               platform=platform, journal_id="mixed-a")
        (old_dir / "corrupt.json").write_text(
            "{not-json 张三 13800000001", encoding="utf-8")
        (old_dir / "unknown.json").write_text(json.dumps({
            "version": 1,
            "records": [{
                "journal_id": "unknown-owner",
                "identifier": "unknown-owner",
                "batch_key": "2026-09-16|",
                "delivery_date": "2026-09-16",
                "fingerprint": {},
                "status": "unresolved",
            }],
        }, ensure_ascii=False), encoding="utf-8")
        before = _snapshot_bytes(old_dir)
        work = base / "work"
        work.mkdir()
        (work / "hidden").write_text("1", encoding="utf-8")

        for account, configured in (("18758187837", "configured-a.json"),
                                    ("18758187838", "configured-b.json")):
            child = _spawn(
                work, work / configured, account=account,
                set_authority=False,
                env_extra={"YIKOU_DATA_DIR": str(data_dir),
                           "INDEP_SSS_URL": platform})
            out, err = child.communicate(timeout=40)
            if child.returncode != 0:
                return True
            if _platform_count(work) != 0:
                return True
            if _snapshot_bytes(old_dir) != before:
                return True
        print("[probe] corrupt/unknown old-hash files: POST=0, bytes unchanged")
        return False


def probe_crash_then_different_journal_duplicate() -> bool:
    """首进程 POST 后崩溃、次进程用不同 journal 路径：是否盲目重发。"""
    with tempfile.TemporaryDirectory() as td:
        work = Path(td)
        (work / "hidden").write_text("1", encoding="utf-8")
        crash = _spawn(work, work / "journal-a.json", "crash")
        crash.communicate(timeout=40)
        if crash.returncode != 17:
            raise AssertionError(f"首进程未按预期崩溃：{crash.returncode}")
        second = _spawn(work, work / "journal-b.json")
        second.communicate(timeout=40)
        count = _platform_count(work)
        print(f"[probe] crash + different-journal POST count = {count}"
              f"（安全期望 1）")
        return count != 1


# ---------------------------------------------------------------------------
# R6-4 补充：M4（JSON 语法损坏 → 当空 journal）、M6（POST 超时/断线 →
# 当明确失败可重发）、M8（批次匹配恒真/忽略批次范围）三个盲区的独立反证。
# 三个场景在正常实现下必须全部为安全（返回 False），在对应变异下必须报 DEFECT。
# ---------------------------------------------------------------------------

# 语法损坏/无法解析的 journal 变体：(写入位置, 文件内容)
_CORRUPT_JOURNAL_CASES: dict[str, tuple[str, bytes]] = {
    # 半截写入/损坏（json.loads 直接抛 JSONDecodeError）
    "authoritative_syntax_garbage": (
        "authoritative", "{not-json 张三 13800000001 总餐次应为9".encode("utf-8")),
    "configured_syntax_garbage": (
        "configured", "{not-json 张三 13800000001 总餐次应为9".encode("utf-8")),
    "truncated_json": (
        "authoritative", b'{"version": 1, "records": [{"journal_id": "x"'),
    # 非 UTF-8 二进制垃圾（UnicodeDecodeError）
    "binary_garbage": ("authoritative", b"\x00\xff\xfe not json at all"),
    # 0 字节文件（同样是 JSONDecodeError）
    "empty_file": ("authoritative", b""),
    # 合法 JSON 但根节点不是对象
    "json_root_array": ("authoritative", b"[]"),
}


def probe_json_syntax_corrupt_journal_fail_closed() -> bool:
    """M4：JSON 语法损坏的 journal 必须 fail-closed，绝不能被当作空 journal。

    安全期望（每个变体）：POST=0、子进程状态=failed、损坏文件字节不变。
    末尾的“合法空 journal”正向对照保证探针不是“有文件就判缺陷”。
    """
    defects: list[str] = []
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        for name, (where, blob) in _CORRUPT_JOURNAL_CASES.items():
            work = base / name
            work.mkdir()
            (work / "hidden").write_text("1", encoding="utf-8")
            configured = work / "journal-a.json"
            authoritative = work / "authoritative-uncertain.json"
            target = authoritative if where == "authoritative" else configured
            target.write_bytes(blob)
            before = target.read_bytes()
            rc, payload = _child_result(_spawn(work, configured))
            count = _platform_count(work)
            status = str(payload.get("status") or "")
            unchanged = target.read_bytes() == before
            print(f"[probe] corrupt-journal[{name}] rc={rc} POST={count} "
                  f"status={status!r} 文件字节不变={unchanged}（安全期望 0/failed/True）")
            if rc != 0 or count != 0 or status != "failed" or not unchanged:
                defects.append(
                    f"{name}:rc={rc},post={count},status={status},"
                    f"bytes_unchanged={unchanged}")

        # 正向对照：合法空 journal（records=[]）不是“损坏”，必须仍能正常下单。
        control = base / "legal_empty_journal"
        control.mkdir()
        (control / "authoritative-uncertain.json").write_text(
            json.dumps({"version": 1, "records": []}), encoding="utf-8")
        rc, payload = _child_result(_spawn(control, control / "journal-a.json"))
        count = _platform_count(control)
        status = str(payload.get("status") or "")
        print(f"[probe] legal-empty-journal 对照 rc={rc} POST={count} "
              f"status={status!r}（安全期望 1/confirmed）")
        if rc != 0 or count != 1 or status != "confirmed":
            defects.append(
                f"legal_empty_journal_control:rc={rc},post={count},status={status}")
    print(f"[probe] JSON 语法损坏 journal defects = {defects}（安全期望 []）")
    return bool(defects)


def probe_post_timeout_uncertain_and_no_blind_resend() -> bool:
    """M6：POST 超时/断线必须是 uncertain + 对账优先 + 禁止盲目重发。

    安全期望：
    1. 传输层异常（SssTransportError / 读超时 / 断线）分类为 ``_SubmissionUncertain``，
       不得成为可重发的“明确失败”；
    2. 2xx 但语义不明的响应（``{}``/``code=0``/``success=null``）同样不确定；
    3. 真实子进程 POST 落库后超时：本地必须保留“活跃未决记录”；
    4. 第二次运行（列表仍读不到）不得再 POST，必须 ``blocked_uncertain``。
    """
    import requests
    from app.ordering import submission as sss_submission

    defects: list[str] = []

    def _classify(label: str, exc_factory) -> None:
        class _Client:
            def post_json(self, *_args, **_kwargs):
                raise exc_factory()

        try:
            sss_submission._post_one(_Client(), {"a": 1})
        except sss_submission._SubmissionUncertain:
            return
        except sss_submission._ExplicitRejection as exc:
            defects.append(f"{label}:classified_as_explicit_rejection:{exc}")
        except BaseException as exc:  # noqa: BLE001
            defects.append(f"{label}:classified_as:{type(exc).__name__}:{exc}")
        else:
            defects.append(f"{label}:accepted_as_success")

    _classify("transport_error", lambda: sss_submission.SssTransportError(
        "POST 断线（合成）"))
    _classify("read_timeout", lambda: requests.exceptions.ReadTimeout(
        "POST 读超时（合成）"))
    _classify("connection_reset", lambda: requests.exceptions.ConnectionError(
        "POST 连接被重置（合成）"))

    for ambiguous in ({}, {"code": 0}, {"success": None}):
        try:
            sss_submission._check_success(ambiguous)
        except sss_submission._SubmissionUncertain:
            continue
        except BaseException as exc:  # noqa: BLE001
            defects.append(
                f"ambiguous_2xx_{ambiguous}:classified_as:{type(exc).__name__}")
        else:
            defects.append(f"ambiguous_2xx_{ambiguous}:accepted_as_success")

    for fault in ("timeout", "connection_reset"):
        with tempfile.TemporaryDirectory() as td:
            work = Path(td)
            (work / "hidden").write_text("1", encoding="utf-8")
            journal = work / "journal-a.json"
            authoritative = work / "authoritative-uncertain.json"
            rc, first = _child_result(_spawn(
                work, journal, env_extra={"INDEP_POST_FAULT": fault},
                child=FAULTY_CHILD))
            count_after_first = _platform_count(work)
            active = _active_records(authoritative)
            if rc != 0 or count_after_first != 1 \
                    or int(first.get("created") or 0) != 0:
                defects.append(
                    f"{fault}:first_run:rc={rc},post={count_after_first},"
                    f"created={first.get('created')}")
            if not active:
                defects.append(
                    f"{fault}:journal_lost_active_uncertain_record")
            rc2, second = _child_result(_spawn(work, journal))
            count_after_second = _platform_count(work)
            second_status = str(second.get("status") or "")
            print(f"[probe] POST fault={fault}: 首轮 POST={count_after_first} "
                  f"status={first.get('status')!r} 活跃未决={len(active)} → "
                  f"二次运行 POST={count_after_second} status={second_status!r}"
                  f"（安全期望 1/1/blocked_uncertain）")
            if count_after_second != 1:
                defects.append(
                    f"{fault}:blind_resend_post={count_after_second}")
            if second_status != "blocked_uncertain":
                defects.append(f"{fault}:second_status={second_status}")
    print(f"[probe] POST 超时/断线 defects = {defects}（安全期望 []）")
    return bool(defects)


def _scope_record(journal_id: str, *, batch_key: str, delivery_date: str,
                  account: str, platform: str,
                  status: str = "unresolved") -> dict:
    """构造一条可被 load_journal 校验通过的合成未决记录。"""
    return {
        "journal_id": journal_id,
        "identifier": journal_id,
        "batch_key": batch_key,
        "delivery_date": delivery_date,
        "account": account,
        "platform": platform,
        "fingerprint": {},
        "status": status,
        "error": "ReadTimeout",
    }


def _seed_authoritative(work: Path, records: list[dict]) -> Path:
    path = work / "authoritative-uncertain.json"
    path.write_text(json.dumps({"version": 1, "records": records},
                               ensure_ascii=False), encoding="utf-8")
    return path


def probe_batch_scope_isolation() -> bool:
    """M8：批次匹配必须按日期/账号/batch_key 隔离，平台由迁移过滤隔离。

    安全期望：
    - 其他账号、其他日期（含旧三段键）、已 resolved 的同批记录都**不得阻断**
      本批合法新订单（POST=1、confirmed）；
    - 同账号同日期、只是旧的 ``date|source|account`` 写法必须**继续阻断**
      （source 差异不能绕过，POST=0、blocked_uncertain）；
    - 迁移来源中“其他平台”的记录必须被跳过，不得阻断本批（POST=1）。
    """
    defects: list[str] = []
    platform = "http://local.invalid"

    # A) 其他账号 / 其他日期 / 旧三段键（其他日期）/ 已 resolved：都不得阻断，
    #    且本批运行结束后这些其他范围的记录必须原样保留（不得被误清理）。
    with tempfile.TemporaryDirectory() as td:
        work = Path(td)
        seeded = [
            _scope_record("other-account", batch_key="2026-09-16|18758187838",
                          delivery_date="2026-09-16", account="18758187838",
                          platform=platform),
            _scope_record("other-date", batch_key="2026-09-15|18758187837",
                          delivery_date="2026-09-15", account="18758187837",
                          platform=platform),
            _scope_record("other-date-legacy",
                          batch_key="2026-09-15|excel|18758187837",
                          delivery_date="2026-09-15", account="18758187837",
                          platform=platform),
            _scope_record("resolved-same-batch",
                          batch_key="2026-09-16|18758187837",
                          delivery_date="2026-09-16", account="18758187837",
                          platform=platform, status="resolved"),
        ]
        authoritative = _seed_authoritative(work, seeded)
        rc, payload = _child_result(_spawn(work, work / "journal-a.json"))
        count = _platform_count(work)
        status = str(payload.get("status") or "")
        print(f"[probe] batch-scope A（其他账号/日期不得阻断）rc={rc} "
              f"POST={count} status={status!r}（安全期望 1/confirmed）")
        if rc != 0 or count != 1 or status != "confirmed":
            defects.append(
                f"other_scope_should_not_block:rc={rc},post={count},"
                f"status={status}")
        after = {record.get("journal_id"): record
                 for record in json.loads(
                     authoritative.read_text(encoding="utf-8"))["records"]}
        for expected in seeded:
            kept = after.get(expected["journal_id"])
            if kept != expected:
                defects.append(
                    f"other_scope_record_clobbered:{expected['journal_id']}:"
                    f"{kept}")

    # B) 同账号同日期、仅旧三段 batch_key（source 不同）：必须阻断。
    with tempfile.TemporaryDirectory() as td:
        work = Path(td)
        _seed_authoritative(work, [
            _scope_record("same-batch-legacy-key",
                          batch_key="2026-09-16|excel|18758187837",
                          delivery_date="2026-09-16", account="18758187837",
                          platform=platform),
        ])
        rc, payload = _child_result(_spawn(work, work / "journal-a.json"))
        count = _platform_count(work)
        status = str(payload.get("status") or "")
        print(f"[probe] batch-scope B（同批旧三段键必须阻断）rc={rc} "
              f"POST={count} status={status!r}（安全期望 0/blocked_uncertain）")
        if rc != 0 or count != 0 or status != "blocked_uncertain":
            defects.append(
                f"same_batch_legacy_key_should_block:rc={rc},post={count},"
                f"status={status}")

    # C) 迁移来源里的“其他平台”记录必须被跳过，不得阻断本批。
    with tempfile.TemporaryDirectory() as td:
        work = Path(td)
        configured = work / "journal-a.json"
        configured.write_text(json.dumps({"version": 1, "records": [
            _scope_record("other-platform", batch_key="2026-09-16|18758187837",
                          delivery_date="2026-09-16", account="18758187837",
                          platform="https://other.invalid"),
        ]}, ensure_ascii=False), encoding="utf-8")
        rc, payload = _child_result(_spawn(work, configured))
        count = _platform_count(work)
        status = str(payload.get("status") or "")
        print(f"[probe] batch-scope C（其他平台不得阻断）rc={rc} POST={count} "
              f"status={status!r}（安全期望 1/confirmed）")
        if rc != 0 or count != 1 or status != "confirmed":
            defects.append(
                f"other_platform_should_not_block:rc={rc},post={count},"
                f"status={status}")

    print(f"[probe] 批次范围隔离 defects = {defects}（安全期望 []）")
    return bool(defects)


USAGE = """独立反证探针（不进入 pytest 自动收集）

    python3 tests/independent_final_counterexample_probe.py [--only 别名,...] [--list]

退出码：0 = 选中的场景都显示“已阻断”；1 = 至少复现一个“仍存在”的缺陷。
--only 只跑指定场景（别名见 --list），用于变异敏感性验证时缩短反馈时间；
不传 --only 时跑全部场景，行为与旧版完全一致。
"""

# (别名, 场景名, 场景函数)。场景名同时是 DEFECT/BLOCKED 报告里的稳定标识，
# tools/r7-acceptance/mutation_check_r6_4.py 依赖这些名字做断言。
SCENARIOS: list[tuple[str, str, "Callable[[], bool]"]] = [
    ("journal-paths", "不同 journal 路径下跨进程重复 POST",
     probe_different_journal_paths_duplicate),
    ("crash-journal-paths", "首进程崩溃后不同 journal 路径盲目重发",
     probe_crash_then_different_journal_duplicate),
    ("recovery-name-leak", "恢复查询泄露或错误拒绝客户/异常原文",
     probe_recovery_status_name_leak),
    ("recovery-exception-leak", "损坏 journal 异常响应回显客户原文",
     probe_recovery_exception_leak),
    ("positive-paths", "合法新订单/授权恢复查询被错误禁用",
     probe_legal_order_and_authorized_recovery_not_disabled),
    ("config-change", "配置变化（workbook 路径）导致重复 POST",
     probe_config_change_different_workbook_duplicate),
    ("legacy-journal", "旧 journal unresolved 未保守阻断",
     probe_legacy_journal_blocks),
    ("batch-lock", "共享批次锁失败未保守阻断",
     probe_batch_lock_failure_blocks),
    ("equivalent-url", "等价 URL 未共享同一权威状态",
     probe_equivalent_url_blocks),
    ("crash-config-change", "POST 崩溃后改配置仍重发",
     probe_crash_then_config_change_blocks),
    ("old-env-path", "旧 YIKOU_SSS_UNCERTAIN_PATH 未合并阻断",
     probe_old_env_uncertain_path_blocks),
    ("old-hash-migration", "旧哈希权威文件未迁移阻断",
     probe_old_hash_authority_migration_blocks),
    ("malformed-journal", "畸形 journal 未 fail-closed",
     probe_malformed_journal_variants_block),
    ("distinct-scope", "不同 origin/账号被错误阻止",
     probe_different_platform_and_account_are_not_overblocked),
    ("equivalent-origin", "等价 origin 变体未共享权威状态",
     probe_equivalent_origin_variants_blocked),
    ("distinct-origin", "不同 host/http/https/端口/账号被错误合并",
     probe_distinct_origins_not_merged),
    ("old-hash-cross-account", "旧哈希 journal 跨账号覆盖或丢失",
     probe_old_hash_cross_account_isolation),
    ("old-hash-corrupt", "损坏/归属未知旧哈希文件被覆盖或放行",
     probe_old_hash_corrupt_unknown_fail_closed),
    # R6-4：三个此前缺失的盲区（M4/M6/M8）。
    ("m4-json-corrupt", "JSON 语法损坏 journal 被当作空状态放行",
     probe_json_syntax_corrupt_journal_fail_closed),
    ("m6-post-timeout", "POST 超时/断线被当作明确失败并允许盲目重发",
     probe_post_timeout_uncertain_and_no_blind_resend),
    ("m8-batch-scope", "批次范围（日期/账号/平台）隔离失效",
     probe_batch_scope_isolation),
]


def _parse_argv(argv: list[str]) -> tuple[list[str], bool]:
    only: list[str] = []
    listing = False
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg == "--only":
            index += 1
            if index >= len(argv):
                raise SystemExit("--only 需要一个逗号分隔的别名列表")
            only.extend(part.strip() for part in argv[index].split(",")
                        if part.strip())
        elif arg.startswith("--only="):
            only.extend(part.strip() for part in arg.split("=", 1)[1].split(",")
                        if part.strip())
        elif arg in ("--list", "-l"):
            listing = True
        elif arg in ("-h", "--help"):
            print(USAGE)
            raise SystemExit(0)
        else:
            raise SystemExit(f"未知参数：{arg}\n\n{USAGE}")
        index += 1
    return only, listing


def main(argv: list[str] | None = None) -> int:
    only, listing = _parse_argv(list(sys.argv[1:] if argv is None else argv))
    if listing:
        for alias, name, _func in SCENARIOS:
            print(f"{alias}\t{name}")
        return 0
    known = {alias for alias, _name, _func in SCENARIOS}
    unknown = [alias for alias in only if alias not in known]
    if unknown:
        raise SystemExit(f"未知场景别名：{unknown}；可用：{sorted(known)}")
    selected = [(alias, name, func) for alias, name, func in SCENARIOS
                if not only or alias in only]
    defects: dict[str, bool] = {}
    for _alias, name, func in selected:
        defects[name] = func()
    print("\n=== independent final counterexample probe ===")
    if only:
        print(f"（--only {','.join(only)}：共 {len(selected)} 个场景）")
    for name, present in defects.items():
        print(("DEFECT   " if present else "BLOCKED  ") + name)
    return 1 if any(defects.values()) else 0


if __name__ == "__main__":
    raise SystemExit(main())
