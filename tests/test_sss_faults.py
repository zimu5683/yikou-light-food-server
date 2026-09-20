"""闪时送下单故障、幂等与恢复的离线回归。

覆盖：POST 超时分类、401+timeout 禁止补发、人工 retry 禁止重发未知单、
本地不确定记录原子读写/跨运行阻断、记录失败停止、multiplicity 记录。
全部使用合成任务与伪客户端，不发真实 POST。
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest
from openpyxl import Workbook

from app.ordering import reconcile as sss_reconcile
from app.ordering import runner as sss_runner
from app.ordering import sss
from app.ordering import submission as sss_submission
from app.ordering import uncertain as sss_uncertain
from app.ordering.models import OrderFingerprint


@pytest.fixture(autouse=True)
def _isolated_authoritative_state(tmp_path, monkeypatch):
    # 每个测试独立的权威共享 journal / 旧默认目录，避免真实用户目录和测试间串扰。
    monkeypatch.setenv("YIKOU_DATA_DIR", str(tmp_path / "userdata"))
    monkeypatch.setenv("YIKOU_SSS_LOCK_ROOT", str(tmp_path / "locks"))
    monkeypatch.setenv(
        "YIKOU_SSS_AUTHORITATIVE_PATH",
        str(tmp_path / "authority" / "sss_uncertain_authoritative.json"),
    )


@pytest.fixture(autouse=True)
def _fast_and_offline_reconcile(monkeypatch):
    monkeypatch.setattr(sss_reconcile, "_SSS_SERVER_PREFILTER", False)
    monkeypatch.setattr(sss_reconcile, "_RECONCILE_POLL_INTERVAL_S", 0.0)
    monkeypatch.setattr(sss_submission, "_PREFILTER_ZERO_RETRY_DELAY_S", 0.0)
    monkeypatch.setattr(sss_uncertain, "_PREFILTER_ZERO_RETRY_DELAY_S", 0.0)


def _fingerprint(name="张三", phone="13800000001", door="A101",
                 delivery="2026-09-16 11:00:00", account="acct"):
    return OrderFingerprint(receive_name=name, receive_phone=phone, door_num=door,
                            expected_delivery_time=delivery, account=account)


def _task(identifier, name="张三", phone="13800000001", door="A101",
          delivery="2026-09-16 11:00:00"):
    return {
        "identifier": identifier,
        "payload": {"tag": identifier},
        "fingerprint": _fingerprint(name, phone, door, delivery),
        "account": "acct",
        "client_request_id": f"cr-{identifier}",
        "sheet": "午餐",
        "batch_id": "b1",
    }


def _empty_fetch(path):
    return {"success": True, "result": {"records": [], "total": 0}}


class _Stop:
    def __init__(self, false_ok=True):
        self.flag = False

    def is_set(self):
        return self.flag

    def set(self):
        self.flag = True


def test_post_one_wraps_plain_timeout_as_uncertain():
    class Client:
        def post_json(self, path, body):
            raise TimeoutError("timed out")

    with pytest.raises(sss._SubmissionUncertain, match="timed out"):
        sss._post_one(Client(), {})


def test_submit_tasks_classifies_auth_and_never_dispatched():
    tasks = [{"identifier": f"t{i}", "payload": {"i": i}} for i in range(3)]

    def submit(payload):
        if payload["i"] == 0:
            raise sss._AuthExpired("401")
        return {"success": True}

    result = sss._submit_tasks_concurrent(tasks, submit, _Stop(), None, max_workers=1)
    assert result.auth == {"t0"}
    assert result.not_sent == {"t1", "t2"}
    assert result.succeeded == set()


def test_timeout_uncertain_not_resent_after_other_task_gets_401():
    """同批 t0 超时、t1 401：重登后只补发 t1，t0 绝不自动补发。"""
    tasks = [_task("t0"), _task("t1", name="李四", phone="13800000002",
                                door="B202")]
    calls: list[str] = []

    def submit(payload):
        calls.append(payload["tag"])
        if len(calls) == 1:
            raise sss._SubmissionUncertain("ReadTimeout")
        if len(calls) == 2:
            raise sss._AuthExpired("401")
        return {"success": True}

    relogins: list[bool] = []
    outcome: dict = {}
    final, reconciled = sss._run_reconciled_submission(
        tasks, lambda: (submit, lambda: None), _empty_fetch, _Stop(), None, None,
        max_workers=1, relogin=lambda: relogins.append(True), outcome=outcome)

    assert calls == ["t0", "t1", "t1"], calls
    assert relogins == [True]
    assert outcome["uncertain"] == ["t0"]
    assert outcome["auth"] == ["t1"]
    assert outcome["success_responses"] == ["t1"]
    assert final is not None and "t0" in {item["identifier"] for item in final.missing}
    assert reconciled is True


def test_explicit_failure_can_retry_only_after_readonly_reconcile():
    """显式失败（success=false）允许在重试前只读对账后串行重试一次；
    结果仍以重试后对账为准，不把 POST success 当作 created。"""
    task = _task("t0")
    on_site: list[dict] = []
    calls: list[str] = []
    decisions: list[tuple[str, str]] = []

    station_record = {
        "id": "s1", "receiveName": "张三", "receivePhone": "13800000001",
        "expectedDeliveryTime": "2026-09-16 11:00:00", "orderType": 2,
        "receiveAddress": {"lnt": 1.0, "lat": 2.0, "areaCode": "330110",
                           "addressDetail": "X", "doorNum": "A101"},
        # 重试产生的站内订单创建时间必须落在本次对账窗口内（与真实时序一致）。
        "created_at": int(time.time() * 1000),
    }

    def submit(payload):
        calls.append(payload["tag"])
        if len(calls) == 1:
            return {"success": False, "message": "boom"}
        on_site.append({**station_record, "created_at": int(time.time() * 1000)})
        return {"success": True}

    def fetch(path):
        return {"success": True, "result": {"records": list(on_site), "total": len(on_site)}}

    def decide(identifier, error):
        decisions.append((identifier, error))
        return "retry"

    work = dict(task)
    work["fingerprint"] = _fingerprint(account="acct")
    final, reconciled = sss._run_reconciled_submission(
        [work], lambda: (submit, lambda: None), fetch, _Stop(), None, decide,
        max_workers=1)

    assert calls == ["t0", "t0"]
    assert decisions == [("t0", "boom")]
    assert reconciled is True
    assert final is not None and final.confirmed == {"t0"}
    assert final.missing == []


def test_401_does_not_resend_timeout_uncertain_under_concurrency():
    """并发同批：一个任务超时、一个任务 401；重登后只补发 401 那个。"""
    tasks = [_task("t0"), _task("t1", name="李四", phone="13800000002",
                                door="B202")]
    calls: list[str] = []

    def submit(payload):
        calls.append(payload["tag"])
        if payload["tag"] == "t0":
            raise sss._SubmissionUncertain("ReadTimeout")
        raise sss._AuthExpired("401")

    outcome: dict = {}
    final, _ = sss._run_reconciled_submission(
        tasks, lambda: (submit, lambda: None), _empty_fetch, _Stop(), None, None,
        max_workers=2, relogin=lambda: None, outcome=outcome)

    assert calls.count("t0") == 1
    assert calls.count("t1") == 2
    assert outcome["uncertain"] == ["t0"]
    assert outcome["auth"] == ["t1"]
    assert final is not None and {item["identifier"] for item in final.missing} == {
        "t0", "t1"}


def test_reconciled_submission_keeps_two_identical_orders():
    """两条同指纹订单遇到两条站内记录：初始对账必须一次确认两单、不发 POST。"""
    first = _task("first")
    second = _task("second")
    submission_calls: list[str] = []
    record = {
        "receiveName": "张三", "receivePhone": "13800000001",
        "expectedDeliveryTime": "2026-09-16 11:00:00",
        "receiveAddress": {"doorNum": "A101", "addressDetail": ""},
    }

    def submit(payload):
        submission_calls.append(payload["tag"])
        return {"success": True}

    def fetch(path):
        return {"success": True, "result": {"records": [
            {**record, "id": "s1"}, {**record, "id": "s2"}], "total": 2}}

    final, reconciled = sss._run_reconciled_submission(
        [first, second], lambda: (submit, lambda: None), fetch, _Stop(), None, None,
        max_workers=2)

    assert submission_calls == []
    assert reconciled is True
    assert final is not None and final.confirmed == {"first", "second"}
    assert final.missing == [] and final.duplicate_count == 0


def test_manual_retry_never_resends_uncertain_task():
    task = _task("t0")
    calls: list[str] = []
    decisions: list[tuple[str, str]] = []

    def submit(payload):
        calls.append(payload["tag"])
        raise sss._SubmissionUncertain("ReadTimeout")

    outcome: dict = {}
    final, _ = sss._run_reconciled_submission(
        [task], lambda: (submit, lambda: None), _empty_fetch, _Stop(), None,
        lambda identifier, error: decisions.append((identifier, error)) or "retry",
        max_workers=1, outcome=outcome)

    assert calls == ["t0"]
    assert decisions == []
    assert outcome["uncertain"] == ["t0"]
    assert final is not None and final.missing == [task]


def test_journal_append_resolve_is_atomic_and_preserves_multiplicity(tmp_path):
    path = tmp_path / "sss_uncertain.json"
    key = sss_uncertain.batch_key("2026-09-16", "excel", "acct")
    entries = [
        {"identifier": "第 3 行 张三", "client_request_id": "cr-3",
         "fingerprint": _fingerprint().as_dict(), "error": "timeout"},
        {"identifier": "第 4 行 张三", "client_request_id": "cr-4",
         "fingerprint": _fingerprint().as_dict(), "error": "timeout"},
    ]
    meta = {"delivery_date": "2026-09-16", "source": "excel",
            "account": "acct", "batch_started_at": time.time()}
    # 分两次落盘，模拟两个提交 round 各自产生不确定单：第二条不能覆盖第一条。
    assert sss_uncertain.append_uncertain_records(path, key, [entries[0]], meta=meta) == 1
    assert sss_uncertain.append_uncertain_records(path, key, [entries[1]], meta=meta) == 1
    payload = sss_uncertain.load_journal(path)
    pending = sss_uncertain.pending_records(payload["records"], key)
    assert len(pending) == 2
    assert {record["journal_id"] for record in pending} == {"cr-3", "cr-4"}
    assert all(record["fingerprint"]["door_num"] == "A101" for record in pending)

    assert sss_uncertain.resolve_uncertain_records(path, key, {"cr-3"}) == 1
    all_records = {record["journal_id"]: record
                   for record in sss_uncertain.load_journal(path)["records"]}
    pending = sss_uncertain.pending_records(list(all_records.values()), key)
    assert [record["journal_id"] for record in pending] == ["cr-4"]
    assert all_records["cr-3"]["status"] == "resolved"
    assert all_records["cr-4"]["status"] == "unresolved"
    assert list(tmp_path.glob(".sss_uncertain*.tmp")) == []


def test_t0_timeout_then_t1_401_timeout_keeps_both_journal_records(tmp_path):
    """P0 回归：第一轮 t0 超时、第二轮 t1 401 后也超时，两条 unresolved 都要保留。

    旧实现第二次 append 会按 batch 清掉所有旧 unresolved，只留 cr-t1，
    导致跨运行不再阻断 t0、下一轮可能重复 POST。这里走真实的
    ``_run_reconciled_submission`` + 本地 journal sink 复现该时序。
    """
    journal = tmp_path / "sss_uncertain.json"
    key = sss_uncertain.batch_key("2026-09-16", "excel", "acct")
    meta = {"delivery_date": "2026-09-16", "source": "excel",
            "account": "acct", "batch_started_at": time.time()}
    tasks = [_task("t0"), _task("t1", name="李四", phone="13800000002", door="B202")]
    calls: list[str] = []
    t1_calls = 0

    def submit(payload):
        nonlocal t1_calls
        tag = payload["tag"]
        calls.append(tag)
        if tag == "t0":
            # 走 _post_one 时 TimeoutError 会被包装为 _SubmissionUncertain；
            # 这里直接调 _run_reconciled_submission，所以显式模拟该分类。
            raise sss._SubmissionUncertain("ReadTimeout")
        t1_calls += 1
        if t1_calls == 1:
            raise sss._AuthExpired("401")
        raise sss._SubmissionUncertain("ReadTimeout")

    def sink(entries, journal_meta):
        sss_uncertain.append_uncertain_records(journal, key, entries, meta=journal_meta)

    outcome: dict = {}
    final, _ = sss._run_reconciled_submission(
        tasks, lambda: (submit, lambda: None), _empty_fetch, _Stop(), None, None,
        max_workers=1, relogin=lambda: None, uncertain_sink=sink,
        uncertain_clear=lambda identifiers: None, journal_meta=meta, outcome=outcome)

    assert calls.count("t0") == 1
    assert calls.count("t1") == 2
    assert outcome["uncertain"] == ["t0", "t1"]
    pending = sss_uncertain.pending_records(
        sss_uncertain.load_journal(journal)["records"], key)
    assert {record["journal_id"] for record in pending} == {"cr-t0", "cr-t1"}
    assert final is not None and {item["identifier"] for item in final.missing} == {
        "t0", "t1"}


def test_journal_append_same_entry_is_idempotent(tmp_path):
    """同一 journal_id 重复追加只保留一条 unresolved，不叠加阻断记录。"""
    path = tmp_path / "sss_uncertain.json"
    key = sss_uncertain.batch_key("2026-09-16", "excel", "acct")
    meta = {"delivery_date": "2026-09-16", "source": "excel",
            "account": "acct", "batch_started_at": time.time()}
    entry = {"identifier": "第 3 行 张三", "client_request_id": "cr-same",
             "fingerprint": _fingerprint().as_dict(), "error": "timeout"}
    # 一次调用内重复 entry、跨调用重复追加都必须幂等。
    assert sss_uncertain.append_uncertain_records(path, key, [entry, entry], meta=meta) == 1
    assert sss_uncertain.append_uncertain_records(path, key, [entry], meta=meta) == 1
    pending = sss_uncertain.pending_records(
        sss_uncertain.load_journal(path)["records"], key)
    assert [record["journal_id"] for record in pending] == ["cr-same"]


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def test_cross_process_append_preserves_all_records(tmp_path):
    """SN-C5：两个/多个独立进程同时追加，跨进程锁必须串行化且不丢记录。"""
    journal = tmp_path / "sss_uncertain.json"
    key = sss_uncertain.batch_key("2026-09-16", "excel", "acct")
    script = """
