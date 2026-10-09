"""已知风险项的独立只读探针（不进入 pytest 自动收集）。

用法::

    python3 tests/independent_final_counterexample_probe.py
    python3 tests/independent_final_counterexample_probe.py --only m4-json-corrupt
    python3 tests/independent_final_counterexample_probe.py --list

退出码：0 = 选中的场景都 SAFE（安全不变量成立、没有复现缺陷）；
1 = 至少复现一个仍然存在的缺陷（报告行以 ``DEFECT`` 开头）。
全部使用临时目录/合成平台，不联网、不真实下单、不真实写 WPS、不读真实凭据。

3.6.19 契约（本探针锁定）：
跨运行阻断（「人工核对后才能重跑」）已**有意下线**——存在历史未决记录、跨作用域
记录、权威位置冲突、journal/登记文件损坏或不可读时，新运行照常登录、对账并提交，
只在日志里提示。因此本探针把这类场景从「POST=0 阻断」改写为「照常提交（POST 计数
增长）+ 原文件字节不变 / 记录不丢」，场景名里不再出现「未保守阻断」这类旧语义。

仍然严格锁定的不变量：

a. 同一权威 journal + 同一 batch_key 的跨进程并发仍由批次级锁挡下
   （``blocked_concurrent`` / ``batch-submission-lock``）；
b. 旧 journal / 旧哈希文件 / 迁移来源只读：不得被覆盖、删除或改写
   （受损文件保持原字节）；
c. 合成平台站内可见时，再次运行必须靠提交前对账识别已存在 → 不重复 POST
   （合法订单恰好一次）；
d. POST 超时/断线 = 已发送未知：本批内不自动重发，且必须留下活跃未决记录；
   只有可识别的显式拒绝才允许（对账后）重试；
e. 不同平台/账号不得被合并身份，其他 scope 的记录不得被清掉/改写；
f. WPS recovery 的只读/脱敏探针不变。

R6-4 补充：M4（JSON 语法损坏 journal 被当空状态）、M6（POST 超时/断线被当明确
失败丢弃「已发送未知」记录）、M8（批次匹配恒真/只按日期或账号/恒假 → 其他 scope
记录被误清或本批记录不被对账）三个盲区场景；对应变异脚本见
``tools/r7-acceptance/mutation_check_r6_4.py``。
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
# 「照常提交」场景里两次运行要靠批次锁串行，放宽锁等待避免调度抖动被误判成缺陷。
_SERIAL_LOCK_TIMEOUT = "60"


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


def _journal_records(path: Path) -> list[dict]:
    """读取 journal 的全部记录（不做活跃过滤）；读不到返回 []。"""
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    records = payload.get("records") if isinstance(payload, dict) else None
    if not isinstance(records, list):
        return []
    return [record for record in records if isinstance(record, dict)]


def _active_records(path: Path) -> list[dict]:
    """读取 journal，返回仍然活跃（未 resolved/discarded）的记录。"""
    return [record for record in _journal_records(path)
            if str(record.get("status") or "unresolved") not in _INACTIVE_STATUSES]


def _record_by_id(path: Path, journal_id: str) -> dict | None:
    for record in _journal_records(path):
        if str(record.get("journal_id") or "") == journal_id:
            return record
    return None


def _record_snapshot(record: dict | None) -> str:
    """记录级快照：逐字段比较「其他 scope 的记录有没有被改写」。"""
    return json.dumps(record, ensure_ascii=False, sort_keys=True, default=str)


def _authority_files(root: Path) -> list[Path]:
    """权威根下按「origin|账号」摘要命名的 scope 文件（不含锁/临时文件）。"""
    if not root.is_dir():
        return []
    return sorted(path for path in root.glob("*.json") if path.is_file())


def _active_ids(path: Path) -> set[str]:
    return {str(record.get("journal_id") or "") for record in _active_records(path)}


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


def probe_different_journal_paths_submit_and_keep_records() -> bool:
    """不同 journal 路径（同一权威共享状态）跨运行：各自照常提交、记录不丢。

    3.6.19：跨运行阻断已下线，本场景改为锁定「不破坏 + 照常提交」：
    两次先后运行（批次锁串行，不靠等超时）都必须真的发出 POST（计数 2）；
    共享权威 journal 必须保留该订单的活跃未决记录；两次运行的记录只落在同一个
    权威文件里（换 journal 路径不得拆分/丢失状态）。
    """
    with tempfile.TemporaryDirectory() as td:
        work = Path(td)
        (work / "hidden").write_text("1", encoding="utf-8")
        first = _spawn(work, work / "journal-a.json",
                       lock_timeout=_SERIAL_LOCK_TIMEOUT)
        _wait_signal(work)
        second = _spawn(work, work / "journal-b.json",
                        lock_timeout=_SERIAL_LOCK_TIMEOUT)
        rc_first, _ = _child_result(first)
        rc_second, _ = _child_result(second)
        count = _platform_count(work)
        authority = work / "authoritative-uncertain.json"
        active = _active_records(authority)
        print(f"[probe] different-journal rc={rc_first}/{rc_second} "
              f"POST count = {count}（3.6.19 期望 2：两次运行各提交一次）"
              f" 活跃记录={len(active)}")
        defects: list[str] = []
        if rc_first != 0 or rc_second != 0:
            defects.append(f"child_rc={rc_first}/{rc_second}")
        if count != 2:
            defects.append(f"post_count={count}")
        if not active:
            defects.append("authority_active_record_lost")
        return bool(defects)


def probe_crash_then_different_journal_keeps_record() -> bool:
    """首进程 POST 后崩溃、次进程换 journal 路径：照常提交且不丢崩溃记录。

    3.6.19：跨运行阻断已下线，本场景改为锁定「不破坏 + 照常提交」：
    崩溃进程本身只 POST 一次（rc=17、计数 1，语义保持不变）；换 journal 路径再次
    运行照常提交（计数 2）；崩溃进程留下的活跃未决记录在第二次运行后仍然活跃。
    """
    with tempfile.TemporaryDirectory() as td:
        work = Path(td)
        (work / "hidden").write_text("1", encoding="utf-8")
        crash = _spawn(work, work / "journal-a.json", "crash")
        rc_crash, _ = _child_result(crash)
        if rc_crash != 17:
            raise AssertionError(f"首进程未按预期崩溃：{rc_crash}")
        count_after_crash = _platform_count(work)
        authority = work / "authoritative-uncertain.json"
        crashed_active = _active_ids(authority)
        second = _spawn(work, work / "journal-b.json",
                        lock_timeout=_SERIAL_LOCK_TIMEOUT)
        rc_second, _ = _child_result(second)
        count = _platform_count(work)
        still_active = _active_ids(authority)
        print(f"[probe] crash + different-journal POST count = {count}"
              f"（crash 后 {count_after_crash}，3.6.19 期望 2：重跑照常提交）")
        defects: list[str] = []
        if count_after_crash != 1:
            defects.append(f"crash_post_count={count_after_crash}")
        if rc_second != 0 or count != 2:
            defects.append(f"rerun_rc={rc_second},post_count={count}")
        if not crashed_active or not crashed_active <= still_active:
            defects.append("crashed_active_record_lost")
        return bool(defects)


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
        out, err = first.communicate(timeout=60)
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


def probe_config_change_shares_authority_scope() -> bool:
    """本地工作簿路径变化不得拆分权威作用域：一个 scope 文件、两次提交各一次。

    3.6.19：跨运行阻断已下线，本场景改为锁定「不破坏 + 照常提交」——两次运行
    （不同 workbook 工作目录、同一共享权威根）都必须真的提交，且两次运行的记录
    都落在按「规范化 origin + 账号」命名的同一个权威文件里。
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
                       shared_dir=shared, set_authority=False,
                       lock_timeout=_SERIAL_LOCK_TIMEOUT)
        rc_first, _ = _child_result(first)
        second = _spawn(work_b, work_b / "journal-b.json",
                        shared_dir=shared, set_authority=False,
                        lock_timeout=_SERIAL_LOCK_TIMEOUT)
        rc_second, _ = _child_result(second)
        count = _platform_count_shared(shared)
        files = _authority_files(shared / "authoritative-root")
        active = [record for path in files for record in _active_records(path)]
        print(f"[probe] workbook-config-change rc={rc_first}/{rc_second} "
              f"POST count = {count}（3.6.19 期望 2：两次运行各提交一次）"
              f" scope 文件={[path.name for path in files]}")
        defects: list[str] = []
        if rc_first != 0 or rc_second != 0:
            defects.append(f"child_rc={rc_first}/{rc_second}")
        if count != 2:
            defects.append(f"post_count={count}")
        if len(files) != 1:
            defects.append(f"authority_files={[path.name for path in files]}")
        if not active:
            defects.append("authority_active_record_lost")
        return bool(defects)