import sys
from app.ordering import uncertain

path, key, prefix = sys.argv[1], sys.argv[2], sys.argv[3]
meta = {"delivery_date": "2026-09-16", "source": "excel",
        "account": "acct", "batch_started_at": 1.0}
for index in range(5):
    uncertain.append_uncertain_records(
        path, key,
        [{"identifier": f"{prefix}-{index}",
          "client_request_id": f"cr-{prefix}-{index}",
          "fingerprint": {},
          "error": "ReadTimeout"}],
        meta=meta)
"""
    processes = [
        subprocess.Popen([sys.executable, "-c", script, str(journal), key, f"p{index}"],
                         cwd=str(_repo_root()))
        for index in range(4)
    ]
    assert [process.wait(timeout=30) for process in processes] == [0, 0, 0, 0]
    pending = sss_uncertain.pending_records(
        sss_uncertain.load_journal(journal)["records"], key)
    assert {record["journal_id"] for record in pending} == {
        f"cr-p{process_index}-{index}"
        for process_index in range(4) for index in range(5)
    }


def test_cross_process_append_and_resolve_do_not_overwrite_each_other(tmp_path):
    """SN-C5：进程 A 追加新记录、进程 B 更新旧记录，两者都不能覆盖对方。"""
    journal = tmp_path / "sss_uncertain.json"
    key = sss_uncertain.batch_key("2026-09-16", "excel", "acct")
    meta = {"delivery_date": "2026-09-16", "source": "excel",
            "account": "acct", "batch_started_at": 1.0}
    sss_uncertain.append_uncertain_records(
        journal, key,
        [{"identifier": "base", "client_request_id": "cr-base",
          "fingerprint": {}, "error": "ReadTimeout"}],
        meta=meta)
    append_script = """
import sys
from app.ordering import uncertain
path, key = sys.argv[1], sys.argv[2]
meta = {"delivery_date": "2026-09-16", "source": "excel",
        "account": "acct", "batch_started_at": 1.0}
for index in range(5):
    uncertain.append_uncertain_records(
        path, key,
        [{"identifier": f"new-{index}", "client_request_id": f"cr-new-{index}",
          "fingerprint": {}, "error": "ReadTimeout"}], meta=meta)
"""
    resolve_script = """
import sys
from app.ordering import uncertain
path, key = sys.argv[1], sys.argv[2]
uncertain.resolve_uncertain_records(path, key, {"cr-base"})
"""
    processes = [
        subprocess.Popen([sys.executable, "-c", append_script, str(journal), key],
                         cwd=str(_repo_root())),
        subprocess.Popen([sys.executable, "-c", resolve_script, str(journal), key],
                         cwd=str(_repo_root())),
    ]
    assert [process.wait(timeout=30) for process in processes] == [0, 0]
    all_records = {record["journal_id"]: record
                   for record in sss_uncertain.load_journal(journal)["records"]}
    assert all_records["cr-base"]["status"] == "resolved"
    assert {f"cr-new-{index}" for index in range(5)} <= set(all_records)
    pending = sss_uncertain.pending_records(
        sss_uncertain.load_journal(journal)["records"], key)
    assert {record["journal_id"] for record in pending} == {
        f"cr-new-{index}" for index in range(5)}


def test_cross_process_lock_times_out_with_explicit_rejection(tmp_path):
    """SN-C5：第二个进程等锁超时必须明确报错退出，不能直接覆盖。"""
    journal = tmp_path / "sss_uncertain.json"
    key = sss_uncertain.batch_key("2026-09-16", "excel", "acct")
    script = """
import sys
from app.ordering import uncertain

try:
    uncertain.append_uncertain_records(
        sys.argv[1], sys.argv[2],
        [{"identifier": "child-blocked", "client_request_id": "cr-blocked",
          "fingerprint": {}, "error": "ReadTimeout"}],
        meta={"delivery_date": "2026-09-16", "source": "excel",
              "account": "acct", "batch_started_at": 1.0})
except uncertain.UncertainJournalError as exc:
    if "超时" in str(exc):
        raise SystemExit(3)
    raise
raise SystemExit(0)
"""
    with sss_uncertain._journal_file_lock(journal):
        completed = subprocess.run(
            [sys.executable, "-c", script, str(journal), key],
            cwd=str(_repo_root()),
            env={**__import__("os").environ,
                 "YIKOU_SSS_JOURNAL_LOCK_TIMEOUT": "0.2"},
            capture_output=True, text=True, timeout=15)
    assert completed.returncode == 3, completed.stderr
    assert not journal.exists()


def test_crash_after_post_is_covered_by_prewrite(tmp_path):
    """SN-C4：请求已发出、客户端断线、进程随后崩溃，下次运行仍能看到记录并阻断。"""
    journal = tmp_path / "sss_uncertain.json"
    sentinel = tmp_path / "sent.txt"
    key = sss_uncertain.batch_key("2026-09-16", "excel", "acct")
    script = """
import os
import sys
from pathlib import Path

from app.ordering import submission, uncertain
from app.ordering.models import OrderFingerprint

journal, sentinel = sys.argv[1], sys.argv[2]
key = uncertain.batch_key("2026-09-16", "excel", "acct")
meta = {"delivery_date": "2026-09-16", "source": "excel",
        "account": "acct", "batch_started_at": 1.0}
fingerprint = OrderFingerprint(
    receive_name="张三", receive_phone="13800000001", door_num="A101",
    expected_delivery_time="2026-09-16 11:00:00", account="acct")
task = {"identifier": "t-crash", "payload": {"tag": "t-crash"},
        "fingerprint": fingerprint, "account": "acct",
        "client_request_id": "cr-crash"}

def fetch(path):
    return {"success": True, "result": {"records": [], "total": 0}}

def submit(payload):
    Path(sentinel).write_text("sent", encoding="utf-8")
    os._exit(17)

def sink(entries, journal_meta):
    uncertain.append_uncertain_records(journal, key, entries, meta=journal_meta)

class Stop:
    def is_set(self):
        return False

    def set(self):
        pass

submission._run_reconciled_submission(
    [task], lambda: (submit, lambda: None), fetch, Stop(), None, None,
    max_workers=1, uncertain_sink=sink,
    uncertain_discard=lambda identifiers: None, journal_meta=meta)
os._exit(0)
"""
    completed = subprocess.run(
        [sys.executable, "-c", script, str(journal), str(sentinel)],
        cwd=str(_repo_root()), capture_output=True, text=True, timeout=30)
    assert completed.returncode == 17, completed.stderr
    assert sentinel.read_text(encoding="utf-8") == "sent"
    pending = sss_uncertain.pending_records(
        sss_uncertain.load_journal(journal)["records"], key)
    assert [record["journal_id"] for record in pending] == ["cr-crash"]
    assert pending[0]["status"] == "inflight"
    # 下一次运行先做只读对账；站内仍无记录，必须保留阻断而不是重复下单。
    remaining, _reconciliation, resolved = sss_uncertain.resolve_pending_records(
        journal, key, lambda path: {"success": True,
                                    "result": {"records": [], "total": 0}},
        attempts=1)
    assert resolved == 0
    assert [record["journal_id"] for record in remaining] == ["cr-crash"]


def test_journal_concurrent_append_preserves_all_records(tmp_path):
    """同进程并发 append 必须串行化读-改-写，任何一个不确定单都不能被覆盖。

    真实调用来自提交 round，通常只在主线程；这里用多线程放大竞态，验证
    即使未来并发写入也不会丢记录。
    """
    path = tmp_path / "sss_uncertain.json"
    key = sss_uncertain.batch_key("2026-09-16", "excel", "acct")
    meta = {"delivery_date": "2026-09-16", "source": "excel",
            "account": "acct", "batch_started_at": time.time()}

    def write(index: int) -> int:
        return sss_uncertain.append_uncertain_records(
            path, key,
            [{"identifier": f"第 {index} 行 张三",
              "client_request_id": f"cr-{index}",
              "fingerprint": _fingerprint(door=f"A{index}").as_dict(),
              "error": "ReadTimeout"}],
            meta=meta)

    with ThreadPoolExecutor(max_workers=8) as executor:
        assert list(executor.map(write, range(8))) == [1] * 8
    pending = sss_uncertain.pending_records(
        sss_uncertain.load_journal(path)["records"], key)
    assert {record["journal_id"] for record in pending} == {f"cr-{i}" for i in range(8)}


def test_journal_write_failure_keeps_existing_records(tmp_path, monkeypatch):
    """追加写盘失败必须抛出并保留旧记录，原文件不能被截断/清空。"""
    path = tmp_path / "sss_uncertain.json"
    key = sss_uncertain.batch_key("2026-09-16", "excel", "acct")
    meta = {"delivery_date": "2026-09-16", "source": "excel",
            "account": "acct", "batch_started_at": time.time()}
    first = {"identifier": "第 3 行 张三", "client_request_id": "cr-first",
             "fingerprint": _fingerprint().as_dict(), "error": "timeout"}
    second = {"identifier": "第 4 行 李四", "client_request_id": "cr-second",
              "fingerprint": _fingerprint(name="李四").as_dict(), "error": "timeout"}
    assert sss_uncertain.append_uncertain_records(path, key, [first], meta=meta) == 1

    def broken_write(path_arg, payload):
        raise sss_uncertain.UncertainJournalError("disk full")

    monkeypatch.setattr(sss_uncertain, "_atomic_write", broken_write)
    with pytest.raises(sss_uncertain.UncertainJournalError, match="disk full"):
        sss_uncertain.append_uncertain_records(path, key, [second], meta=meta)
    pending = sss_uncertain.pending_records(
        sss_uncertain.load_journal(path)["records"], key)
    assert [record["journal_id"] for record in pending] == ["cr-first"]


def test_journal_corrupt_file_fails_closed(tmp_path):
    path = tmp_path / "sss_uncertain.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(sss_uncertain.UncertainJournalError, match="损坏"):
        sss_uncertain.load_journal(path)


def test_prewritten_explicit_failure_is_discarded_not_blocking(tmp_path):
    """SN-C4/C8 恢复：明确 success=false 是“未发送”证据，discard 后不阻断后续批次。"""
    journal = tmp_path / "sss_uncertain.json"
    key = sss_uncertain.batch_key("2026-09-16", "excel", "acct")
    meta = {"delivery_date": "2026-09-16", "source": "excel",
            "account": "acct", "batch_started_at": time.time()}
    calls: list[str] = []

    def submit(payload):
        calls.append(payload["tag"])
        return {"success": False, "message": "地址无效"}

    def sink(entries, journal_meta):
        sss_uncertain.append_uncertain_records(journal, key, entries, meta=journal_meta)

    def discard(identifiers):
        sss_uncertain.discard_uncertain_records(journal, key, identifiers)

    outcome: dict = {}
    sss._run_reconciled_submission(
        [_task("t0")], lambda: (submit, lambda: None), _empty_fetch, _Stop(),
        None, None, max_workers=1, uncertain_sink=sink,
        uncertain_discard=discard, journal_meta=meta, outcome=outcome)

    assert calls == ["t0"]
    assert sss_uncertain.pending_records(
        sss_uncertain.load_journal(journal)["records"], key) == []
    assert [record["journal_id"] for record in sss_uncertain.load_journal(
        journal)["records"] if record.get("status") == "discarded"] == ["cr-t0"]


def test_prewritten_success_record_stays_active_until_reconciled(tmp_path):
    """SN-C4：POST success 也不等于已确认；journal 要留到站内对账确认才 resolved。"""
    journal = tmp_path / "sss_uncertain.json"
    key = sss_uncertain.batch_key("2026-09-16", "excel", "acct")
    meta = {"delivery_date": "2026-09-16", "source": "excel",
            "account": "acct", "batch_started_at": time.time()}
    task = _task("t0")
    station = {"id": "s1", "receiveName": "张三", "receivePhone": "13800000001",
               "expectedDeliveryTime": "2026-09-16 11:00:00",
               "receiveAddress": {"doorNum": "A101", "addressDetail": ""},
               "created_at": int(time.time() * 1000)}
    fetch_count = 0

    def fetch(path):
        nonlocal fetch_count
        fetch_count += 1
        # initial 对账为空；提交后收尾对账可见站内记录。
        records = [station] if fetch_count >= 2 else []
        return {"success": True, "result": {"records": records, "total": len(records)}}

    def submit(payload):
        return {"success": True}

    def sink(entries, journal_meta):
        sss_uncertain.append_uncertain_records(journal, key, entries, meta=journal_meta)

    def clear(identifiers):
        sss_uncertain.resolve_uncertain_records(journal, key, identifiers)

    outcome: dict = {}
    final, reconciled = sss._run_reconciled_submission(
        [task], lambda: (submit, lambda: None), fetch, _Stop(), None, None,
        max_workers=1, uncertain_sink=sink, uncertain_clear=clear,
        uncertain_discard=lambda ids: None, journal_meta=meta, outcome=outcome)

    assert reconciled is True
    assert final is not None and final.confirmed == {"t0"}
    assert outcome["success_responses"] == ["t0"]
    assert sss_uncertain.pending_records(
        sss_uncertain.load_journal(journal)["records"], key) == []


def test_journal_write_failure_before_post_stops_all_posts():
    """SN-C4：POST 前置 journal 写盘失败时，连一个危险提交都不能派发。"""
    tasks = [_task("t0"), _task("t1", name="李四", phone="13800000002", door="B202")]
    calls: list[str] = []

    def submit(payload):
        calls.append(payload["tag"])
        if payload["tag"] == "t0":
            raise sss._SubmissionUncertain("ReadTimeout")
        return {"success": True}

    def broken_sink(entries, meta):
        raise OSError("disk full")

    stop = _Stop()
    outcome: dict = {}
    sss._run_reconciled_submission(
        tasks, lambda: (submit, lambda: None), _empty_fetch, stop, None, None,
        max_workers=2, uncertain_sink=broken_sink,
        journal_meta={"delivery_date": "2026-09-16"}, outcome=outcome)

    assert calls == []
    assert stop.is_set() is True
    assert "写入失败" in outcome["journal_error"]


def test_uncertain_record_is_written_before_post(tmp_path):
    """SN-C4：submit 被调用时 journal 里必须已经有 inflight/unresolved 记录。"""
    journal = tmp_path / "sss_uncertain.json"
    key = sss_uncertain.batch_key("2026-09-16", "excel", "acct")
    meta = {"delivery_date": "2026-09-16", "source": "excel",
            "account": "acct", "batch_started_at": time.time()}
    observed: list[int] = []

    def submit(payload):
        observed.append(len(sss_uncertain.pending_records(
            sss_uncertain.load_journal(journal)["records"], key)))
        raise sss._SubmissionUncertain("ReadTimeout")

    def sink(entries, journal_meta):
        sss_uncertain.append_uncertain_records(journal, key, entries, meta=journal_meta)

    sss._run_reconciled_submission(
        [_task("t0")], lambda: (submit, lambda: None), _empty_fetch, _Stop(),
        None, None, max_workers=1, uncertain_sink=sink, journal_meta=meta,
        uncertain_discard=lambda identifiers: None)

    assert observed == [1]
    pending = sss_uncertain.pending_records(
        sss_uncertain.load_journal(journal)["records"], key)
    assert len(pending) == 1 and pending[0]["status"] == "unresolved"



_TWO_PROCESS_CHILD_SCRIPT = r"""
import datetime as dt
import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