def probe_legacy_journal_migrates_and_submits() -> bool:
    """旧 journal（显式配置路径）里的 unresolved 先迁移、再照常提交、记录不丢。

    3.6.19：历史未决记录不再阻断本批；本场景改为锁定「不破坏 + 照常提交」：
    本批照常提交（POST=1）；旧记录必须迁移进权威 journal 且保持活跃 unresolved
    （不被 resolved/discarded）；兼容镜像（显式配置的旧路径）里旧记录仍在
    （镜像合并只增不删）。
    """
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
        rc, _payload = _child_result(child)
        count = _platform_count(work)
        authority = work / "authoritative-uncertain.json"
        migrated = _record_by_id(authority, "cr-legacy-1")
        mirror_kept = _record_by_id(legacy, "cr-legacy-1")
        migrated_status = str((migrated or {}).get("status") or "unresolved")
        print(f"[probe] legacy-journal rc={rc} POST count = {count}"
              f"（3.6.19 期望 1：照常提交）迁移记录 status={migrated_status!r} "
              f"镜像保留={mirror_kept is not None}")
        defects: list[str] = []
        if rc != 0:
            defects.append(f"child_rc={rc}")
        if count != 1:
            defects.append(f"post_count={count}")
        if migrated is None:
            defects.append("legacy_record_not_migrated")
        elif migrated_status in _INACTIVE_STATUSES:
            defects.append(f"legacy_record_rewritten:{migrated_status}")
        if mirror_kept is None:
            defects.append("legacy_mirror_record_deleted")
        return bool(defects)


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
            out, err = child.communicate(timeout=60)
        finally:
            blocker.release()
        count = _platform_count(work)
        blocked = "blocked_concurrent" in out
        print(f"[probe] batch-lock-failure POST count = {count}"
              f" blocked_status={blocked}（安全期望 0/True）")
        return count != 0 or not blocked


def probe_equivalent_url_shares_authority_scope() -> bool:
    """等价 URL 写法（path/query/fragment）必须共享同一权威作用域。

    3.6.19：跨运行阻断已下线，本场景改为锁定作用域身份不变：两次运行都必须真的
    提交（POST=2），记录落在同一个按「规范化 origin + 账号」命名的权威文件里，
    且记录里的 platform 是规范化 origin（换写法不得另起一个 scope / 丢掉记录）。
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
        first = _spawn(work_a, work_a / "a.json", shared_dir=shared,
                       set_authority=False, lock_timeout=_SERIAL_LOCK_TIMEOUT,
                       env_extra={"INDEP_SSS_URL": "http://local.invalid/a/b#frag"})
        rc_first, _ = _child_result(first)
        second = _spawn(work_b, work_b / "b.json", shared_dir=shared,
                        set_authority=False, lock_timeout=_SERIAL_LOCK_TIMEOUT,
                        env_extra={"INDEP_SSS_URL": "http://local.invalid"})
        rc_second, _ = _child_result(second)
        count = _platform_count_shared(shared)
        files = _authority_files(shared / "authoritative-root")
        platforms = sorted({str(record.get("platform"))
                            for path in files for record in _active_records(path)})
        print(f"[probe] equivalent-url rc={rc_first}/{rc_second} POST count = "
              f"{count}（3.6.19 期望 2）scope 文件={[path.name for path in files]} "
              f"platform={platforms}")
        defects: list[str] = []
        if rc_first != 0 or rc_second != 0:
            defects.append(f"child_rc={rc_first}/{rc_second}")
        if count != 2:
            defects.append(f"post_count={count}")
        if len(files) != 1:
            defects.append(
                f"scope_not_shared:files={[path.name for path in files]}")
        if platforms != ["http://local.invalid"]:
            defects.append(f"platform_scope={platforms}")
        return bool(defects)


def probe_crash_then_equivalent_url_keeps_record() -> bool:
    """POST 后崩溃，再用等价 URL 写法重跑：照常提交、崩溃记录不丢。

    3.6.19：跨运行阻断已下线。安全期望：崩溃进程只 POST 一次（rc=17、计数 1）；
    换等价写法重跑照常提交（计数 2）；两次运行共享同一个权威 scope 文件，且崩溃
    进程留下的活跃未决记录在重跑后仍然活跃（记录不丢）。
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
        crash = _spawn(work_a, work_a / "a.json", "crash", shared_dir=shared,
                       set_authority=False, lock_timeout=_SERIAL_LOCK_TIMEOUT,
                       env_extra={"INDEP_SSS_URL": "http://local.invalid/x#y"})
        rc_crash, _ = _child_result(crash)
        if rc_crash != 17:
            print(f"[probe] crash-config first process rc={rc_crash}")
            return True
        count_after_crash = _platform_count_shared(shared)
        second = _spawn(work_b, work_b / "b.json", shared_dir=shared,
                        set_authority=False, lock_timeout=_SERIAL_LOCK_TIMEOUT,
                        env_extra={"INDEP_SSS_URL": "http://local.invalid"})
        rc_second, _ = _child_result(second)
        count = _platform_count_shared(shared)
        files = _authority_files(shared / "authoritative-root")
        active = [record for path in files for record in _active_records(path)]
        print(f"[probe] crash+config-change POST count = {count}"
              f"（crash 后 {count_after_crash}，3.6.19 期望 2）"
              f" scope 文件={[path.name for path in files]}")
        defects: list[str] = []
        if count_after_crash != 1:
            defects.append(f"crash_post_count={count_after_crash}")
        if rc_second != 0 or count != 2:
            defects.append(f"rerun_rc={rc_second},post_count={count}")
        if len(files) != 1:
            defects.append(
                f"scope_not_shared:files={[path.name for path in files]}")
        if not active:
            defects.append("crashed_active_record_lost")
        return bool(defects)


def probe_old_env_journal_migrates_and_submits() -> bool:
    """旧 YIKOU_SSS_UNCERTAIN_PATH 里的 unresolved 先迁移、再照常提交、记录不丢。

    3.6.19：历史未决记录不再阻断本批；本场景改为锁定「不破坏 + 照常提交」：
    本批照常提交（POST=1）；旧环境变量路径里的记录必须迁移进权威 journal 并保持
    活跃 unresolved，且旧文件里的这条记录仍在（兼容镜像只增不删）。
    """
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
        rc, _payload = _child_result(child)
        count = _platform_count(work)
        authority = work / "authoritative-uncertain.json"
        migrated = _record_by_id(authority, "cr-old-env-1")
        mirror_kept = _record_by_id(legacy_env, "cr-old-env-1")
        migrated_status = str((migrated or {}).get("status") or "unresolved")
        print(f"[probe] old-env-journal rc={rc} POST count = {count}"
              f"（3.6.19 期望 1：照常提交）迁移记录 status={migrated_status!r} "
              f"镜像保留={mirror_kept is not None}")
        defects: list[str] = []
        if rc != 0:
            defects.append(f"child_rc={rc}")
        if count != 1:
            defects.append(f"post_count={count}")
        if migrated is None:
            defects.append("old_env_record_not_migrated")
        elif migrated_status in _INACTIVE_STATUSES:
            defects.append(f"old_env_record_rewritten:{migrated_status}")
        if mirror_kept is None:
            defects.append("old_env_mirror_record_deleted")
        return bool(defects)