mode, work_dir, index = sys.argv[1], Path(sys.argv[2]), sys.argv[3]
work_dir.mkdir(parents=True, exist_ok=True)
excel = work_dir / (sys.argv[5] if len(sys.argv) > 5 else "闪时送.xlsx")
journal = Path(sys.argv[4]) if len(sys.argv) > 4 else work_dir / "sss_uncertain.json"
sss_url = sys.argv[6] if len(sys.argv) > 6 else "http://local.invalid"
account = sys.argv[7] if len(sys.argv) > 7 else "18758187837"
platform = work_dir / "platform.json"
hidden = work_dir / "hidden"
signal = work_dir / "signal"
barrier = work_dir / "barrier"
barrier.mkdir(parents=True, exist_ok=True)


class FixedDatetime(dt.datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 9, 16, 10, 0, 0)


from app.ordering import reconcile as sss_reconcile
from app.ordering import runner as sss_runner
from app.ordering import submission as sss_submission
from app.ordering import uncertain

sss_runner._dt.datetime = FixedDatetime


def _read_state():
    if not platform.exists():
        return {"records": [], "count": 0}
    return json.loads(platform.read_text(encoding="utf-8"))


def _mutate(mutator):
    with uncertain._journal_file_lock(platform):
        state = _read_state()
        mutator(state)
        tmp = platform.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, platform)


class FakeClient:
    def __init__(self, *args, **kwargs):
        pass

    def fetch_captcha(self):
        return b"png"

    def login(self, code):
        pass

    def get_json(self, path):
        if "get-login-user-account" in path:
            return {"success": True, "result": {"totalAmount": 500.0}}
        if "one-touch-send/list" in path:
            state = _read_state()
            records = [] if hidden.exists() else (state.get("records") or [])
            return {"success": True, "result": {"records": records, "total": len(records)}}
        raise AssertionError("unexpected GET " + path)

    def post_json(self, path, body=None):
        record = {
            "id": f"order-{index}",
            "receiveName": body["receiveName"],
            "receivePhone": body["receivePhone"],
            "expectedDeliveryTime": body["expectedDeliveryTime"],
            "orderType": body["orderType"],
            "storeId": body["storeId"],
            "goodsDetail": body["goodsDetail"],
            "receiveAddress": body["receiveAddress"],
            "account": account,
            "created_at": int(time.time() * 1000),
        }

        def mutator(state):
            state["count"] = int(state.get("count", 0)) + 1
            state.setdefault("records", []).append(record)

        _mutate(mutator)
        signal.write_text("1", encoding="utf-8")
        if mode == "crash":
            os._exit(17)
        if mode == "ambiguous":
            return {}
        return {"success": True}

    def fork(self):
        return self

    def close(self):
        pass


sss_runner.SssApiClient = FakeClient
sss_reconcile._SSS_SERVER_PREFILTER = False
sss_reconcile._RECONCILE_POLL_INTERVAL_S = 0.0
sss_submission._PREFILTER_ZERO_RETRY_DELAY_S = 0.0

if mode == "race":
    (barrier / f"ready.{index}").write_text("1", encoding="utf-8")
    deadline = time.time() + 20
    while len(list(barrier.glob("ready.*"))) < 2:
        if time.time() > deadline:
            raise SystemExit(20)
        time.sleep(0.02)


class Stop:
    def is_set(self):
        return False

    def set(self):
        pass


cfg = SimpleNamespace(
    sss_excel_path=str(excel), sss_order_source="excel",
    sss_account=account, sss_url=sss_url,
    sss_dry_run=False, sss_preflight=False,
    sss_store_name="一口轻食", sss_common_address="嗯哼",
    sss_use_fixed_address=True, sss_fixed_lnt=1.0, sss_fixed_lat=2.0,
    sss_fixed_area_code="330110", sss_fixed_address_detail="X",
    sss_product_name="轻食",
    sss_store_id=211053, sss_store_name_cached="一口轻食",
    sss_max_workers=2, sss_unit_price=1.9, sss_read_timeout_s=20.0,
    sss_idempotency_field="", sss_uncertain_path=str(journal),
)
result = sss_runner.run_sss_job(cfg, Stop(), lambda message: None,
                                password="x", captcha_callback=lambda img: "1234")