def probe_old_hash_migration_readonly_and_submits() -> bool:
    """旧哈希权威文件只读迁移：照常提交；旧文件字节不变、记录迁移进当前 scope。

    3.6.19：历史未决记录不再阻断本批；本场景改为锁定「不破坏 + 照常提交」：
    本批照常提交（POST=1）；旧哈希文件（只读迁移来源）字节必须完全不变；旧记录
    必须迁移进当前 scope 的权威 journal 且保持活跃 unresolved。
    """
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
        before = old_file.read_bytes()
        work = Path(td) / "work"
        work.mkdir()
        (work / "hidden").write_text("1", encoding="utf-8")
        child = _spawn(work, work / "configured.json",
                       env_extra={"YIKOU_DATA_DIR": str(data_dir)})
        rc, _payload = _child_result(child)
        count = _platform_count(work)
        bytes_unchanged = old_file.read_bytes() == before
        authority = work / "authoritative-uncertain.json"
        migrated = _record_by_id(authority, "cr-old-hash-1")
        migrated_status = str((migrated or {}).get("status") or "unresolved")
        print(f"[probe] old-hash-authority rc={rc} POST count = {count}"
              f"（3.6.19 期望 1：照常提交）旧文件字节不变={bytes_unchanged} "
              f"迁移记录 status={migrated_status!r}")
        defects: list[str] = []
        if rc != 0:
            defects.append(f"child_rc={rc}")
        if count != 1:
            defects.append(f"post_count={count}")
        if not bytes_unchanged:
            defects.append("readonly_old_hash_file_modified")
        if migrated is None:
            defects.append("old_hash_record_not_migrated")
        elif migrated_status in _INACTIVE_STATUSES:
            defects.append(f"old_hash_record_rewritten:{migrated_status}")
        return bool(defects)


def probe_malformed_journal_variants_block() -> bool:
    """records=[null]、缺字段、未知版本、混合坏记录都必须 fail-closed。

    3.6.19：这里「零 POST」不再来自历史记录闸门，而是来自本地写盘路径——
    畸形 journal 无法安全读改写，本批停止派发 POST（记录可靠落盘仍然是硬要求）。
    """
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
            out, err = child.communicate(timeout=60)
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
                       lock_timeout=_SERIAL_LOCK_TIMEOUT,
                       env_extra={"INDEP_SSS_URL": "http://a.invalid"})
        second = _spawn(work_b, work_b / "b.json", account="18758187838",
                        shared_dir=shared, set_authority=False,
                        lock_timeout=_SERIAL_LOCK_TIMEOUT,
                        env_extra={"INDEP_SSS_URL": "http://b.invalid"})
        first.communicate(timeout=60)
        second.communicate(timeout=60)
        count = _platform_count_shared(shared)
        print(f"[probe] different origin/account POST count = {count}"
              f"（安全期望 2）")
        return count != 2


def _paired_origin_scenario(url_a: str, url_b: str, *, crash_first: bool = False
                            ) -> tuple[int, int, list[str], list[str]]:
    """两个真实子进程使用等价/不同 URL。

    返回 (POST count, first rc, 权威 scope 文件名列表, 记录里的 platform 取值)；
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
                           lock_timeout=_SERIAL_LOCK_TIMEOUT,
                           env_extra={"INDEP_SSS_URL": url_a})
            out, err = first.communicate(timeout=60)
            first_rc = first.returncode
            second = _spawn(work_b, work_b / "b.json",
                            shared_dir=shared, set_authority=False,
                            lock_timeout=_SERIAL_LOCK_TIMEOUT,
                            env_extra={"INDEP_SSS_URL": url_b})
            second.communicate(timeout=60)
        else:
            first = _spawn(work_a, work_a / "a.json",
                           shared_dir=shared, set_authority=False,
                           lock_timeout=_SERIAL_LOCK_TIMEOUT,
                           env_extra={"INDEP_SSS_URL": url_a})
            second = _spawn(work_b, work_b / "b.json",
                            shared_dir=shared, set_authority=False,
                            lock_timeout=_SERIAL_LOCK_TIMEOUT,
                            env_extra={"INDEP_SSS_URL": url_b})
            first.communicate(timeout=60)
            second.communicate(timeout=60)
            first_rc = first.returncode
        files = _authority_files(shared / "authoritative-root")
        platforms = sorted({str(record.get("platform"))
                            for path in files for record in _active_records(path)})
        return _platform_count_shared(shared), first_rc, \
            [path.name for path in files], platforms


def probe_equivalent_origin_variants_blocked() -> bool:
    """等价 origin 变体（大小写/默认端口/path/query/fragment/尾斜杠）必须共享一个 scope。

    3.6.19：跨运行阻断已下线，判据改为作用域身份：每一对等价写法都必须照常提交
    （POST=2）、只落在一个权威文件里，且记录的 platform 是规范化 origin。
    """
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
        count, rc, files, platforms = _paired_origin_scenario(url_a, url_b)
        print(f"[probe] equivalent-origin {url_a!r} vs {url_b!r}: "
              f"POST={count} rc={rc} scope={files} platform={platforms}"
              f"（安全期望 2/1 个 scope）")
        if count != 2:
            defects.append(f"{url_a}|{url_b}:post={count}")
        if len(files) != 1:
            defects.append(f"{url_a}|{url_b}:scope_not_shared={files}")
    # 首次 POST 后崩溃，再用等价 URL 运行。
    count, rc, files, _platforms = _paired_origin_scenario(
        "https://example.invalid/path",
        "HTTPS://EXAMPLE.INVALID:443/other", crash_first=True)
    print(f"[probe] crash+equivalent-origin POST={count} rc={rc} scope={files}"
          f"（安全期望 2/17/1 个 scope）")
    if count != 2 or rc != 17:
        defects.append(f"crash-equivalent:post={count},rc={rc}")
    if len(files) != 1:
        defects.append(f"crash-equivalent:scope_not_shared={files}")
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
                           lock_timeout=_SERIAL_LOCK_TIMEOUT,
                           env_extra={"INDEP_SSS_URL": url_a})
            second = _spawn(work_b, work_b / "b.json", account=acc_b,
                            shared_dir=shared, set_authority=False,
                            lock_timeout=_SERIAL_LOCK_TIMEOUT,
                            env_extra={"INDEP_SSS_URL": url_b})
            first.communicate(timeout=60)
            second.communicate(timeout=60)
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
    """账号 A 运行不得覆盖 B/其他平台旧哈希文件；两个账号各自照常提交、记录不丢。

    3.6.19：跨运行阻断已下线，本场景改为锁定「不破坏 + 照常提交 + 身份不合并」：
    A、B 两个账号先后各提交一次（POST 累计 2）；旧哈希文件（只读迁移来源）字节
    始终不变；A 的权威文件不得出现 B / 其他平台的记录，B 的记录迁移进自己的 scope。
    """
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
        child_a.communicate(timeout=60)
        count_after_a = _platform_count(work)
        after_a = _snapshot_bytes(old_dir)
        records_a = _authority_records(root)

        child_b = _spawn(
            work, work / "configured-b.json", account="18758187838",
            set_authority=False,
            env_extra={"YIKOU_DATA_DIR": str(data_dir),
                       "INDEP_SSS_URL": platform})
        child_b.communicate(timeout=60)
        count_after_b = _platform_count(work)
        after_b = _snapshot_bytes(old_dir)

        b_unresolved = [r for r in _authority_records(root)
                        if r.get("account") == "18758187838"
                        and str(r.get("status") or "unresolved") == "unresolved"]
        defects: list[str] = []
        if count_after_a != 1:
            defects.append(f"post_count_after_account_a={count_after_a}")
        if count_after_b != 2:
            defects.append(f"post_count_after_account_b={count_after_b}")
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
        print(f"[probe] old-hash cross-account POST={count_after_a}→"
              f"{count_after_b}（3.6.19 期望 1→2：各自照常提交） "
              f"bytes_unchanged={after_a == before and after_b == before} "
              f"b_migrated={bool(b_unresolved)} defects={defects}")
        return bool(defects)


def probe_old_hash_corrupt_unknown_fail_closed() -> bool:
    """损坏/归属未知旧哈希文件不得被改写，也不得被当成本账号记录静默放行。

    3.6.19：跨运行阻断已下线——本场景改为锁定「不破坏 + 照常提交 + 身份不合并」：
    两个账号先后照常提交（POST 各 +1）；损坏文件与归属未知文件字节完全不变
    （受损文件保持原字节）；归属未知记录若被并入权威文件必须带 ``scope_unknown``
    标记，绝不能被当成当前账号的普通未决记录。
    """
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

        defects: list[str] = []
        for index, (account, configured) in enumerate(
                (("18758187837", "configured-a.json"),
                 ("18758187838", "configured-b.json")), start=1):
            child = _spawn(
                work, work / configured, account=account,
                set_authority=False,
                env_extra={"YIKOU_DATA_DIR": str(data_dir),
                           "INDEP_SSS_URL": platform})
            rc, _payload = _child_result(child)
            count = _platform_count(work)
            if rc != 0:
                defects.append(f"account={account}:child_rc={rc}")
            if count != index:
                defects.append(f"account={account}:post_count={count}")
            if _snapshot_bytes(old_dir) != before:
                defects.append(f"account={account}:readonly_old_hash_modified")

        authorities = [work / "authoritative-uncertain.json"] \
            + _authority_files(work / "authoritative-root")
        for path in authorities:
            for record in _journal_records(path):
                if str(record.get("journal_id")) != "unknown-owner":
                    continue
                if not record.get("scope_unknown"):
                    defects.append(
                        f"ownership_unknown_record_adopted:{path.name}")
        print(f"[probe] corrupt/unknown old-hash files: bytes unchanged="
              f"{_snapshot_bytes(old_dir) == before} defects={defects}"
              f"（安全期望 []）")
        return bool(defects)


# ---------------------------------------------------------------------------
# R6-4 补充：M4（JSON 语法损坏 → 当空 journal）、M6（POST 超时/断线 →
# 当明确失败丢弃已发送记录）、M8（批次匹配失准 → 其他 scope 记录被误清 /
# 本批记录不被对账）三个盲区的独立反证。
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
    3.6.19 起这里的「零 POST」不再来自历史记录闸门，而是本地写盘路径：损坏的
    journal 无法安全读改写，本批必须在派发 POST 之前停下，且不覆盖原文件。
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
    """M6：POST 超时/断线必须算“已发送未知”，不得被当明确失败丢弃记录。

    安全期望：
    1. 传输层异常（SssTransportError / 读超时 / 断线）分类为 ``_SubmissionUncertain``，
       不得成为可重发的“明确失败”；
    2. 2xx 但语义不明的响应（``{}``/``code=0``/``success=null``）同样不确定；
    3. 真实子进程 POST 落库后超时：本批恰好 1 次 POST，且本地必须留下一条活跃
       未决记录（已发送未知绝不能因为“分类成失败”而被丢弃）；
    4. 本批内绝不自动重发（单轮 POST 计数只 +1）；
    5. 3.6.19 起跨运行阻断已下线：站内不可见时再次运行照常提交（计数 2），
       第一次运行的活跃记录必须仍在（记录不丢）。
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
            active_first = _active_ids(authoritative)
            if rc != 0 or count_after_first != 1 \
                    or int(first.get("created") or 0) != 0:
                defects.append(
                    f"{fault}:first_run:rc={rc},post={count_after_first},"
                    f"created={first.get('created')}")
            if not active_first:
                defects.append(
                    f"{fault}:journal_lost_active_uncertain_record")
            rc2, second = _child_result(_spawn(
                work, journal, lock_timeout=_SERIAL_LOCK_TIMEOUT))
            count_after_second = _platform_count(work)
            still_active = _active_ids(authoritative)
            second_status = str(second.get("status") or "")
            print(f"[probe] POST fault={fault}: 首轮 POST={count_after_first} "
                  f"status={first.get('status')!r} 活跃未决={len(active_first)} → "
                  f"二次运行 POST={count_after_second} status={second_status!r}"
                  f"（3.6.19 期望 1/1/2：重跑照常提交）")
            if count_after_second != 2:
                defects.append(
                    f"{fault}:rerun_post_count={count_after_second}")
            if not active_first <= still_active:
                defects.append(
                    f"{fault}:active_record_lost_after_rerun")
    print(f"[probe] POST 超时/断线 defects = {defects}（安全期望 []）")
    return bool(defects)