print(json.dumps(result, ensure_ascii=False, default=str))
"""


def _write_child_script(tmp_path: Path) -> Path:
    script = tmp_path / "sss_two_process_child.py"
    script.write_text(_TWO_PROCESS_CHILD_SCRIPT, encoding="utf-8")
    return script


def test_two_processes_same_batch_only_one_post(tmp_path):
    """SN-C1：两个真实子进程强制同时读取空 journal，平台模拟器只能收到 1 次 POST。"""
    work = tmp_path / "race"
    work.mkdir()
    _write_lunch_excel(work / "闪时送.xlsx")
    script = _write_child_script(tmp_path)
    processes = [
        subprocess.Popen([sys.executable, str(script), "race", str(work), str(index)],
                         cwd=str(_repo_root()),
                         env={**os.environ, "PYTHONPATH": str(_repo_root())})
        for index in (1, 2)
    ]
    assert [process.wait(timeout=40) for process in processes] == [0, 0]

    state = json.loads((work / "platform.json").read_text(encoding="utf-8"))
    assert state["count"] == 1
    assert len(state["records"]) == 1
    journal = work / "sss_uncertain.json"
    assert sss_uncertain.pending_records(
        sss_uncertain.load_journal(journal)["records"]) == []


def test_crash_after_post_second_process_does_not_repost(tmp_path):
    """SN-C1：首进程 POST 后崩溃，次进程持批次锁重读 journal/对账，不得盲目重发。"""
    work = tmp_path / "crash"
    work.mkdir()
    _write_lunch_excel(work / "闪时送.xlsx")
    script = _write_child_script(tmp_path)
    crash = subprocess.run([sys.executable, str(script), "crash", str(work), "1"],
                           cwd=str(_repo_root()),
                           env={**os.environ, "PYTHONPATH": str(_repo_root())},
                           capture_output=True, text=True, timeout=40)
    assert crash.returncode == 17, crash.stderr

    state = json.loads((work / "platform.json").read_text(encoding="utf-8"))
    assert state["count"] == 1
    journal = work / "sss_uncertain.json"
    pending = sss_uncertain.pending_records(
        sss_uncertain.load_journal(journal)["records"])
    assert len(pending) == 1
    assert pending[0]["status"] in {"inflight", "unresolved"}

    recover = subprocess.run([sys.executable, str(script), "recover", str(work), "2"],
                             cwd=str(_repo_root()),
                             env={**os.environ, "PYTHONPATH": str(_repo_root())},
                             capture_output=True, text=True, timeout=40)
    assert recover.returncode == 0, recover.stderr
    state = json.loads((work / "platform.json").read_text(encoding="utf-8"))
    assert state["count"] == 1
    assert sss_uncertain.pending_records(
        sss_uncertain.load_journal(journal)["records"]) == []


def test_two_processes_different_journals_only_one_post(tmp_path):
    """R2 红项 1：不同 sss_uncertain_path、同批次，权威共享状态只能 POST 1 次。"""
    work = tmp_path / "race-diff-journals"
    work.mkdir()
    _write_lunch_excel(work / "闪时送.xlsx")
    script = _write_child_script(tmp_path)
    processes = [
        subprocess.Popen(
            [sys.executable, str(script), "race", str(work), str(index),
             str(work / f"journal-{index}.json")],
            cwd=str(_repo_root()),
            env={**os.environ, "PYTHONPATH": str(_repo_root())})
        for index in (1, 2)
    ]
    assert [process.wait(timeout=40) for process in processes] == [0, 0]
    state = json.loads((work / "platform.json").read_text(encoding="utf-8"))
    assert state["count"] == 1
    assert len(state["records"]) == 1
    authority = Path(os.environ["YIKOU_SSS_AUTHORITATIVE_PATH"])
    assert sss_uncertain.pending_records(
        sss_uncertain.load_journal(authority)["records"]) == []


def test_crash_different_journal_second_does_not_repost(tmp_path):
    """R2 红项 1：首进程 POST 后崩溃，次进程换 journal 路径也不能重发。"""
    work = tmp_path / "crash-diff-journals"
    work.mkdir()
    (work / "hidden").write_text("1", encoding="utf-8")  # 站内列表延迟不可见
    _write_lunch_excel(work / "闪时送.xlsx")
    script = _write_child_script(tmp_path)
    crash = subprocess.run(
        [sys.executable, str(script), "crash", str(work), "1",
         str(work / "journal-a.json")],
        cwd=str(_repo_root()),
        env={**os.environ, "PYTHONPATH": str(_repo_root())},
        capture_output=True, text=True, timeout=40)
    assert crash.returncode == 17, crash.stderr
    first_state = json.loads((work / "platform.json").read_text(encoding="utf-8"))
    assert first_state["count"] == 1

    recover = subprocess.run(
        [sys.executable, str(script), "recover", str(work), "2",
         str(work / "journal-b.json")],
        cwd=str(_repo_root()),
        env={**os.environ, "PYTHONPATH": str(_repo_root())},
        capture_output=True, text=True, timeout=40)
    assert recover.returncode == 0, recover.stderr
    state = json.loads((work / "platform.json").read_text(encoding="utf-8"))
    assert state["count"] == 1
    authority = Path(os.environ["YIKOU_SSS_AUTHORITATIVE_PATH"])
    pending = sss_uncertain.pending_records(
        sss_uncertain.load_journal(authority)["records"])
    assert len(pending) == 1


def test_ambiguous_2xx_different_journals_second_does_not_repost(tmp_path):
    """R2 红项 1：模糊 2xx + 不同 journal，权威状态仍阻止第二实例重发。"""
    work = tmp_path / "ambiguous-diff-journals"
    work.mkdir()
    (work / "hidden").write_text("1", encoding="utf-8")
    _write_lunch_excel(work / "闪时送.xlsx")
    script = _write_child_script(tmp_path)
    first = subprocess.run(
        [sys.executable, str(script), "ambiguous", str(work), "1",
         str(work / "journal-a.json")],
        cwd=str(_repo_root()),
        env={**os.environ, "PYTHONPATH": str(_repo_root())},
        capture_output=True, text=True, timeout=40)
    assert first.returncode == 0, first.stderr
    state = json.loads((work / "platform.json").read_text(encoding="utf-8"))
    assert state["count"] == 1

    second = subprocess.run(
        [sys.executable, str(script), "recover", str(work), "2",
         str(work / "journal-b.json")],
        cwd=str(_repo_root()),
        env={**os.environ, "PYTHONPATH": str(_repo_root())},
        capture_output=True, text=True, timeout=40)
    assert second.returncode == 0, second.stderr
    state = json.loads((work / "platform.json").read_text(encoding="utf-8"))
    assert state["count"] == 1


def test_authoritative_path_independent_of_configured_journal(monkeypatch, tmp_path):
    monkeypatch.delenv("YIKOU_SSS_AUTHORITATIVE_PATH", raising=False)
    monkeypatch.setenv("YIKOU_SSS_AUTHORITATIVE_ROOT", str(tmp_path / "authority-root"))
    configured = tmp_path / "custom" / "journal.json"
    config = SimpleNamespace(sss_uncertain_path=str(configured),
                             sss_url="http://local.invalid",
                             sss_account="18758187837")
    authority = sss_uncertain.authoritative_uncertain_path(config)
    assert authority != configured.resolve()
    assert str(authority).startswith(str(tmp_path / "authority-root"))


def test_authoritative_path_ignores_journal_path_but_scopes_deployment(monkeypatch, tmp_path):
    monkeypatch.delenv("YIKOU_SSS_AUTHORITATIVE_PATH", raising=False)
    monkeypatch.setenv("YIKOU_SSS_AUTHORITATIVE_ROOT", str(tmp_path / "authority-root"))
    common = {"sss_url": "http://local.invalid", "sss_account": "18758187837",
              "sss_excel_path": str(tmp_path / "work" / "闪时送.xlsx")}
    first = SimpleNamespace(sss_uncertain_path=str(tmp_path / "a.json"), **common)
    second = SimpleNamespace(sss_uncertain_path=str(tmp_path / "b.json"), **common)
    assert (sss_uncertain.authoritative_uncertain_path(first)
            == sss_uncertain.authoritative_uncertain_path(second))
    other = SimpleNamespace(sss_uncertain_path=str(tmp_path / "c.json"),
                            **{**common, "sss_account": "18758187838"})
    assert (sss_uncertain.authoritative_uncertain_path(first)
            != sss_uncertain.authoritative_uncertain_path(other))


def test_legacy_journal_is_merged_into_authority_then_mirrored(monkeypatch, tmp_path):
    monkeypatch.setenv("YIKOU_SSS_AUTHORITATIVE_PATH", str(tmp_path / "authority.json"))
    legacy = tmp_path / "legacy.json"
    key = sss_uncertain.batch_key("2026-09-16", "excel", "18758187837")
    legacy.write_text(json.dumps({
        "version": 1,
        "records": [{
            "journal_id": "cr-legacy", "identifier": "第 3 行 张三",
            "batch_key": key, "delivery_date": "2026-09-16",
            "source": "excel", "account": "18758187837",
            "status": "unresolved", "fingerprint": {}, "error": "ReadTimeout",
        }],
    }, ensure_ascii=False), encoding="utf-8")
    authority = tmp_path / "authority.json"
    summary = sss_uncertain.merge_journals(authority, [legacy])
    assert summary["merged"] == 1
    assert [record["journal_id"] for record in sss_uncertain.pending_records(
        sss_uncertain.load_journal(authority)["records"], key)] == ["cr-legacy"]
    # 镜像后旧路径仍能看到 union，不会静默丢 unresolved。
    assert sss_uncertain.mirror_journal(authority, legacy) == 1
    assert [record["journal_id"] for record in sss_uncertain.pending_records(
        sss_uncertain.load_journal(legacy)["records"], key)] == ["cr-legacy"]


def test_corrupt_legacy_journal_fails_closed_with_manual_steps(tmp_path):
    legacy = tmp_path / "legacy.json"
    legacy.write_text("{not json", encoding="utf-8")
    with pytest.raises(sss_uncertain.UncertainJournalError) as excinfo:
        sss_uncertain.merge_journals(tmp_path / "authority.json", [legacy])
    message = str(excinfo.value)
    assert "无法安全迁移" in message
    assert str(legacy) in message
    assert "人工核对" in message
    assert "不要直接删除" in message


def test_platform_identity_matches_client_origin_and_ignores_workbook(monkeypatch, tmp_path):
    """SN-C1：权威身份使用实际 API origin，不受 workbook/URL 路径/斜杠影响。"""
    monkeypatch.delenv("YIKOU_SSS_AUTHORITATIVE_PATH", raising=False)
    monkeypatch.setenv("YIKOU_SSS_AUTHORITATIVE_ROOT", str(tmp_path / "root"))
    base = {"sss_account": "187 5818 7837"}
    a = SimpleNamespace(sss_url="https://example.invalid/takeout",
                        sss_excel_path=str(tmp_path / "a.xlsx"),
                        sss_uncertain_path=str(tmp_path / "ja.json"), **base)
    b = SimpleNamespace(sss_url="https://example.invalid/other/path/",
                        sss_excel_path=str(tmp_path / "b.xlsx"),
                        sss_uncertain_path=str(tmp_path / "jb.json"), **base)
    assert (sss_uncertain.authoritative_uncertain_path(a)
            == sss_uncertain.authoritative_uncertain_path(b)), "origin 相同、workbook/路径不同必须同权威"
    source_variant = SimpleNamespace(
        sss_url="https://example.invalid/takeout",
        sss_excel_path=str(tmp_path / "a.xlsx"),
        sss_uncertain_path=str(tmp_path / "je.json"),
        sss_order_source="wps", **base)
    assert (sss_uncertain.authoritative_uncertain_path(a)
            == sss_uncertain.authoritative_uncertain_path(source_variant)), "名单来源变化不能改变权威状态"
    different_host = SimpleNamespace(
        sss_url="https://other.invalid/takeout",
        sss_excel_path=str(tmp_path / "a.xlsx"),
        sss_uncertain_path=str(tmp_path / "jc.json"), **base)
    assert (sss_uncertain.authoritative_uncertain_path(a)
            != sss_uncertain.authoritative_uncertain_path(different_host))
    different_account = SimpleNamespace(
        sss_url="https://example.invalid/takeout",
        sss_excel_path=str(tmp_path / "a.xlsx"),
        sss_uncertain_path=str(tmp_path / "jd.json"),
        sss_account="18758187838")
    assert (sss_uncertain.authoritative_uncertain_path(a)
            != sss_uncertain.authoritative_uncertain_path(different_account))


def test_legacy_env_journal_path_is_collected_and_migrated(monkeypatch, tmp_path):
    legacy = tmp_path / "legacy-env.json"
    monkeypatch.setenv("YIKOU_SSS_UNCERTAIN_PATH", str(legacy))
    monkeypatch.setenv("YIKOU_DATA_DIR", str(tmp_path / "userdata"))
    key = sss_uncertain.batch_key("2026-09-16", "excel", "18758187837")
    sss_uncertain.append_uncertain_records(
        legacy, key,
        [{"identifier": "env-record", "client_request_id": "cr-env",
          "fingerprint": {}, "error": "ReadTimeout"}],
        meta={"delivery_date": "2026-09-16", "source": "excel",
              "account": "18758187837", "platform": "https://example.invalid"})
    authority = tmp_path / "authority.json"
    paths = sss_uncertain.legacy_uncertain_paths(SimpleNamespace(), authority)
    assert legacy in paths
    summary = sss_uncertain.merge_journals(
        authority, [legacy],
        scope={"platform": "https://example.invalid", "account": "18758187837"})
    assert summary["merged"] == 1
    pending = sss_uncertain.pending_records(
        sss_uncertain.load_journal(authority)["records"], key)
    assert [record["journal_id"] for record in pending] == ["cr-env"]


def test_prior_hash_authority_is_collected_and_scope_filtered(monkeypatch, tmp_path):
    userdata = tmp_path / "userdata"
    monkeypatch.setenv("YIKOU_DATA_DIR", str(userdata))
    old_dir = userdata / "sss_authoritative"
    old_dir.mkdir(parents=True)
    old_file = old_dir / "oldhash.json"
    key = sss_uncertain.batch_key("2026-09-16", "excel", "18758187837")
    sss_uncertain.append_uncertain_records(
        old_file, key,
        [{"identifier": "oldhash", "client_request_id": "cr-oldhash",
          "fingerprint": {}, "error": "ReadTimeout"}],
        meta={"delivery_date": "2026-09-16", "source": "excel",
              "account": "18758187837", "platform": "https://example.invalid"})
    authority = tmp_path / "authority.json"
    paths = sss_uncertain.legacy_uncertain_paths(SimpleNamespace(), authority)
    assert old_file in paths
    assert str(old_file) in sss_uncertain.merge_journals(
        authority, [old_file],
        scope={"platform": "https://example.invalid",
               "account": "18758187837"})["sources"]
    # 不同平台/账号的旧记录不能被并入当前权威。
    other_dir = userdata / "sss_authoritative"
    other_file = other_dir / "other.json"
    sss_uncertain.append_uncertain_records(
        other_file, key,
        [{"identifier": "other", "client_request_id": "cr-other",
          "fingerprint": {}, "error": "ReadTimeout"}],
        meta={"delivery_date": "2026-09-16", "source": "excel",
              "account": "18758187837", "platform": "https://other.invalid"})
    summary = sss_uncertain.merge_journals(
        authority, [other_file],
        scope={"platform": "https://example.invalid",
               "account": "18758187837"})
    assert summary["merged"] == 0
    other_account = other_dir / "other-account.json"
    sss_uncertain.append_uncertain_records(
        other_account, key,
        [{"identifier": "other-account", "client_request_id": "cr-other-account",
          "fingerprint": {}, "error": "ReadTimeout"}],
        meta={"delivery_date": "2026-09-16", "source": "excel",
              "account": "18758187838", "platform": "https://example.invalid"})
    summary = sss_uncertain.merge_journals(
        authority, [other_account],
        scope={"platform": "https://example.invalid",
               "account": "18758187837"})
    assert summary["merged"] == 0


def test_legacy_record_without_platform_fails_closed(tmp_path):
    good = tmp_path / "good.json"
    key = sss_uncertain.batch_key("2026-09-16", "excel", "18758187837")
    sss_uncertain.append_uncertain_records(
        good, key,
        [{"identifier": "legacy-1", "client_request_id": "cr-legacy-1",
          "fingerprint": {}, "error": "ReadTimeout"}],
        meta={"delivery_date": "2026-09-16", "source": "excel",
              "account": "18758187837"})
    with pytest.raises(sss_uncertain.UncertainJournalError) as excinfo:
        sss_uncertain.merge_journals(
            tmp_path / "authority.json", [good],
            scope={"platform": "https://example.invalid",
                   "account": "18758187837"})
    message = str(excinfo.value)
    assert "无法安全判定平台/账号" in message
    assert "人工核对" in message
    authority = tmp_path / "authority.json"
    preserved = sss_uncertain.load_journal(authority)["records"]
    assert [record["journal_id"] for record in preserved] == ["cr-legacy-1"]
    assert preserved[0]["scope_unknown"] is True


def test_journal_record_validation_fails_closed(tmp_path):
    cases = {
        "null_record": {"version": 1, "records": [None]},
        "missing_identity": {"version": 1, "records": [{
            "batch_key": "2026-09-16|18758187837", "fingerprint": {}}]},
        "missing_batch": {"version": 1, "records": [{
            "journal_id": "cr-1", "fingerprint": {}}]},
        "unknown_version": {"version": 99, "records": []},
        "mixed": {"version": 1, "records": [
            {"journal_id": "ok", "batch_key": "2026-09-16|18758187837",
             "fingerprint": {}},
            None,
        ]},
    }
    for name, payload in cases.items():
        path = tmp_path / f"{name}.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(sss_uncertain.UncertainJournalError):
            sss_uncertain.load_journal(path)
    empty = tmp_path / "empty.json"
    empty.write_text(json.dumps({"version": 1, "records": []}), encoding="utf-8")
    assert sss_uncertain.load_journal(empty)["records"] == []
    legacy_no_version = tmp_path / "legacy.json"
    legacy_no_version.write_text(json.dumps({"records": []}), encoding="utf-8")
    assert sss_uncertain.load_journal(legacy_no_version)["version"] == 1


def test_workbook_change_without_authority_path_override_only_one_post(monkeypatch, tmp_path):
    """生产身份/路径生成回归：不同 workbook + 不同 journal，无 PATH 覆盖时 POST=1。"""
    monkeypatch.delenv("YIKOU_SSS_AUTHORITATIVE_PATH", raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    work = tmp_path / "workbook-identity"
    work.mkdir()
    _write_lunch_excel(work / "workbook-1.xlsx")
    _write_lunch_excel(work / "workbook-2.xlsx")
    script = _write_child_script(tmp_path)
    processes = [
        subprocess.Popen(
            [sys.executable, str(script), "race", str(work), str(index),
             str(work / f"journal-{index}.json"), f"workbook-{index}.xlsx"],
            cwd=str(_repo_root()),
            env={**os.environ, "PYTHONPATH": str(_repo_root())})
        for index in (1, 2)
    ]
    assert [process.wait(timeout=40) for process in processes] == [0, 0]
    state = json.loads((work / "platform.json").read_text(encoding="utf-8"))
    assert state["count"] == 1
    assert len(state["records"]) == 1


def test_workbook_change_crash_without_authority_path_override_does_not_repost(monkeypatch, tmp_path):
    """生产身份回归：workbook 变化 + 首进程 POST 后崩溃，次实例不能重发。"""
    monkeypatch.delenv("YIKOU_SSS_AUTHORITATIVE_PATH", raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    work = tmp_path / "workbook-crash"
    work.mkdir()
    (work / "hidden").write_text("1", encoding="utf-8")
    _write_lunch_excel(work / "workbook-a.xlsx")
    _write_lunch_excel(work / "workbook-b.xlsx")
    script = _write_child_script(tmp_path)
    env = {**os.environ, "PYTHONPATH": str(_repo_root())}
    crash = subprocess.run(
        [sys.executable, str(script), "crash", str(work), "1",
         str(work / "journal-a.json"), "workbook-a.xlsx"],
        cwd=str(_repo_root()), env=env, capture_output=True, text=True, timeout=40)
    assert crash.returncode == 17, crash.stderr
    assert json.loads((work / "platform.json").read_text(encoding="utf-8"))["count"] == 1
    recover = subprocess.run(
        [sys.executable, str(script), "recover", str(work), "2",
         str(work / "journal-b.json"), "workbook-b.xlsx"],
        cwd=str(_repo_root()), env=env, capture_output=True, text=True, timeout=40)
    assert recover.returncode == 0, recover.stderr
    assert json.loads((work / "platform.json").read_text(encoding="utf-8"))["count"] == 1


def test_origin_normalization_matches_client_rules():
    from app.integrations.api_client import origin_from_url

    assert origin_from_url("HTTPS://EXAMPLE.INVALID:443/other") == "https://example.invalid"
    assert origin_from_url("https://example.invalid/takeout") == "https://example.invalid"
    assert origin_from_url("HTTP://EXAMPLE.INVALID:80/a?x=1#f") == "http://example.invalid"
    assert origin_from_url("http://Example.COM:8080/a") == "http://example.com:8080"
    assert origin_from_url("http://[2001:DB8::1]:8080/x?y=1") == "http://[2001:db8::1]:8080"
    assert origin_from_url("https://[::1]/x") == "https://[::1]"
    assert origin_from_url("https://example.invalid:443/x") == "https://example.invalid"
    assert origin_from_url("https://example.invalid:8443/x") != origin_from_url("https://example.invalid/x")
    assert origin_from_url("http://example.invalid/x") != origin_from_url("https://example.invalid/x")
    assert origin_from_url("https://other.invalid/x") != origin_from_url("https://example.invalid/x")


def test_authority_scope_uses_shared_origin_and_isolates_real_differences(monkeypatch, tmp_path):
    monkeypatch.delenv("YIKOU_SSS_AUTHORITATIVE_PATH", raising=False)
    monkeypatch.setenv("YIKOU_SSS_AUTHORITATIVE_ROOT", str(tmp_path / "root"))
    base = {"sss_account": "18758187837"}
    lower = SimpleNamespace(sss_url="https://example.invalid/takeout", **base)
    upper = SimpleNamespace(sss_url="HTTPS://EXAMPLE.INVALID:443/other", **base)
    assert (sss_uncertain.authoritative_uncertain_path(lower)
            == sss_uncertain.authoritative_uncertain_path(upper))
    for changed in ({"sss_url": "http://example.invalid/takeout"},
                    {"sss_url": "https://other.invalid/takeout"},
                    {"sss_url": "https://example.invalid:8443/takeout"},
                    {"sss_url": "https://example.invalid/takeout",
                     "sss_account": "18758187838"}):
        assert (sss_uncertain.authoritative_uncertain_path(lower)
                != sss_uncertain.authoritative_uncertain_path(SimpleNamespace(**changed)))


@pytest.mark.parametrize("first_url,second_url", [
    ("HTTPS://EXAMPLE.INVALID:443/other", "https://example.invalid/takeout"),
    ("https://example.invalid/takeout", "HTTPS://EXAMPLE.INVALID:443/other"),
    ("HTTP://EXAMPLE.INVALID:80/a", "http://example.invalid/b"),
])
def test_equivalent_origin_crash_does_not_repost(monkeypatch, tmp_path, first_url, second_url):
    """A/B/C：等价 origin（大小写/默认端口）POST 后崩溃，次实例不能重发。"""
    monkeypatch.delenv("YIKOU_SSS_AUTHORITATIVE_PATH", raising=False)
    monkeypatch.setenv("YIKOU_SSS_AUTHORITATIVE_ROOT", str(tmp_path / "authority-root"))
    work = tmp_path / "origin-crash"
    work.mkdir()
    (work / "hidden").write_text("1", encoding="utf-8")
    _write_lunch_excel(work / "wb-a.xlsx")
    _write_lunch_excel(work / "wb-b.xlsx")
    script = _write_child_script(tmp_path)
    env = {**os.environ, "PYTHONPATH": str(_repo_root())}
    crash = subprocess.run(
        [sys.executable, str(script), "crash", str(work), "1",
         str(work / "journal-a.json"), "wb-a.xlsx", first_url],
        cwd=str(_repo_root()), env=env, capture_output=True, text=True, timeout=40)
    assert crash.returncode == 17, crash.stderr
    assert json.loads((work / "platform.json").read_text(encoding="utf-8"))["count"] == 1
    recover = subprocess.run(
        [sys.executable, str(script), "recover", str(work), "2",
         str(work / "journal-b.json"), "wb-b.xlsx", second_url],
        cwd=str(_repo_root()), env=env, capture_output=True, text=True, timeout=40)
    assert recover.returncode == 0, recover.stderr
    assert json.loads((work / "platform.json").read_text(encoding="utf-8"))["count"] == 1


def test_non_default_ports_and_accounts_are_isolated_and_can_post(monkeypatch, tmp_path):
    """D：非默认端口/不同账号属于不同安全域，每个域允许合法的一次 POST。"""
    monkeypatch.delenv("YIKOU_SSS_AUTHORITATIVE_PATH", raising=False)
    monkeypatch.setenv("YIKOU_SSS_AUTHORITATIVE_ROOT", str(tmp_path / "authority-root"))
    script = _write_child_script(tmp_path)
    env = {**os.environ, "PYTHONPATH": str(_repo_root())}

    # 不同非默认端口：各自独立平台目录，各自 POST=1。
    port_counts: list[int] = []
    for index, sss_url in ((1, "https://example.invalid:8443/a"),
                           (2, "https://example.invalid:9443/b")):
        work = tmp_path / f"port-{index}"
        work.mkdir()
        _write_lunch_excel(work / "闪时送.xlsx")
        child = subprocess.run(
            [sys.executable, str(script), "recover", str(work), str(index),
             str(work / "journal.json"), "闪时送.xlsx", sss_url, "18758187837"],
            cwd=str(_repo_root()), env=env, capture_output=True, text=True, timeout=40)
        assert child.returncode == 0, child.stderr
        port_counts.append(json.loads(
            (work / "platform.json").read_text(encoding="utf-8"))["count"])
    assert port_counts == [1, 1]

    # 不同账号：同一平台同一订单内容，两个账号各自合法 POST 一次。
    work = tmp_path / "accounts"
    work.mkdir()
    _write_lunch_excel(work / "闪时送.xlsx")
    for index, account in ((1, "111"), (2, "222")):
        child = subprocess.run(
            [sys.executable, str(script), "recover", str(work), str(index),
             str(work / f"journal-{index}.json"), "闪时送.xlsx",
             "http://local.invalid", account],
            cwd=str(_repo_root()), env=env, capture_output=True, text=True, timeout=40)
        assert child.returncode == 0, child.stderr
    assert json.loads((work / "platform.json").read_text(encoding="utf-8"))["count"] == 2


def _legacy_hash_record(hash_dir: Path, filename: str, account: str,
                        platform: str = "http://local.invalid",
                        with_platform: bool = True) -> Path:
    path = hash_dir / filename
    key = sss_uncertain.batch_key("2026-09-16", "excel", account)
    meta = {"delivery_date": "2026-09-16", "source": "excel", "account": account}
    if with_platform:
        meta["platform"] = platform
    sss_uncertain.append_uncertain_records(
        path, key,
        [{"identifier": f"id-{account}", "client_request_id": f"cr-{account}",
          "fingerprint": {}, "error": "ReadTimeout"}],
        meta=meta)
    return path


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_legacy_hash_files_are_readonly_sources_not_mirrors(monkeypatch, tmp_path):
    """E/F/G：旧哈希目录只读迁移；其他账号/平台文件字节级不变。"""
    monkeypatch.delenv("YIKOU_SSS_AUTHORITATIVE_PATH", raising=False)
    userdata = tmp_path / "userdata"
    hash_dir = userdata / "sss_authoritative"
    hash_dir.mkdir(parents=True)
    monkeypatch.setenv("YIKOU_DATA_DIR", str(userdata))
    monkeypatch.setenv("YIKOU_SSS_AUTHORITATIVE_ROOT", str(tmp_path / "authority-root"))
    account_a = _legacy_hash_record(hash_dir, "account-a.json", "111")
    account_b = _legacy_hash_record(hash_dir, "account-b.json", "222")
    other_platform = _legacy_hash_record(
        hash_dir, "other-platform.json", "111", platform="http://other.invalid")
    before = {path.name: _sha256(path)
              for path in (account_a, account_b, other_platform)}

    work = tmp_path / "legacy-hash-work"
    work.mkdir()
    (work / "hidden").write_text("1", encoding="utf-8")
    _write_lunch_excel(work / "闪时送.xlsx")
    script = _write_child_script(tmp_path)
    env = {**os.environ, "PYTHONPATH": str(_repo_root())}
    first = subprocess.run(
        [sys.executable, str(script), "recover", str(work), "1",
         str(work / "journal-a.json"), "闪时送.xlsx", "http://local.invalid", "111"],
        cwd=str(_repo_root()), env=env, capture_output=True, text=True, timeout=40)
    assert first.returncode == 0, first.stderr
    platform_file = work / "platform.json"
    first_count = (json.loads(platform_file.read_text(encoding="utf-8"))["count"]
                   if platform_file.exists() else 0)
    assert first_count == 0
    for path in (account_a, account_b, other_platform):
        assert _sha256(path) == before[path.name], f"{path.name} 被镜像覆盖/改写"

    second = subprocess.run(
        [sys.executable, str(script), "recover", str(work), "2",
         str(work / "journal-b.json"), "闪时送.xlsx", "http://local.invalid", "222"],
        cwd=str(_repo_root()), env=env, capture_output=True, text=True, timeout=40)
    assert second.returncode == 0, second.stderr
    second_count = (json.loads(platform_file.read_text(encoding="utf-8"))["count"]
                    if platform_file.exists() else 0)
    assert second_count == 0
    for path in (account_a, account_b, other_platform):
        assert _sha256(path) == before[path.name], f"{path.name} 被镜像覆盖/改写"


def test_corrupt_legacy_hash_file_not_overwritten_and_zero_post(monkeypatch, tmp_path):
    """H：损坏旧文件 fail-closed、不覆盖、零 POST。"""
    monkeypatch.delenv("YIKOU_SSS_AUTHORITATIVE_PATH", raising=False)
    userdata = tmp_path / "userdata"
    hash_dir = userdata / "sss_authoritative"
    hash_dir.mkdir(parents=True)
    monkeypatch.setenv("YIKOU_DATA_DIR", str(userdata))
    monkeypatch.setenv("YIKOU_SSS_AUTHORITATIVE_ROOT", str(tmp_path / "authority-root"))
    corrupt = hash_dir / "account-corrupt.json"
    corrupt.write_text("{not json", encoding="utf-8")
    other = _legacy_hash_record(hash_dir, "account-other.json", "222")
    before_corrupt = _sha256(corrupt)
    before_other = _sha256(other)

    work = tmp_path / "corrupt-work"
    work.mkdir()
    (work / "hidden").write_text("1", encoding="utf-8")
    _write_lunch_excel(work / "闪时送.xlsx")
    script = _write_child_script(tmp_path)
    env = {**os.environ, "PYTHONPATH": str(_repo_root())}
    child = subprocess.run(
        [sys.executable, str(script), "recover", str(work), "1",
         str(work / "journal-a.json"), "闪时送.xlsx", "http://local.invalid", "111"],
        cwd=str(_repo_root()), env=env, capture_output=True, text=True, timeout=40)
    assert child.returncode == 0, child.stderr
    platform_file = work / "platform.json"
    child_count = (json.loads(platform_file.read_text(encoding="utf-8"))["count"]
                   if platform_file.exists() else 0)
    assert child_count == 0
    assert _sha256(corrupt) == before_corrupt
    assert _sha256(other) == before_other


def test_unknown_legacy_hash_file_quarantined_not_overwritten_zero_post(monkeypatch, tmp_path):
    """H：归属未知旧文件必须 fail-closed、不覆盖、不放行 POST。"""
    monkeypatch.delenv("YIKOU_SSS_AUTHORITATIVE_PATH", raising=False)
    userdata = tmp_path / "userdata"
    hash_dir = userdata / "sss_authoritative"
    hash_dir.mkdir(parents=True)
    monkeypatch.setenv("YIKOU_DATA_DIR", str(userdata))
    monkeypatch.setenv("YIKOU_SSS_AUTHORITATIVE_ROOT", str(tmp_path / "authority-root"))
    unknown = _legacy_hash_record(hash_dir, "account-unknown.json", "111",
                                  with_platform=False)
    other = _legacy_hash_record(hash_dir, "account-other.json", "222")
    before_unknown = _sha256(unknown)
    before_other = _sha256(other)

    work = tmp_path / "unknown-work"
    work.mkdir()
    (work / "hidden").write_text("1", encoding="utf-8")
    _write_lunch_excel(work / "闪时送.xlsx")
    script = _write_child_script(tmp_path)
    env = {**os.environ, "PYTHONPATH": str(_repo_root())}
    child = subprocess.run(
        [sys.executable, str(script), "recover", str(work), "1",
         str(work / "journal-a.json"), "闪时送.xlsx", "http://local.invalid", "111"],
        cwd=str(_repo_root()), env=env, capture_output=True, text=True, timeout=40)
    assert child.returncode == 0, child.stderr
    platform_file = work / "platform.json"
    count = (json.loads(platform_file.read_text(encoding="utf-8"))["count"]
             if platform_file.exists() else 0)
    assert count == 0
    assert _sha256(unknown) == before_unknown
    assert _sha256(other) == before_other


def test_mirror_targets_never_include_legacy_hash_sources(monkeypatch, tmp_path):
    userdata = tmp_path / "userdata"
    hash_dir = userdata / "sss_authoritative"
    hash_dir.mkdir(parents=True)
    monkeypatch.setenv("YIKOU_DATA_DIR", str(userdata))
    old_file = _legacy_hash_record(hash_dir, "old.json", "111")
    configured = tmp_path / "configured.json"
    config = SimpleNamespace(sss_uncertain_path=str(configured))
    authority = tmp_path / "authority.json"
    sources = sss_uncertain.legacy_uncertain_paths(config, authority)
    mirrors = sss_uncertain.mirror_uncertain_paths(config, authority)
    assert old_file in sources
    assert old_file not in mirrors
    assert configured in mirrors


def test_atomic_write_fsyncs_file_and_parent_dir(monkeypatch, tmp_path):
    journal = tmp_path / "fsync.json"
    key = sss_uncertain.batch_key("2026-09-16", "excel", "acct")
    opened: list[tuple[str, int]] = []
    fsynced: list[int] = []
    real_open = sss_uncertain.os.open
    real_fsync = sss_uncertain.os.fsync

    def spy_open(path, flags, *args, **kwargs):
        opened.append((str(path), flags))
        return real_open(path, flags, *args, **kwargs)

    def spy_fsync(fd):
        fsynced.append(fd)
        return real_fsync(fd)

    monkeypatch.setattr(sss_uncertain.os, "open", spy_open)
    monkeypatch.setattr(sss_uncertain.os, "fsync", spy_fsync)
    sss_uncertain.append_uncertain_records(
        journal, key,
        [{"identifier": "t0", "client_request_id": "cr-t0",
          "fingerprint": {}, "error": "ReadTimeout"}],
        meta={"delivery_date": "2026-09-16", "source": "excel",
              "account": "acct", "platform": "http://local.invalid"})
    assert len(fsynced) >= 2, "文件和父目录都必须 fsync"
    assert any(flags & getattr(sss_uncertain.os, "O_DIRECTORY", 0)
               for _path, flags in opened), "必须 fsync 父目录"


def test_atomic_write_temp_creation_failure_wrapped(monkeypatch, tmp_path):
    journal = tmp_path / "journal.json"
    key = sss_uncertain.batch_key("2026-09-16", "excel", "acct")

    def denied(*args, **kwargs):
        raise PermissionError("directory not writable")

    monkeypatch.setattr(sss_uncertain.tempfile, "mkstemp", denied)
    with pytest.raises(sss_uncertain.UncertainJournalError, match="目录不可写"):
        sss_uncertain.append_uncertain_records(
            journal, key,
            [{"identifier": "t0", "client_request_id": "cr-t0",
              "fingerprint": {}, "error": "ReadTimeout"}],
            meta={"delivery_date": "2026-09-16", "source": "excel",
                  "account": "acct", "platform": "http://local.invalid"})
    assert not journal.exists()


def test_atomic_write_replace_failure_preserves_original(monkeypatch, tmp_path):
    journal = tmp_path / "journal.json"
    key = sss_uncertain.batch_key("2026-09-16", "excel", "acct")
    meta = {"delivery_date": "2026-09-16", "source": "excel",
            "account": "acct", "platform": "http://local.invalid"}
    sss_uncertain.append_uncertain_records(
        journal, key,
        [{"identifier": "t0", "client_request_id": "cr-t0",
          "fingerprint": {}, "error": "ReadTimeout"}], meta=meta)
    before = journal.read_bytes()

    def broken_replace(src, dst):
        raise OSError("replace failed")

    monkeypatch.setattr(sss_uncertain.os, "replace", broken_replace)
    with pytest.raises(sss_uncertain.UncertainJournalError, match="无法原子写入"):
        sss_uncertain.append_uncertain_records(
            journal, key,
            [{"identifier": "t1", "client_request_id": "cr-t1",
              "fingerprint": {}, "error": "ReadTimeout"}], meta=meta)
    assert journal.read_bytes() == before


def test_atomic_write_parent_fsync_failure_blocks(monkeypatch, tmp_path):
    journal = tmp_path / "journal.json"
    key = sss_uncertain.batch_key("2026-09-16", "excel", "acct")

    def broken_fsync(path):
        raise OSError("fsync unsupported")

    monkeypatch.setattr(sss_uncertain, "_fsync_parent_dir", broken_fsync)
    with pytest.raises(sss_uncertain.UncertainJournalError, match="父目录 fsync 失败"):
        sss_uncertain.append_uncertain_records(
            journal, key,
            [{"identifier": "t0", "client_request_id": "cr-t0",
              "fingerprint": {}, "error": "ReadTimeout"}],
            meta={"delivery_date": "2026-09-16", "source": "excel",
                  "account": "acct", "platform": "http://local.invalid"})


def test_batch_lock_root_does_not_depend_on_tmpdir(monkeypatch, tmp_path):
    monkeypatch.delenv("YIKOU_SSS_LOCK_ROOT", raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("TMPDIR", str(tmp_path / "tmp-a"))
    first = sss_uncertain.batch_submission_lock(
        tmp_path / "journal.json", sss_uncertain.batch_key("2026-09-16", "excel", "111"))
    monkeypatch.setenv("TMPDIR", str(tmp_path / "tmp-b"))
    monkeypatch.setenv("YIKOU_SSS_AUTHORITATIVE_ROOT", str(tmp_path / "other-root"))
    second = sss_uncertain.batch_submission_lock(
        tmp_path / "journal.json", sss_uncertain.batch_key("2026-09-16", "excel", "111"))
    assert first.lock_path == second.lock_path
    assert str(first.lock_path).startswith(str(tmp_path / "state"))
    assert str(tmp_path / "tmp-a") not in str(first.lock_path)
    assert str(tmp_path / "tmp-b") not in str(first.lock_path)


def test_load_journal_json_syntax_error_is_not_empty(tmp_path):
    """M4：JSON 语法损坏不得当空 journal 放行。"""
    broken = tmp_path / "broken.json"
    broken.write_bytes(b'{"version": 1, "records": [')
    with pytest.raises(sss_uncertain.UncertainJournalError, match="损坏"):
        sss_uncertain.load_journal(broken)


def test_timeout_classification_is_uncertain_not_explicit_failure():
    """M6：POST 超时必须是 uncertain，不能归为可重发的显式失败。"""
    class Client:
        def post_json(self, path, body):
            raise TimeoutError("ReadTimeout")

    with pytest.raises(sss._SubmissionUncertain):
        sss._post_one(Client(), {})
    result = sss._submit_tasks_concurrent(
        [{"identifier": "t0", "payload": {}}],
        lambda payload: sss._post_one(Client(), payload), _Stop(), None, max_workers=1)
    assert result.failures == []
    assert [identifier for identifier, _detail in result.uncertain] == ["t0"]


def test_batch_matching_enforces_date_and_account_scope():
    """M8：批次匹配不得忽略日期/账号范围。"""
    record = {"journal_id": "r1", "batch_key": "2026-09-16|111",
              "delivery_date": "2026-09-16", "account": "111",
              "fingerprint": {}, "status": "unresolved"}
    assert sss_uncertain.pending_records(
        [record], sss_uncertain.batch_key("2026-09-16", "excel", "111"))
    assert sss_uncertain.pending_records(
        [record], sss_uncertain.batch_key("2026-09-17", "excel", "111")) == []
    assert sss_uncertain.pending_records(
        [record], sss_uncertain.batch_key("2026-09-16", "excel", "222")) == []


def _write_lunch_excel(path: Path) -> None:
    wb = Workbook()
    ws = wb.active
    ws.title = "午餐"
    ws.append(["午餐", None, None, None])
    ws.append(["姓名", "门牌号", "电话", "送达时间"])
    ws.append(["张三", "A101", "13800000001", "11:00"])
    wb.save(path)
    wb.close()


def _blocked_config(tmp_path, journal_path, account="18758187837"):
    return SimpleNamespace(
        sss_excel_path=str(tmp_path / "闪时送.xlsx"), sss_order_source="excel",
        sss_account=account, sss_dry_run=False, sss_preflight=False,
        sss_store_name="一口轻食", sss_common_address="嗯哼",
        sss_use_fixed_address=True, sss_fixed_lnt=1.0, sss_fixed_lat=2.0,
        sss_fixed_area_code="330110", sss_fixed_address_detail="X",
        sss_product_name="轻食", sss_url="https://example.invalid",
        sss_store_id=211053, sss_store_name_cached="一口轻食",
        sss_max_workers=2, sss_unit_price=1.9, sss_read_timeout_s=20.0,
        sss_idempotency_field="", sss_uncertain_path=str(journal_path),
    )


def _blocked_fake_client(post_records, list_records):
    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def fetch_captcha(self):
            return b"\x89PNG fake"

        def login(self, code):
            pass

        def get_json(self, path):
            if "get-login-user-account" in path:
                return {"success": True,
                        "result": {"totalAmount": 500.0, "freezeAmount": 0.0}}
            if "one-touch-send/list" in path:
                return {"success": True,
                        "result": {"records": list(list_records), "total": len(list_records)}}
            raise AssertionError(f"未预期的 GET：{path}")

        def post_json(self, path, body=None):
            post_records.append((path, body))
            raise AssertionError("有未解决不确定记录时绝不能发送 POST")

        def close(self):
            pass

    return FakeClient


def test_run_sss_job_timeout_records_unknown_and_never_resends(monkeypatch, tmp_path):
    """端到端：POST 超时→uncertain→写本地记录→只读对账仍缺失→不自动/人工重发。"""
    excel = tmp_path / "闪时送.xlsx"
    _write_lunch_excel(excel)
    journal = tmp_path / "sss_uncertain.json"
    post_count: list[int] = []

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def fetch_captcha(self):
            return b"\x89PNG fake"

        def login(self, code):
            pass

        def get_json(self, path):
            if "get-login-user-account" in path:
                return {"success": True,
                        "result": {"totalAmount": 500.0, "freezeAmount": 0.0}}
            if "one-touch-send/list" in path:
                return {"success": True, "result": {"records": [], "total": 0}}
            raise AssertionError(f"未预期的 GET：{path}")

        def post_json(self, path, body=None):
            post_count.append(1)
            raise TimeoutError("ReadTimeout")

        def fork(self):
            return self

        def close(self):
            pass

    monkeypatch.setattr(sss_runner, "SssApiClient", FakeClient)
    cfg = _blocked_config(tmp_path, journal)
    logs: list[str] = []
    result = sss.run_sss_job(cfg, _Stop(), logs.append, password="x",
                             captcha_callback=lambda img: "1234")

    assert post_count == [1]
    assert result["status"] == "unconfirmed"
    assert result["created"] == 0 and result["reconciled"] is True
    assert result["partial"] is True
    assert result["summary"]["uncertain"] == 1
    assert result["summary"]["success_responses"] == 0
    assert "只读" in result["next_action"]
    pending = sss_uncertain.pending_records(sss_uncertain.load_journal(journal)["records"])
    assert len(pending) == 1
    assert pending[0]["error"] == "ReadTimeout"


def test_run_sss_job_batch_lock_failure_blocks_all_posts(monkeypatch, tmp_path):
    """SN-C1：批次级锁创建/获取失败时，runner 必须零 POST 返回明确 block。"""
    excel = tmp_path / "闪时送.xlsx"
    _write_lunch_excel(excel)
    journal = tmp_path / "sss_uncertain.json"
    posts: list = []
    monkeypatch.setattr(sss_runner, "SssApiClient",
                        _blocked_fake_client(posts, []))

    class BrokenLock:
        def acquire(self):
            raise sss_uncertain.UncertainJournalError("lock dir unavailable")

        def release(self):
            pass

    monkeypatch.setattr(sss_runner, "batch_submission_lock",
                        lambda *args, **kwargs: BrokenLock())
    cfg = _blocked_config(tmp_path, journal)
    result = sss.run_sss_job(cfg, _Stop(), [], password="x",
                             captcha_callback=lambda img: "1234")

    assert result["status"] == "blocked_concurrent"
    assert result["stopped"] is True
    assert posts == []
    assert "跨进程锁" in result["next_action"] or "另一进程" in result["next_action"]


def test_run_sss_job_journal_fsync_failure_blocks_all_posts(monkeypatch, tmp_path):
    """R6-2：journal 父目录 fsync 失败时零 POST、明确 failed。"""
    excel = tmp_path / "闪时送.xlsx"
    _write_lunch_excel(excel)
    journal = tmp_path / "sss_uncertain.json"
    posts: list = []
    monkeypatch.setattr(sss_runner, "SssApiClient",
                        _blocked_fake_client(posts, []))

    def broken_fsync(path):
        raise OSError("fsync unsupported")

    monkeypatch.setattr(sss_uncertain, "_fsync_parent_dir", broken_fsync)
    cfg = _blocked_config(tmp_path, journal)
    result = sss.run_sss_job(cfg, _Stop(), [], password="x",
                             captcha_callback=lambda img: "1234")
    assert result["status"] == "failed"
    assert result["stopped"] is True
    assert posts == []


def test_run_sss_job_journal_write_failure_returns_failed_without_post(monkeypatch, tmp_path):
    """SN-C4：POST 前置写盘失败→零 POST、明确 failed/blocked，而不是继续提交。"""
    excel = tmp_path / "闪时送.xlsx"
    _write_lunch_excel(excel)
    journal = tmp_path / "sss_uncertain.json"
    posts: list = []
    monkeypatch.setattr(sss_runner, "SssApiClient",
                        _blocked_fake_client(posts, []))

    def broken_append(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(sss_runner, "append_uncertain_records", broken_append)
    cfg = _blocked_config(tmp_path, journal)
    result = sss.run_sss_job(cfg, _Stop(), [], password="x",
                             captcha_callback=lambda img: "1234")

    assert result["status"] == "failed"
    assert result["stopped"] is True
    assert posts == []
    assert "本地不确定记录写入失败" in result["next_action"]
    assert result["summary"]["failed"] >= 1


def test_run_sss_job_blocks_across_runs_when_journal_unresolved(monkeypatch, tmp_path):
    excel = tmp_path / "闪时送.xlsx"
    _write_lunch_excel(excel)
    journal = tmp_path / "sss_uncertain.json"
    fixed_date = dt.date(2026, 9, 16)
    account = "18758187837"
    key = sss_uncertain.batch_key(fixed_date, "excel", account)
    meta = {"delivery_date": fixed_date.isoformat(), "source": "excel",
            "account": account, "platform": "https://example.invalid",
            "batch_started_at": time.time()}
    # 模拟第一轮 t0 超时、第二轮 t1 又超时：第二次 append 绝不能删掉 t0。
    sss_uncertain.append_uncertain_records(
        journal, key,
        [{"identifier": "第 3 行 张三", "client_request_id": "cr-t0",
          "fingerprint": _fingerprint().as_dict(), "error": "ReadTimeout"}],
        meta=meta)
    sss_uncertain.append_uncertain_records(
        journal, key,
        [{"identifier": "第 4 行 张三", "client_request_id": "cr-t1",
          "fingerprint": _fingerprint(door="B202").as_dict(), "error": "ReadTimeout"}],
        meta=meta)
    monkeypatch.setattr(sss_runner, "expected_delivery_date", lambda now: fixed_date)

    posts: list = []
    monkeypatch.setattr(sss_runner, "SssApiClient",
                        _blocked_fake_client(posts, []))
    cfg = _blocked_config(tmp_path, journal, account)
    logs: list[str] = []
    result = sss.run_sss_job(cfg, _Stop(), logs.append, password="x",
                             captcha_callback=lambda img: "1234")

    assert result["status"] == "blocked_uncertain"
    assert result["uncertain_records"] == 2
    assert result["stopped"] is True
    assert result["next_action"]
    assert posts == []
    assert any("拒绝任何 POST" in line for line in logs)
    # 记录没有被后续运行绕过；跨运行仍然是 unresolved。
    records = sss_uncertain.pending_records(
        sss_uncertain.load_journal(journal)["records"], key)
    assert {record["journal_id"] for record in records} == {"cr-t0", "cr-t1"}


def test_account_normalisation_is_conservative():
    """SN-C2：只合并空白/全角等明确等价差异，不丢前导零、不 lowercase。"""
    assert sss_uncertain.normalise_account("187 5818 7837") == "18758187837"
    assert sss_uncertain.normalise_account("１８７ ５８１８ ７８３７") == "18758187837"
    assert sss_uncertain.normalise_account("018758187837") == "018758187837"
    assert sss_uncertain.normalise_account("187-5818-7837") == "187-5818-7837"
    assert sss_uncertain.normalise_account("18758187838") != "18758187837"
    assert sss_uncertain.normalise_account(18758187837) == "18758187837"
    assert sss_uncertain.normalise_account(None) == ""


def test_batch_key_uses_normalised_account_only():
    spaced = sss_uncertain.batch_key("2026-09-16", "excel", "187 5818 7837")
    compact = sss_uncertain.batch_key("2026-09-16", "wps", "18758187837")
    assert spaced == compact
    assert sss_uncertain.batch_key("2026-09-16", "excel", "18758187838") != compact


def test_journal_matches_account_variants_but_not_different_account(tmp_path):
    path = tmp_path / "sss_uncertain.json"
    key = sss_uncertain.batch_key("2026-09-16", "excel", "18758187837")
    meta = {"delivery_date": "2026-09-16", "source": "excel",
            "account": "187 5818 7837", "batch_started_at": time.time()}
    assert sss_uncertain.append_uncertain_records(
        path, key,
        [{"identifier": "t0", "client_request_id": "cr-account",
          "fingerprint": {}, "error": "ReadTimeout"}],
        meta=meta) == 1
    pending = sss_uncertain.pending_records(
        sss_uncertain.load_journal(path)["records"], key)
    assert [record["journal_id"] for record in pending] == ["cr-account"]
    # journal 写入时账号已被规范化，不会把不同账号写脏。
    assert pending[0]["account"] == "18758187837"
    assert sss_uncertain.pending_records(
        sss_uncertain.load_journal(path)["records"],
        sss_uncertain.batch_key("2026-09-16", "excel", "18758187838")) == []


def test_legacy_account_variant_journal_is_migratable(tmp_path):
    """SN-C2：旧 journal 里带空格的账号写法仍能被新键匹配并清理。"""
    path = tmp_path / "sss_uncertain.json"
    old_key = "2026-09-16|excel|187 5818 7837"
    path.write_text(json.dumps({
        "version": 1,
        "records": [{
            "journal_id": "cr-old-account", "identifier": "第 3 行 张三",
            "batch_key": old_key, "delivery_date": "2026-09-16",
            "source": "excel", "account": "187 5818 7837",
            "status": "unresolved", "fingerprint": {}, "error": "ReadTimeout",
        }],
    }, ensure_ascii=False), encoding="utf-8")
    new_key = sss_uncertain.batch_key("2026-09-16", "wps", "18758187837")
    pending = sss_uncertain.pending_records(
        sss_uncertain.load_journal(path)["records"], new_key)
    assert [record["journal_id"] for record in pending] == ["cr-old-account"]
    assert sss_uncertain.resolve_uncertain_records(path, new_key, {"cr-old-account"}) == 1
    assert sss_uncertain.pending_records(
        sss_uncertain.load_journal(path)["records"], new_key) == []


def test_check_success_ambiguous_2xx_is_uncertain():
    for response in ({}, [], {"code": 0}, {"message": "x"}, {"success": False},
                     {"success": "false"}, {"success": 2}, {"success": None}):
        with pytest.raises(sss._SubmissionUncertain):
            sss._check_success(response)
    # 1 在平台上等价于明确成功，可以放行；其余未知 truthy 格式保持不确定。
    sss._check_success({"success": 1})
    # 有 message 的 success=false 才是明确拒绝，不能当 uncertain。
    try:
        sss._check_success({"success": False, "message": "boom"})
    except sss._SubmissionUncertain:
        raise AssertionError("显式 success=false + message 不应视为不确定")
    except LookupError:
        pass
    else:
        raise AssertionError("显式拒绝应抛 LookupError")


def test_ambiguous_2xx_submit_is_uncertain_not_failure():
    tasks = [_task(f"t{i}") for i in range(3)]

    def submit(payload):
        if payload["tag"] == "t1":
            return {}
        return {"success": True}

    result = sss._submit_tasks_concurrent(tasks, submit, _Stop(), None, max_workers=1)
    assert result.failures == []
    assert [identifier for identifier, _ in result.uncertain] == ["t1"]
    assert result.succeeded == {"t0", "t2"}


def test_post_one_wraps_malformed_json_as_uncertain():
    class Client:
        def post_json(self, path, body):
            raise ValueError("Expecting value: line 1 column 1 (char 0)")

    with pytest.raises(sss._SubmissionUncertain, match="Expecting value"):
        sss._post_one(Client(), {})


def test_ambiguous_2xx_keeps_journal_active_for_next_run(tmp_path):
    """SN-C3：平台可能已落单但返回 ambiguous 2xx；journal 必须 active 阻断，绝不 discard。"""
    journal = tmp_path / "sss_uncertain.json"
    key = sss_uncertain.batch_key("2026-09-16", "excel", "acct")
    meta = {"delivery_date": "2026-09-16", "source": "excel",
            "account": "acct", "batch_started_at": time.time()}
    calls: list[str] = []

    def submit(payload):
        calls.append(payload["tag"])
        return {}  # 2xx 但无法确认

    def sink(entries, journal_meta):
        sss_uncertain.append_uncertain_records(journal, key, entries, meta=journal_meta)

    def discard(identifiers):
        sss_uncertain.discard_uncertain_records(journal, key, identifiers)

    sss._run_reconciled_submission(
        [_task("t0")], lambda: (submit, lambda: None), _empty_fetch, _Stop(),
        None, None, max_workers=1, uncertain_sink=sink,
        uncertain_discard=discard, journal_meta=meta)

    assert calls == ["t0"]
    pending = sss_uncertain.pending_records(
        sss_uncertain.load_journal(journal)["records"], key)
    assert [record["journal_id"] for record in pending] == ["cr-t0"]
    assert all(record["status"] != "discarded" for record in pending)
    # 下一次运行只能只读对账；站内未出现时不得清掉记录去重发。
    remaining, _reconciliation, resolved = sss_uncertain.resolve_pending_records(
        journal, key, _empty_fetch, attempts=1)
    assert resolved == 0
    assert [record["journal_id"] for record in remaining] == ["cr-t0"]


def test_ambiguous_2xx_with_platform_landed_is_reconciled_without_repost(tmp_path):
    """平台已落单但返回 {}：收尾对账确认后清理记录，下一轮不会重复 POST。"""
    journal = tmp_path / "sss_uncertain.json"
    key = sss_uncertain.batch_key("2026-09-16", "excel", "acct")
    meta = {"delivery_date": "2026-09-16", "source": "excel",
            "account": "acct", "batch_started_at": time.time()}
    task = _task("t0")
    calls: list[str] = []
    on_site: list[dict] = []

    def submit(payload):
        calls.append(payload["tag"])
        on_site.append({
            "id": "s1", "receiveName": "张三", "receivePhone": "13800000001",
            "expectedDeliveryTime": "2026-09-16 11:00:00",
            "receiveAddress": {"doorNum": "A101", "addressDetail": ""},
            "created_at": int(time.time() * 1000),
        })
        return {}

    def fetch(path):
        return {"success": True, "result": {"records": list(on_site),
                                            "total": len(on_site)}}

    def sink(entries, journal_meta):
        sss_uncertain.append_uncertain_records(journal, key, entries, meta=journal_meta)

    def clear(identifiers):
        sss_uncertain.resolve_uncertain_records(journal, key, identifiers)

    outcome: dict = {}
    final, reconciled = sss._run_reconciled_submission(
        [task], lambda: (submit, lambda: None), fetch, _Stop(), None, None,
        max_workers=1, uncertain_sink=sink, uncertain_clear=clear,
        uncertain_discard=lambda identifiers: None, journal_meta=meta, outcome=outcome)

    assert calls == ["t0"]
    assert reconciled is True
    assert final is not None and final.confirmed == {"t0"}
    # 对账确认后 uncertain 清理完成；journal 不残留 active 记录，下一轮没有可重发对象。
    assert outcome["uncertain"] == []
    assert sss_uncertain.pending_records(
        sss_uncertain.load_journal(journal)["records"], key) == []


def test_run_sss_job_account_variant_blocks_across_runs(monkeypatch, tmp_path):
    """SN-C2：历史 journal 账号带空格，本次 compact，仍跨运行阻断。"""
    excel = tmp_path / "闪时送.xlsx"
    _write_lunch_excel(excel)
    journal = tmp_path / "sss_uncertain.json"
    fixed_date = dt.date(2026, 9, 16)
    compact = "18758187837"
    key = sss_uncertain.batch_key(fixed_date, "excel", compact)
    journal.write_text(json.dumps({
        "version": 1,
        "records": [{
            "journal_id": "cr-account-variant", "identifier": "第 3 行 张三",
            "batch_key": f"{fixed_date.isoformat()}|excel|187 5818 7837",
            "delivery_date": fixed_date.isoformat(), "source": "excel",
            "account": "187 5818 7837", "platform": "https://example.invalid",
            "status": "unresolved",
            "fingerprint": _fingerprint().as_dict(), "error": "ReadTimeout",
        }],
    }, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(sss_runner, "expected_delivery_date", lambda now: fixed_date)
    posts: list = []
    monkeypatch.setattr(sss_runner, "SssApiClient", _blocked_fake_client(posts, []))
    cfg = _blocked_config(tmp_path, journal, compact)
    first = sss.run_sss_job(cfg, _Stop(), [], password="x",
                            captcha_callback=lambda img: "1234")
    second = sss.run_sss_job(cfg, _Stop(), [], password="x",
                             captcha_callback=lambda img: "1234")
    assert first["status"] == "blocked_uncertain"
    assert second["status"] == "blocked_uncertain"
    assert posts == []
    assert sss_uncertain.pending_records(
        sss_uncertain.load_journal(journal)["records"], key)


def test_batch_key_ignores_source_but_keeps_date_and_account():
    """SN-C8：source 是可变配置，不能作为绕过阻断的条件。"""
    assert (sss_uncertain.batch_key("2026-09-16", "excel", "acct")
            == sss_uncertain.batch_key("2026-09-16", "wps", "acct"))
    assert (sss_uncertain.batch_key("2026-09-16", "excel", "acct")
            != sss_uncertain.batch_key("2026-09-17", "excel", "acct"))
    assert (sss_uncertain.batch_key("2026-09-16", "excel", "acct")
            != sss_uncertain.batch_key("2026-09-16", "excel", "other"))
    # 旧三段键仍可构造，用于读取旧 journal 数据。
    assert (sss_uncertain.legacy_batch_key("2026-09-16", "excel", "acct")
            == "2026-09-16|excel|acct")


def test_legacy_journal_record_matches_after_source_change(tmp_path):
    """SN-C8：旧 journal 三段键 + source 字段仍按日期/账号匹配新二段键。"""
    path = tmp_path / "sss_uncertain.json"
    old_key = sss_uncertain.legacy_batch_key("2026-09-16", "excel", "acct")
    record = {
        "journal_id": "cr-old", "identifier": "第 3 行 张三",
        "batch_key": old_key, "delivery_date": "2026-09-16",
        "source": "excel", "account": "acct", "status": "unresolved",
        "fingerprint": _fingerprint().as_dict(), "error": "ReadTimeout",
    }
    path.write_text(json.dumps({"version": 1, "records": [record]}), encoding="utf-8")
    new_key = sss_uncertain.batch_key("2026-09-16", "wps", "acct")
    pending = sss_uncertain.pending_records(
        sss_uncertain.load_journal(path)["records"], new_key)
    assert [item["journal_id"] for item in pending] == ["cr-old"]
    # 只读对账确认后，旧格式记录也能被显式清理。
    assert sss_uncertain.resolve_uncertain_records(path, new_key, {"cr-old"}) == 1
    assert sss_uncertain.pending_records(
        sss_uncertain.load_journal(path)["records"], new_key) == []


def test_run_sss_job_cross_source_blocks_and_repeats(monkeypatch, tmp_path):
    """SN-C8：上次 journal 来自 excel，本次 source=wps，同日期/账号仍必须阻断。"""
    from app.ordering.import_models import DayOrders

    fixed_date = dt.date(2026, 9, 16)
    account = "18758187837"
    journal = tmp_path / "sss_uncertain.json"
    old_key = sss_uncertain.legacy_batch_key(fixed_date, "excel", account)
    journal.write_text(json.dumps({
        "version": 1,
        "records": [{
            "journal_id": "cr-cross-source", "identifier": "第 3 行 张三",
            "batch_key": old_key, "delivery_date": fixed_date.isoformat(),
            "source": "excel", "account": account,
            "platform": "https://example.invalid", "status": "unresolved",
            "fingerprint": _fingerprint(account=account).as_dict(),
            "error": "ReadTimeout",
        }],
    }, ensure_ascii=False), encoding="utf-8")
    day = DayOrders(target_date=fixed_date, orders_by_sheet={
        "午餐": [{"row": 3, "name": "张三", "door": "A101",
                  "phone": "13800000001"}]})
    monkeypatch.setattr(sss_runner, "prepare_day_orders",
                        lambda config, **kwargs: day)
    monkeypatch.setattr(sss_runner, "expected_delivery_date", lambda now: fixed_date)
    posts: list = []
    monkeypatch.setattr(sss_runner, "SssApiClient", _blocked_fake_client(posts, []))
    cfg = _blocked_config(tmp_path, journal, account)
    cfg.sss_order_source = "wps"

    first = sss.run_sss_job(cfg, _Stop(), [], password="x",
                            captcha_callback=lambda img: "1234")
    second = sss.run_sss_job(cfg, _Stop(), [], password="x",
                             captcha_callback=lambda img: "1234")

    assert first["status"] == "blocked_uncertain"
    assert first["uncertain_records"] == 1
    assert second["status"] == "blocked_uncertain"
    assert posts == []
    remaining = sss_uncertain.pending_records(
        sss_uncertain.load_journal(journal)["records"])
    assert [item["journal_id"] for item in remaining] == ["cr-cross-source"]


def test_run_sss_job_clears_journal_when_readonly_reconcile_confirms(monkeypatch, tmp_path):
    """跨运行记录被只读对账确认后允许继续，且不会重复 POST 这条已确认订单。"""
    excel = tmp_path / "闪时送.xlsx"
    _write_lunch_excel(excel)
    post_records: list = []
    journal = tmp_path / "sss_uncertain.json"
    fixed_date = dt.date(2026, 9, 16)
    account = "18758187837"
    key = sss_uncertain.batch_key(fixed_date, "excel", account)
    task = _task("第 3 行 张三", door="A101")
    task["account"] = account
    task["fingerprint"] = _fingerprint(account=account)

    def fixed_collect(orders_by_sheet, store_id, address, goods_name, **kwargs):
        return [task]

    monkeypatch.setattr(sss_runner, "_collect_tasks", fixed_collect)
    station_record = {
        "id": "1001", "receiveName": "张三", "receivePhone": "13800000001",
        "expectedDeliveryTime": "2026-09-16 11:00:00", "orderType": 2,
        "storeId": 211053, "goodsDetail": [{"goodsName": "轻食", "goodsNum": 1}],
        "receiveAddress": {"lnt": 1.0, "lat": 2.0, "areaCode": "330110",
                           "addressDetail": "X", "doorNum": "A101"},
        "created_at": "2026-09-16 10:00:00",
    }
    sss_uncertain.append_uncertain_records(
        journal, key,
        [{"identifier": task["identifier"], "client_request_id": "cr-ok",
          "fingerprint": task["fingerprint"].as_dict(), "error": "ReadTimeout"}],
        meta={"delivery_date": fixed_date.isoformat(), "source": "excel",
              "account": account, "platform": "https://example.invalid",
              # 批次开始时间必须早于站内订单创建时间，否则被 created_after 正确排除。
              # 站内订单的裸时间字符串按 UTC+8 解释（见 reconcile._record_created_timestamp），
              # 批次开始时间必须用同一时区构造，否则在非 UTC+8 机器（如 CI 的 UTC）上会被
              # created_after 误排除，测试将随本机时区变化而失败。
              "batch_started_at": dt.datetime(
                  2026, 9, 16, 9, 59, 0,
                  tzinfo=dt.timezone(dt.timedelta(hours=8))).timestamp()})
    monkeypatch.setattr(sss_runner, "expected_delivery_date", lambda now: fixed_date)
    monkeypatch.setattr(sss_runner, "SssApiClient",
                        _blocked_fake_client(post_records, [station_record]))
    cfg = _blocked_config(tmp_path, journal, account)
    logs: list[str] = []
    result = sss.run_sss_job(cfg, _Stop(), logs.append, password="x",
                             captcha_callback=lambda img: "1234")

    # 站内记录同时命中了本批 Excel 任务与历史不确定记录：本批应识别为无需新 POST。
    assert result["status"] == "confirmed"
    assert post_records == []
    assert result["created"] == 1
    assert sss_uncertain.pending_records(
        sss_uncertain.load_journal(journal)["records"], key) == []