def _scope_record(journal_id: str, *, batch_key: str, delivery_date: str,
                  account: str, platform: str,
                  status: str = "unresolved",
                  fingerprint: dict | None = None) -> dict:
    """构造一条可被 load_journal 校验通过的合成未决记录。"""
    return {
        "journal_id": journal_id,
        "identifier": journal_id,
        "client_request_id": journal_id,
        "batch_key": batch_key,
        "delivery_date": delivery_date,
        "account": account,
        "platform": platform,
        "fingerprint": dict(fingerprint or {}),
        "status": status,
        "error": "ReadTimeout",
    }


def _seed_authoritative(work: Path, records: list[dict]) -> Path:
    path = work / "authoritative-uncertain.json"
    path.write_text(json.dumps({"version": 1, "records": records},
                               ensure_ascii=False), encoding="utf-8")
    return path


def _capture_station_and_fingerprint(base: Path
                                     ) -> tuple[list[dict], dict, str] | None:
    """跑一次正常子进程，取回站内订单记录与本批任务的 journal 指纹/请求 id。

    用例 D 需要「站内已存在本批订单」的合成状态；直接复用真实运行产生的
    platform.json 与 journal 记录，避免把报文指纹硬编码进探针。
    """
    capture = base / "capture"
    capture.mkdir()
    rc, _payload = _child_result(_spawn(capture, capture / "journal-a.json"))
    platform_path = capture / "platform.json"
    journal_path = capture / "authoritative-uncertain.json"
    if rc != 0 or not platform_path.exists() or not journal_path.exists():
        return None
    station = json.loads(platform_path.read_text(encoding="utf-8"))
    records = _journal_records(journal_path)
    if not records:
        return None
    fingerprint = dict(records[0].get("fingerprint") or {})
    request_id = str(records[0].get("client_request_id")
                     or records[0].get("journal_id") or "")
    if not isinstance(station.get("records"), list) or not fingerprint \
            or not request_id:
        return None
    return station["records"], fingerprint, request_id


#: 合成「另一个人的站内订单」时替换的字段：站内记录字段 → 指纹字段。
#: 名字/电话/门牌同时改，其余（门店/商品/地址/时间）保持不变，保证
#: 「站内记录 ↔ 记录指纹」仍然自洽。
_OTHER_PERSON_MARKERS = {
    "receiveName": "李四",
    "receivePhone": "13900000002",
    "doorNum": "B202",
}
_OTHER_PERSON_FINGERPRINT = {
    "receive_name": "李四",
    "receive_phone": "13900000002",
    "door_num": "b202",
}


def _other_person_station_record(record: dict, *, index: int) -> dict:
    """复制一条站内订单记录并换成“另一个人”的订单（用于隔离判据）。"""
    clone = json.loads(json.dumps(record, ensure_ascii=False))
    clone["id"] = f"order-seeded-other-person-{index}"
    clone["receiveName"] = _OTHER_PERSON_MARKERS["receiveName"]
    clone["receivePhone"] = _OTHER_PERSON_MARKERS["receivePhone"]
    address = clone.get("receiveAddress")
    if isinstance(address, dict):
        address["doorNum"] = _OTHER_PERSON_MARKERS["doorNum"]
    return clone


def _other_person_fingerprint(fingerprint: dict) -> dict:
    """把本批任务的指纹换成“另一个人”的指纹（与上面的站内记录自洽）。"""
    clone = dict(fingerprint)
    clone.update(_OTHER_PERSON_FINGERPRINT)
    return clone


def probe_batch_scope_isolation() -> bool:
    """M8：批次匹配必须按日期/账号隔离——其他 scope 记录不被误清/误并。

    安全期望（3.6.19：跨运行阻断已下线，判据改为「不破坏 + 照常提交 + 身份隔离」）：
    - A) 其他账号、其他日期（含旧三段键）、已 resolved 的同批记录都不得影响本批
      合法新订单：照常提交（POST=1、confirmed），且这些记录逐字段原样保留
      （不得被清理/覆盖）；
    - B) 同账号同日期、只是旧的 ``date|source|account`` 写法：照常提交（POST=1）、
      旧记录保持活跃 unresolved（3.6.19 起不再阻断，但记录不得被丢弃）；
    - C) 迁移来源中“其他平台”的记录必须被跳过（不并入本 scope、不得阻断），
      本批照常提交（POST=1）；
    - D) 站内已有本批订单 + 本批自己有一条历史未决记录：不得重复 POST，本批记录
      必须被对账清理成 resolved，而其他账号/日期（相同报文指纹）的记录必须逐字段
      原样保留——匹配过宽或过窄都会在这里暴露。
    """
    defects: list[str] = []
    platform = "http://local.invalid"

    # A) 其他账号 / 其他日期 / 旧三段键（其他日期）/ 已 resolved：都不得影响本批，
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
        print(f"[probe] batch-scope A（其他账号/日期不得影响本批）rc={rc} "
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
            if _record_snapshot(kept) != _record_snapshot(expected):
                defects.append(
                    f"other_scope_record_clobbered:{expected['journal_id']}:"
                    f"{kept}")

    # B) 同账号同日期、仅旧三段 batch_key（source 不同）：3.6.19 起照常提交，
    #    但这条历史记录必须保持活跃 unresolved（不得被丢弃/改写）。
    with tempfile.TemporaryDirectory() as td:
        work = Path(td)
        seeded = _scope_record("same-batch-legacy-key",
                               batch_key="2026-09-16|excel|18758187837",
                               delivery_date="2026-09-16",
                               account="18758187837", platform=platform)
        authoritative = _seed_authoritative(work, [seeded])
        rc, payload = _child_result(_spawn(work, work / "journal-a.json"))
        count = _platform_count(work)
        status = str(payload.get("status") or "")
        kept = _record_by_id(authoritative, "same-batch-legacy-key")
        print(f"[probe] batch-scope B（同批旧三段键：照常提交且记录保留）rc={rc} "
              f"POST={count} status={status!r} 记录仍在={kept is not None}"
              f"（安全期望 1/confirmed/True）")
        if rc != 0 or count != 1 or status != "confirmed":
            defects.append(
                f"same_batch_legacy_key_should_submit:rc={rc},post={count},"
                f"status={status}")
        if _record_snapshot(kept) != _record_snapshot(seeded):
            defects.append(f"same_batch_legacy_record_clobbered:{kept}")

    # C) 迁移来源里的“其他平台”记录必须被跳过（不并入本 scope），本批照常提交。
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
        authority_records = _journal_records(
            work / "authoritative-uncertain.json")
        print(f"[probe] batch-scope C（其他平台不得并入本 scope）rc={rc} "
              f"POST={count} status={status!r}（安全期望 1/confirmed）")
        if rc != 0 or count != 1 or status != "confirmed":
            defects.append(
                f"other_platform_should_not_block:rc={rc},post={count},"
                f"status={status}")
        if any(str(record.get("journal_id")) == "other-platform"
               for record in authority_records):
            defects.append("other_platform_record_merged_into_scope")

    # D) 本批自己的历史记录 + 站内已有该订单：不得重复 POST；本批记录必须被
    #    对账清理，而其他账号/日期（对应“另一个人的站内订单”）的记录必须逐字段
    #    原样保留——匹配过宽（恒真/只按日期或账号）会把别的 scope 清掉，
    #    匹配过窄（恒假）则本批自己的记录永远不被对账。
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        captured = _capture_station_and_fingerprint(base)
        if captured is None:
            defects.append("scope_d_capture_failed")
        else:
            station_records, fingerprint, request_id = captured
            other_fingerprint = _other_person_fingerprint(fingerprint)
            # 站内再加两条“另一个人”的订单：其他 scope 的两条记录各对应一条，
            # 匹配过宽时两条都会被误判成本批记录而对账清理（一条一条地暴露）。
            station_seed = list(station_records) + [
                _other_person_station_record(station_records[0], index=1),
                _other_person_station_record(station_records[0], index=2)]
            work = base / "own-record"
            work.mkdir()
            (work / "platform.json").write_text(json.dumps(
                {"count": len(station_seed), "records": station_seed},
                ensure_ascii=False), encoding="utf-8")
            own = _scope_record(request_id, batch_key="2026-09-16|18758187837",
                                delivery_date="2026-09-16",
                                account="18758187837", platform=platform,
                                fingerprint=fingerprint)
            other_account = _scope_record(
                "scope-d-other-account", batch_key="2026-09-16|18758187838",
                delivery_date="2026-09-16", account="18758187838",
                platform=platform, fingerprint=other_fingerprint)
            other_date = _scope_record(
                "scope-d-other-date", batch_key="2026-09-15|18758187837",
                delivery_date="2026-09-15", account="18758187837",
                platform=platform, fingerprint=other_fingerprint)
            others = {record["journal_id"]: record
                      for record in (other_account, other_date)}
            authoritative = _seed_authoritative(
                work, [own, other_account, other_date])
            rc, payload = _child_result(_spawn(work, work / "journal-a.json"))
            count = _platform_count(work)
            own_after = _record_by_id(authoritative, request_id)
            own_status = str((own_after or {}).get("status") or "")
            clobbered = [
                journal_id for journal_id, record in others.items()
                if _record_snapshot(_record_by_id(authoritative, journal_id))
                != _record_snapshot(record)]
            print(f"[probe] batch-scope D（站内已有 + 本批记录对账清理）rc={rc} "
                  f"站内计数={count}（安全期望 {len(station_seed)}：本轮未新增 POST）"
                  f" 本批记录 status={own_status!r}（期望 resolved）"
                  f" 其他 scope 被改写={clobbered}（期望 []）")
            if rc != 0 or count != len(station_seed):
                defects.append(
                    f"scope_d_unexpected_post:rc={rc},post={count}")
            if own_status != "resolved":
                defects.append(f"own_batch_record_not_reconciled:{own_status}")
            for journal_id in clobbered:
                defects.append(f"other_scope_record_clobbered:{journal_id}")

    print(f"[probe] 批次范围隔离 defects = {defects}（安全期望 []）")
    return bool(defects)


USAGE = """独立反证探针（不进入 pytest 自动收集）

    python3 tests/independent_final_counterexample_probe.py [--only 别名,...] [--list]

退出码：0 = 选中的场景都 SAFE（安全不变量成立、没有复现缺陷）；
1 = 至少复现一个仍然存在的缺陷（报告行以 DEFECT 开头）。
--only 只跑指定场景（别名见 --list），用于变异敏感性验证时缩短反馈时间；
不传 --only 时跑全部场景。

3.6.19：跨运行阻断（「人工核对后才能重跑」）已下线，涉及它的场景改写为
「照常提交 + 原文件字节不变 / 记录不丢」；只有批次锁、只读来源不破坏、
站内可见时的对账去重、账号/平台隔离、WPS recovery 脱敏仍然严格。
"""

# (别名, 场景名, 场景函数)。场景名同时是 DEFECT/SAFE 报告里的稳定标识，
# tools/r7-acceptance/mutation_check_r6_4.py 依赖这些名字做断言。
SCENARIOS: list[tuple[str, str, "Callable[[], bool]"]] = [
    ("journal-paths", "不同 journal 路径跨运行各自提交且记录不丢",
     probe_different_journal_paths_submit_and_keep_records),
    ("crash-journal-paths", "崩溃后换 journal 路径重跑丢失既有记录",
     probe_crash_then_different_journal_keeps_record),
    ("recovery-name-leak", "恢复查询泄露或错误拒绝客户/异常原文",
     probe_recovery_status_name_leak),
    ("recovery-exception-leak", "损坏 journal 异常响应回显客户原文",
     probe_recovery_exception_leak),
    ("positive-paths", "合法新订单/授权恢复查询被错误禁用",
     probe_legal_order_and_authorized_recovery_not_disabled),
    ("config-change", "工作簿路径变化拆分权威作用域或丢失记录",
     probe_config_change_shares_authority_scope),
    ("legacy-journal", "旧 journal 迁移失败或历史记录丢失",
     probe_legacy_journal_migrates_and_submits),
    ("batch-lock", "共享批次锁失败未保守阻断",
     probe_batch_lock_failure_blocks),
    ("equivalent-url", "等价 URL 写法拆分权威作用域",
     probe_equivalent_url_shares_authority_scope),
    ("crash-config-change", "崩溃后换等价 URL 重跑丢失既有记录",
     probe_crash_then_equivalent_url_keeps_record),
    ("old-env-path", "旧 YIKOU_SSS_UNCERTAIN_PATH 迁移失败或记录丢失",
     probe_old_env_journal_migrates_and_submits),
    ("old-hash-migration", "旧哈希权威文件被改写或记录未迁移",
     probe_old_hash_migration_readonly_and_submits),
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
    ("m6-post-timeout", "POST 超时/断线被当作明确失败（已发送未知记录被丢弃）",
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
    print("（3.6.19：跨运行阻断已下线；SAFE = 安全不变量成立，DEFECT = 仍复现缺陷）")
    if only:
        print(f"（--only {','.join(only)}：共 {len(selected)} 个场景）")
    for name, present in defects.items():
        print(("DEFECT   " if present else "SAFE     ") + name)
    failed = sum(1 for present in defects.values() if present)
    print(f"汇总：SAFE {len(defects) - failed}/{len(defects)}，DEFECT {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
