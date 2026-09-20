"""独立反证回归（A/B/C/D 最终验收未闭环项的对抗性测试）。

只新增本文件及同目录合成子进程脚本；不改各实现者测试。
全部离线：临时目录、合成 Excel、伪客户端/伪平台，不联网、不真实下单、
不写真实 WPS、不读真实客户或系统凭据。
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.api import bridge as bridge_module
from app.api.bridge import Bridge
from app.web.auth import AuthStore
from app.web.server import SESSION_COOKIE, create_server
from app.ordering import submission as sss_submission
from app.ordering import uncertain as sss_uncertain
from app.wps.executor import _refresh_journal_after_lock
from app.wps.journal import SyncJournal
from app.wps.ledger import SyncLedger
from app.wps.models import SheetPlan
from app.wps.recovery import recovery_status

REPO_ROOT = Path(__file__).resolve().parents[1]
CHILD = REPO_ROOT / "tests" / "independent_sss_batch_child.py"


def _spawn(work: Path, journal: Path, mode: str = "normal",
           account: str = "18758187837", *,
           shared_dir: Path | None = None,
           set_authority: bool = True,
           lock_timeout: str = "3",
           env_extra: dict[str, str] | None = None) -> subprocess.Popen:
    # 强制两个子进程共享同一权威 journal / 数据根，避免测试写到真实用户配置。
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
        [sys.executable, str(CHILD), str(work), str(journal), mode, account],
        cwd=str(REPO_ROOT),
        env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )


def _wait_signal(work: Path, timeout: float = 20.0, *,
                 shared_dir: Path | None = None) -> None:
    base = shared_dir if shared_dir is not None else work
    deadline = time.time() + timeout
    while time.time() < deadline:
        if (base / "signal").exists():
            return
        time.sleep(0.02)
    raise AssertionError(f"等待合成平台 POST signal 超时：{work}")


def _platform_count(work: Path, *, shared_dir: Path | None = None) -> int:
    base = shared_dir if shared_dir is not None else work
    state_path = base / "platform.json"
    if not state_path.exists():
        return 0
    return int(json.loads(state_path.read_text(encoding="utf-8"))["count"])


def test_same_journal_two_processes_only_one_post(tmp_path):
    """真实子进程 + 合成平台：同一 journal 路径时只能产生 1 次 POST。"""
    work = tmp_path / "same"
    work.mkdir()
    (work / "hidden").write_text("1", encoding="utf-8")
    journal = work / "sss_uncertain.json"

    first = _spawn(work, journal)
    _wait_signal(work)
    second = _spawn(work, journal)
    first_out, first_err = first.communicate(timeout=40)
    second_out, second_err = second.communicate(timeout=40)

    assert first.returncode == 0, first_err or first_out
    assert second.returncode == 0, second_err or second_out
    assert _platform_count(work) == 1


def test_crash_after_post_same_journal_does_not_repost(tmp_path):
    """首进程 POST 后崩溃：次进程必须读 journal/对账，不能盲目补发。"""
    work = tmp_path / "crash"
    work.mkdir()
    (work / "hidden").write_text("1", encoding="utf-8")
    journal = work / "sss_uncertain.json"

    crash = _spawn(work, journal, "crash")
    out, err = crash.communicate(timeout=40)
    assert crash.returncode == 17, err or out
    assert _platform_count(work) == 1


def test_account_variant_two_processes_same_journal_blocked(tmp_path):
    """等价账号写法（空格/NFKC）跨真实子进程必须命中同一 unresolved 阻断。"""
    work = tmp_path / "account-variant"
    work.mkdir()
    (work / "hidden").write_text("1", encoding="utf-8")
    journal = work / "sss_uncertain.json"

    first = _spawn(work, journal, account="１８７ ５８１８ ７８３７")
    _wait_signal(work)
    second = _spawn(work, journal, account="18758187837")
    first_out, first_err = first.communicate(timeout=40)
    second_out, second_err = second.communicate(timeout=40)

    assert first.returncode == 0, first_err or first_out
    assert second.returncode == 0, second_err or second_out
    assert _platform_count(work) == 1

    recover = _spawn(work, journal)
    out2, err2 = recover.communicate(timeout=40)
    assert recover.returncode == 0, err2 or out2
    assert _platform_count(work) == 1


def test_different_journal_paths_two_processes_only_one_post(tmp_path):
    """同一业务批次，即使实例配置了不同 sss_uncertain_path，也只能 POST=1。"""
    work = tmp_path / "different-journal"
    work.mkdir()
    (work / "hidden").write_text("1", encoding="utf-8")
    data_dir = work / "deployment-data"
    data_dir.mkdir()
    authority = work / "authoritative-uncertain.json"

    first = _spawn(work, work / "journal-a.json")
    _wait_signal(work)
    second = _spawn(work, work / "journal-b.json")
    first_out, first_err = first.communicate(timeout=40)
    second_out, second_err = second.communicate(timeout=40)

    assert first.returncode == 0, first_err or first_out
    assert second.returncode == 0, second_err or second_out
    assert _platform_count(work) == 1
    # 第二个进程必须在权威共享 journal 上看到并处理第一个进程的未决记录。
    assert authority.exists()


def test_crash_then_different_journal_does_not_repost(tmp_path):
    """首进程 POST 后崩溃，次进程用不同 journal 路径也不能盲目重发。"""
    work = tmp_path / "crash-different-journal"
    work.mkdir()
    (work / "hidden").write_text("1", encoding="utf-8")

    crash = _spawn(work, work / "journal-a.json", "crash")
    out, err = crash.communicate(timeout=40)
    assert crash.returncode == 17, err or out
    assert _platform_count(work) == 1

    recover = _spawn(work, work / "journal-b.json")
    out2, err2 = recover.communicate(timeout=40)
    assert recover.returncode == 0, err2 or out2
    assert _platform_count(work) == 1


def test_legal_new_order_is_created_once_across_two_processes(tmp_path):
    """正向：列表可见时合法新订单正常创建一次，后到进程不得重复 POST。"""
    work = tmp_path / "legal-new-order"
    work.mkdir()

    first = _spawn(work, work / "journal-a.json")
    first_out, first_err = first.communicate(timeout=40)
    assert first.returncode == 0, first_err or first_out
    assert _platform_count(work) == 1
    result = json.loads(first_out.strip().splitlines()[-1])
    assert result.get("created") == 1
    assert result.get("status") in ("confirmed", "partial", "success")

    second = _spawn(work, work / "journal-b.json")
    second_out, second_err = second.communicate(timeout=40)
    assert second.returncode == 0, second_err or second_out
    assert _platform_count(work) == 1


def test_legacy_journal_is_migrated_and_blocks_before_post(tmp_path):
    """旧 sss_uncertain_path 中的 unresolved 必须先合并并保守阻断。"""
    work = tmp_path / "legacy"
    work.mkdir()
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

    assert child.returncode == 0, err or out
    assert _platform_count(work) == 0, "旧 unresolved 未阻断，POST 被发出了"
    authority = work / "authoritative-uncertain.json"
    assert authority.exists()
    pending = sss_uncertain.pending_records(
        sss_uncertain.load_journal(authority)["records"])
    assert len(pending) == 1


def test_batch_lock_failure_blocks_before_post(tmp_path):
    """共享批次锁被占用/获取失败时必须零 POST，不能并行提交。"""
    work = tmp_path / "lock-failure"
    work.mkdir()
    (work / "hidden").write_text("1", encoding="utf-8")
    key = sss_uncertain.batch_key("2026-09-16", "excel", "18758187837")
    blocker = sss_uncertain.batch_submission_lock(work / "unused", key, timeout=5.0)
    blocker.acquire()
    try:
        child = _spawn(work, work / "journal-a.json", lock_timeout="0.2")
        out, err = child.communicate(timeout=40)
    finally:
        blocker.release()

    assert child.returncode == 0, err or out
    assert _platform_count(work) == 0, "锁失败时仍发出了 POST"
    assert "blocked_concurrent" in out, out


def test_ambiguous_2xx_is_uncertain_not_explicit_failure():
    """{} / [] / None / 缺 success / 畸形 JSON 必须归 uncertain，绝不能 discard。"""
    for payload in ({}, [], None, {"code": 0}, {"success": None},
                    {"success": "true"}, {"data": {}}):
        with pytest.raises(sss_submission._SubmissionUncertain):
            sss_submission._check_success(payload)

    # success=false 但没有 message/code：证据不足，也必须 uncertain。
    with pytest.raises(sss_submission._SubmissionUncertain):
        sss_submission._check_success({"success": False})

    # 明确的 success=false + message/code 才允许走显式拒绝（discard 路径）。
    with pytest.raises(sss_submission._ExplicitRejection):
        sss_submission._check_success({"success": False, "message": "参数错误"})


def test_malformed_transport_is_wrapped_as_uncertain():
    class _Client:
        def post_json(self, _path, _payload):
            raise sss_submission.SssTransportError("HTML instead of JSON")

    with pytest.raises(sss_submission._SubmissionUncertain):
        sss_submission._post_one(_Client(), {"a": 1})


def test_account_variants_and_legacy_journal_match_but_other_account_does_not():
    assert sss_uncertain.normalise_account("187 5818 7837") == "18758187837"
    assert sss_uncertain.normalise_account("１８７ ５８１８ ７８３７") == "18758187837"
    key = sss_uncertain.batch_key("2026-09-20", "excel", "187 5818 7837")

    legacy = {
        "batch_key": "2026-09-20|wps|18758187837",
        "delivery_date": "2026-09-20",
        "account": "18758187837",
        "status": "unresolved",
    }
    assert sss_uncertain._record_matches_batch(legacy, key) is True

    other = dict(legacy, account="18758187838",
                 batch_key="2026-09-20|wps|18758187838")
    assert sss_uncertain._record_matches_batch(other, key) is False


class _DeleteBackend:
    def __init__(self, *, deleted=False, remaining=None, exc=None):
        self.deleted = deleted
        self.remaining = remaining
        self.exc = exc

    def delete(self, _account):
        if self.exc is not None:
            raise self.exc
        return self.deleted

    def get(self, _account):
        return self.remaining


@pytest.mark.parametrize(
    "backend, expected_state",
    [
        (_DeleteBackend(deleted=False, remaining="secret"), "delete_failed"),
        (_DeleteBackend(deleted=False, remaining=None),
         "already_absent_or_unavailable"),
        (_DeleteBackend(exc=RuntimeError("keyring down")), "delete_error"),
    ],
)
def test_clear_password_false_or_raise_never_reports_ok(
        tmp_path, monkeypatch, backend, expected_state):
    bridge = Bridge(config_path=str(tmp_path / "config.json"), is_admin=True)
    bridge._config.phone_number = "13800000000"
    bridge._config.sss_account = "sss-user"
    monkeypatch.setattr(bridge_module, "delete_password", backend.delete)
    monkeypatch.setattr(bridge_module, "delete_sss_password", backend.delete)
    monkeypatch.setattr(bridge_module, "get_password", backend.get)
    monkeypatch.setattr(bridge_module, "get_sss_password", backend.get)

    got = bridge.clear_password("order")
    assert got["ok"] is False, got
    assert got["state"] == expected_state, got
    assert "secret" not in json.dumps(got, ensure_ascii=False)


def _apply_result_via_bridge(tmp_path, executor_result: dict) -> dict:
    bridge = Bridge(config_path=str(tmp_path / "config.json"), is_admin=True)
    bridge._wps_cli = lambda: SimpleNamespace(path="/fake/kdocs-cli")
    reservation = bridge._operations.try_reserve("wps_upload")
    assert reservation.granted
    plan = SheetPlan(sheet="东湖中餐", file_id="F1",
                     target_date=_dt.date(2026, 9, 20), target_col=1)
    record = SimpleNamespace(preview_id="pv-independent")
    bundle = {"target": _dt.date(2026, 9, 20)}
    monkey_bridge = bridge_module
    old_summary, old_format, old_apply = (
        monkey_bridge.summarize_plan, monkey_bridge.format_plan,
        monkey_bridge.apply_plan)
    monkey_bridge.summarize_plan = lambda _plans: {}
    monkey_bridge.format_plan = lambda _plans: ""
    monkey_bridge.apply_plan = lambda _cli, _plans, **_kw: executor_result
    try:
        return bridge._wps_apply_plan([plan], record, reservation.operation, bundle)
    finally:
        monkey_bridge.summarize_plan = old_summary
        monkey_bridge.format_plan = old_format
        monkey_bridge.apply_plan = old_apply


@pytest.mark.parametrize(
    "executor_result, expected_status",
    [
        ({"status": "future_new_state", "written": 1, "failed": 0,
          "sheets": [{"sheet": "东湖中餐", "status": "ok"}]}, "uncertain"),
        ({"status": "future_new_state", "written": 1, "failed": 0,
          "sheets": []}, "uncertain"),
        ({"status": "ok", "written": 1, "failed": 0, "sheets": []}, "uncertain"),
        ({"status": "ok", "written": 2, "failed": 0,
          "sheets": [{"sheet": "东湖中餐", "status": "ok"}]}, "uncertain"),
        ({"status": "ok", "written": 1, "failed": 0,
          "sheets": [{"sheet": "东湖中餐", "status": "ok"}]}, "success"),
        ({"status": "verified", "written": 1, "failed": 0,
          "sheets": [{"sheet": "东湖中餐", "status": "verified"}]}, "success"),
    ],
)
def test_wps_unknown_or_contradictory_executor_states_never_success(
        tmp_path, executor_result, expected_status):
    got = _apply_result_via_bridge(tmp_path, executor_result)
    assert got["status"] == expected_status, got
    if expected_status == "success":
        assert got["ok"] is True
    else:
        assert got["ok"] is False
        assert got.get("uncertain") is True


def test_stale_journal_object_reads_canonical_pending_after_refresh(tmp_path):
    ledger_path = tmp_path / "state.json"
    ledger = SyncLedger(ledger_path)
    stale = SyncJournal(ledger.journal_path)  # 锁外构造的旧空对象

    canonical = SyncJournal(ledger.journal_path)
    canonical.create_operation("op-child", {
        "0:东湖中餐:F1": {"sheet": "东湖中餐", "file_id": "F1",
                          "status": "uncertain", "reason": "child pending",
                          "next_action": "manual_reconcile"},
    })
    canonical.save()

    refreshed = _refresh_journal_after_lock(stale, ledger)
    assert refreshed is not None
    pending = refreshed.pending_operations()
    assert "op-child" in pending
    assert pending["op-child"]["status"] == "uncertain"


def test_recovery_status_is_read_only_and_hides_paths(tmp_path):
    ledger_path = tmp_path / "state.json"
    journal = SyncJournal(tmp_path / "state.json.journal")
    journal.create_operation("op-ro", {
        "k": {"sheet": "东湖中餐", "file_id": "SENSITIVE_FILE_ID",
              "target_date": "2026-09-20", "status": "uncertain",
              "problems": ["总餐次期望 9 实际 3"],
              "reason": "回读不一致", "next_action": "manual_reconcile"},
    }, target_date="2026-09-20")
    journal.save()
    ledger = SyncLedger(ledger_path)
    journal_before = journal.path.read_bytes()
    ledger_before = ledger_path.read_bytes() if ledger_path.exists() else b""

    status = recovery_status(ledger, journal=journal)

    assert journal.path.read_bytes() == journal_before
    if ledger_path.exists():
        assert ledger_path.read_bytes() == ledger_before
    text = json.dumps(status, ensure_ascii=False)
    assert "SENSITIVE_FILE_ID" not in text
    assert str(tmp_path) not in text
    assert status["read_only"] is True
    assert status["queried_cloud"] is False


def _seed_recovery_journal(data_dir: Path) -> Path:
    from app.wps.journal import SyncJournal
    from app.wps.ledger import SyncLedger

    data_dir.mkdir(parents=True, exist_ok=True)
    ledger_path = data_dir / "wps_sync_state.json"
    SyncLedger(ledger_path).save()
    journal_path = data_dir / "wps_sync_state.json.journal"
    journal = SyncJournal(journal_path)
    journal.create_operation("op-sensitive", {
        "k": {
            "sheet": "东湖中餐",
            "file_id": "SECRET_FILE_ID",
            "target_date": "2026-09-20",
            "status": "uncertain",
            "problems": [
                "张三: 总餐次应为 9 实际 3",
                "电话 13800000001 地址 学3-101",
            ],
            "risk_reason": "客户 张三 13800000001 餐次异常",
            "reason": "回读不一致",
            "next_action": "manual_reconcile",
        },
    }, target_date="2026-09-20")
    journal.save()
    return journal_path


def test_non_admin_recovery_query_is_safe_summary(tmp_path, monkeypatch):
    """普通用户可查询，但只得到安全摘要，不含客户/餐次/文件/路径原文。"""
    data_dir = tmp_path / "data"
    journal_path = _seed_recovery_journal(data_dir)
    monkeypatch.setenv("YIKOU_DATA_DIR", str(data_dir))
    bridge = Bridge(config_path=str(tmp_path / "config.json"), is_admin=False)
    bridge.set_request_identity("bob")
    before = journal_path.read_bytes()

    got = bridge.wps_recovery_status()
    after = journal_path.read_bytes()

    assert got["ok"] is True, got
    assert got["scope"] == "summary"
    assert got["read_only"] is True and got["queried_cloud"] is False
    assert before == after
    assert "operations" not in got and "pending_operations" not in got
    text = json.dumps(got, ensure_ascii=False)
    for marker in ("张三", "13800000001", "总餐次应为", "SECRET_FILE_ID",
                   "东湖中餐", str(data_dir), "学3-101"):
        assert marker not in text, f"普通用户响应泄露了 {marker!r}: {text}"


def test_admin_recovery_query_is_authorized_and_sanitized(tmp_path, monkeypatch):
    """正向：管理员授权查询必须可用，但白名单 DTO 仍不得含客户/文件原文。"""
    data_dir = tmp_path / "data-admin"
    journal_path = _seed_recovery_journal(data_dir)
    monkeypatch.setenv("YIKOU_DATA_DIR", str(data_dir))
    bridge = Bridge(config_path=str(tmp_path / "config.json"), is_admin=True)
    bridge.set_request_identity("admin")
    before = journal_path.read_bytes()

    got = bridge.wps_recovery_status()

    assert got["ok"] is True, got
    assert got["scope"] == "admin"
    assert isinstance(got.get("operations"), list) and got["operations"], got
    assert got["read_only"] is True and got["queried_cloud"] is False
    assert journal_path.read_bytes() == before
    text = json.dumps(got, ensure_ascii=False)
    for marker in ("张三", "13800000001", "总餐次应为", "SECRET_FILE_ID",
                   "学3-101", str(data_dir)):
        assert marker not in text, f"管理员响应异常泄露了 {marker!r}: {text}"


def test_recovery_query_corrupt_journal_error_is_sanitized(tmp_path, monkeypatch):
    """异常响应也不能回显损坏文件中可能夹带的客户原文。"""
    data_dir = tmp_path / "data-corrupt"
    data_dir.mkdir(parents=True)
    (data_dir / "wps_sync_state.json").write_text(
        json.dumps({"version": 1, "batches": {}}), encoding="utf-8")
    (data_dir / "wps_sync_state.json.journal").write_text(
        "{not-json 张三 13800000001 总餐次应为9", encoding="utf-8")
    monkeypatch.setenv("YIKOU_DATA_DIR", str(data_dir))
    bridge = Bridge(config_path=str(tmp_path / "config.json"), is_admin=False)
    bridge.set_request_identity("bob")

    got = bridge.wps_recovery_status()

    assert got["ok"] is False, got
    text = json.dumps(got, ensure_ascii=False)
    for marker in ("张三", "13800000001", "总餐次应为", "not-json"):
        assert marker not in text, f"异常响应泄露了 {marker!r}: {text}"


def _http_call(server, method: str, path: str, *, body=None,
               cookie: str | None = None) -> tuple[int, str]:
    port = server.server_address[1]
    headers: dict[str, str] = {}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if cookie:
        headers["Cookie"] = f"{SESSION_COOKIE}={cookie}"
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=data, method=method,
        headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return response.status, response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


def _http_token(server, username: str, password: str) -> str:
    port = server.server_address[1]
    data = urllib.parse.urlencode(
        {"action": "login", "username": username,
         "password": password}).encode("utf-8")
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/login", data=data, method="POST")
    request.add_header("Content-Type", "application/x-www-form-urlencoded")

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):  # noqa: ANN002
            return None

    response = None
    try:
        response = urllib.request.build_opener(_NoRedirect).open(
            request, timeout=15)
    except urllib.error.HTTPError as exc:
        response = exc
    raw = response.headers.get("Set-Cookie", "")
    assert f"{SESSION_COOKIE}=" in raw, raw
    return raw.split(f"{SESSION_COOKIE}=", 1)[1].split(";", 1)[0]


def test_recovery_http_permissions_redaction_and_read_only(tmp_path, monkeypatch):
    """真实本地 HTTP：权限、脱敏、异常返回与查询只读性。"""
    admin = "admin@example.com"
    user = "worker@example.com"
    admin_pw = "adminpw123"
    user_pw = "workerpw123"

    data_dir = tmp_path / "data"
    journal_path = _seed_recovery_journal(data_dir)
    monkeypatch.setenv("YIKOU_DATA_DIR", str(data_dir))

    auth = AuthStore(tmp_path / "auth")
    auth.create_admin(admin, admin_pw)
    auth.register(user, user_pw)
    auth.approve(user, by=admin)
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html><title>t</title>",
                                     encoding="utf-8")
    server = create_server("127.0.0.1", 0, dist_dir=dist,
                           config_path=tmp_path / "config.json", auth=auth)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    markers = ("张三", "13800000001", "总餐次应为", "FILE-CUSTOMER-ID",
               "SENSITIVE_FUTURE_FIELD", "学3-101", str(data_dir))
    dangerous = {
        "ok": True,
        "journal_path": "/tmp/private.journal",
        "operations": [{
            "operation_id": "张三-wps-secret",
            "status": "uncertain",
            "sheets": [{
                "sheet": "东湖中餐",
                "file_id": "FILE-CUSTOMER-ID",
                "target_date": "2026-09-20",
                "target_ref": "张三 13800000000 学3-101",
                "status": "uncertain",
                "problems": ["张三 总餐次应为 9 实际 3",
                             "13800000001 学3-101"],
                "risk_reason": "客户 张三 13800000001 餐次异常",
                "reason": "异常原文",
                "next_action": "manual_reconcile",
            }],
        }],
        "counts": {"uncertain": 1, "future_unknown_count": 99},
        "next_action": "manual_reconcile",
        "future_secret": "SENSITIVE_FUTURE_FIELD",
    }
    original = bridge_module._wps_recovery_status_contract
    monkeypatch.setattr(bridge_module, "_wps_recovery_status_contract",
                        lambda _ledger: dangerous)
    try:
        status, _body = _http_call(server, "POST", "/api/wps_recovery_status",
                                   body=[])
        assert status == 401

        user_cookie = _http_token(server, user, user_pw)
        admin_cookie = _http_token(server, admin, admin_pw)
        before = journal_path.read_bytes()

        user_status, user_text = _http_call(
            server, "POST", "/api/wps_recovery_status",
            body=[], cookie=user_cookie)
        assert user_status == 200, user_text
        user_body = json.loads(user_text)
        assert user_body["scope"] == "summary"
        assert user_body["read_only"] is True
        assert user_body["queried_cloud"] is False
        assert "operations" not in user_body
        assert "pending_operations" not in user_body
        assert "future_unknown_count" not in user_body["counts"]

        admin_status, admin_text = _http_call(
            server, "POST", "/api/wps_recovery_status",
            body=[], cookie=admin_cookie)
        assert admin_status == 200, admin_text
        admin_body = json.loads(admin_text)
        assert admin_body["scope"] == "admin"
        assert isinstance(admin_body.get("operations"), list) and admin_body["operations"]
        assert admin_body["read_only"] is True

        for text in (user_text, admin_text):
            for marker in markers:
                assert marker not in text, f"HTTP 响应泄露 {marker!r}: {text}"
        assert journal_path.read_bytes() == before, "恢复查询不得写 journal"

        # 异常路径：损坏 journal 原文夹带客户标记，响应必须只回安全 error_code。
        monkeypatch.setattr(bridge_module, "_wps_recovery_status_contract",
                            original)
        corrupt = "{not-json 张三 13800000001 总餐次应为9"
        journal_path.write_text(corrupt, encoding="utf-8")
        err_status, err_text = _http_call(
            server, "POST", "/api/wps_recovery_status",
            body=[], cookie=user_cookie)
        assert err_status == 200, err_text
        err_body = json.loads(err_text)
        assert err_body["ok"] is False
        for marker in ("张三", "13800000001", "总餐次应为", "not-json"):
            assert marker not in err_text, f"异常响应泄露 {marker!r}: {err_text}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_pending_interactions_owner_expiry_duplicate_and_cancel(tmp_path):
    bridge = Bridge(config_path=str(tmp_path / "config.json"), is_admin=False)
    bridge._task_owner = "alice"
    bridge._task_owner_is_admin = False
    bridge._task_operation_id = "op-owner"
    bridge.set_request_identity("alice")

    entry_id, entry = bridge._register_interaction("sss_retry", request={
        "title": "确认", "message": "m", "choices": [
            {"value": "retry", "label": "重试", "style": "primary"}]})
    assert bridge.pending_interactions()["count"] == 1

    # 跨用户：bob 不可见也不可 resolve。
    bridge.set_request_identity("bob")
    assert bridge.pending_interactions()["count"] == 0
    denied = bridge.resolve_decision(entry_id, "retry")
    assert denied["ok"] is False and denied["status"] == "forbidden"

    # 归属人：首次 resolve 成功，重复 resolve 必须 not_pending。
    bridge.set_request_identity("alice")
    first = bridge.resolve_decision(entry_id, "retry")
    assert first["ok"] is True
    second = bridge.resolve_decision(entry_id, "retry")
    assert second["ok"] is False and second["status"] == "not_pending"
    assert entry.holder[-1] == "retry"

    # 过期：pending 查询隐藏，resolve 返回 expired。
    bridge._task_owner = "alice"
    bridge._interaction_timeout_s = 0.01
    expired_id, _entry = bridge._register_interaction("captcha", request={"image": "x"})
    time.sleep(0.05)
    assert bridge.pending_interactions()["count"] == 0
    expired = bridge.resolve_captcha(expired_id, "1234")
    assert expired["ok"] is False and expired["status"] == "expired"


def test_cancel_wakes_decision_worker_with_safe_default(tmp_path):
    import threading

    bridge = Bridge(config_path=str(tmp_path / "config.json"), is_admin=True)
    bridge._interaction_timeout_s = 5.0
    result: list[str] = []

    worker = threading.Thread(
        target=lambda: result.append(
            bridge._request_decision("sss_retry", "t", "m", [])),
        daemon=True)
    worker.start()
    deadline = time.time() + 5
    while not bridge._decisions and time.time() < deadline:
        time.sleep(0.01)
    assert bridge._decisions, "决策交互应已登记"

    bridge._cancel_pending_interactions("独立测试停止竞争")
    worker.join(5)
    assert not worker.is_alive()
    assert result == ["stop"]
    assert bridge.pending_interactions()["count"] == 0


def _load_probe_module():
    import importlib.util
    probe_path = REPO_ROOT / "tests" / "independent_final_counterexample_probe.py"
    spec = importlib.util.spec_from_file_location("indep_final_probe", probe_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_probe_still_detects_dangerous_recovery_leak(monkeypatch):
    """证明修正后的探针仍会因“危险返回”失败，而不是硬编码 BLOCKED。"""
    def dangerous_full(_ledger):
        return {
            "ok": True,
            "read_only": True,
            "queried_cloud": False,
            "counts": {"uncertain": 1},
            "next_action": "manual_reconcile",
            "operations": [{
                "operation_id": "op-danger",
                "pending": True,
                "sheets": [{
                    "status": "uncertain",
                    "problems": ["张三: 总餐次应为 9，实际 3",
                                 "电话 13800000001 地址 学3-101"],
                    "risk_reason": "客户 张三 13800000001 餐次异常",
                }],
            }],
        }
    monkeypatch.setattr(bridge_module, "_wps_recovery_status_contract",
                        dangerous_full)
    monkeypatch.setattr(
        Bridge, "_sanitize_wps_recovery",
        staticmethod(lambda raw, *, viewer_admin: raw))
    probe = _load_probe_module()
    assert probe.probe_recovery_status_name_leak() is True


def test_probe_script_is_green_all_safety_invariants_blocked():
    """把探针纳入 pytest 自动回归：正常实现必须 exit 0 且全部不变量 BLOCKED。"""
    completed = subprocess.run(
        [sys.executable, str(REPO_ROOT / "tests" /
                             "independent_final_counterexample_probe.py")],
        cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=300)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "DEFECT" not in completed.stdout, completed.stdout
    assert completed.stdout.count("BLOCKED") == 21, completed.stdout


def test_probe_registry_covers_r6_4_blind_spots():
    """R6-4：M4/M6/M8 三个盲区场景必须仍在探针清单里，且别名稳定。"""
    probe = _load_probe_module()
    registry = {alias: name for alias, name, _func in probe.SCENARIOS}
    assert len(probe.SCENARIOS) == 21, registry
    assert registry["m4-json-corrupt"] == "JSON 语法损坏 journal 被当作空状态放行"
    assert registry["m6-post-timeout"] == \
        "POST 超时/断线被当作明确失败并允许盲目重发"
    assert registry["m8-batch-scope"] == "批次范围（日期/账号/平台）隔离失效"
    # 只读用法/别名帮助可用，且 --only 不会静默跑空。
    listed = subprocess.run(
        [sys.executable, str(REPO_ROOT / "tests" /
                             "independent_final_counterexample_probe.py"),
         "--list"],
        cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=60)
    assert listed.returncode == 0, listed.stdout + listed.stderr
    for alias in ("m4-json-corrupt", "m6-post-timeout", "m8-batch-scope"):
        assert alias in listed.stdout, listed.stdout


def _load_mutation_check_r6_4():
    import importlib.util
    script = REPO_ROOT / "tools" / "r7-acceptance" / "mutation_check_r6_4.py"
    assert script.is_file(), script
    spec = importlib.util.spec_from_file_location("r6_4_mutation_check", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_r6_4_mutation_cases_match_probe_scenarios_and_anchors():
    """变异用例必须与探针场景一一对应，锚点在当前工作区唯一（防止静默漂移）。"""
    mutation = _load_mutation_check_r6_4()
    probe = _load_probe_module()
    probe_aliases = {alias for alias, _name, _func in probe.SCENARIOS}
    probe_names = {name for _alias, name, _func in probe.SCENARIOS}
    assert [case["id"] for case in mutation.CASES] == [
        "m4", "m6", "m8-always-true", "m8-ignore-range", "m8-always-false"]
    for case in mutation.CASES:
        assert case["aliases"], case
        assert set(case["aliases"]) <= probe_aliases, case
        assert case["scenario"] in probe_names, case
        for edit in case["edits"]:
            text = (REPO_ROOT / edit["file"]).read_text(encoding="utf-8")
            assert text.count(edit["old"]) == 1, (case["id"], edit["file"])
            assert edit["new"] not in text, (case["id"], edit["file"])
            assert edit["old"] != edit["new"], (case["id"], edit["file"])


def test_r6_4_mutations_force_probe_defect_before_injected_restored(tmp_path):
    """固化 M4/M6/M8：正常通过 → 注入后探针 exit≠0 且报 DEFECT → 恢复后再通过。"""
    summary_path = tmp_path / "r6-4-summary.json"
    completed = subprocess.run(
        [sys.executable, str(REPO_ROOT / "tools" / "r7-acceptance" /
                             "mutation_check_r6_4.py"),
         "--json", str(summary_path), "--log-dir", str(tmp_path / "mutation-logs")],
        cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=900)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert summary["ok"] is True, summary
    cases = {case["id"]: case for case in summary["cases"]}
    assert set(cases) == {"m4", "m6", "m8-always-true", "m8-ignore-range",
                          "m8-always-false"}
    for case_id, case in cases.items():
        phases = case["phases"]
        assert phases["before"]["exit"] == 0, (case_id, phases)
        assert phases["before"]["defect_lines"] == [], (case_id, phases)
        assert phases["injected"]["exit"] != 0, (case_id, phases)
        assert case["expected_defect"] in phases["injected"]["defect_lines"], \
            (case_id, phases)
        assert phases["restored"]["exit"] == 0, (case_id, phases)
        assert phases["restored"]["defect_lines"] == [], (case_id, phases)
        assert case["anchor_check"], case_id
        assert all(count == 1 for count in case["anchor_check"].values()), \
            (case_id, case["anchor_check"])
        # 变异位置必须精确记录（文件:行），且注入前后源码摘要必须不同。
        assert case["mutation_sites"], case_id
        for site in case["mutation_sites"]:
            assert site["anchor_line"] > 0, site
            assert site["original_sha256"] != site["mutated_sha256"], site
        # 探针必须给出“行为学差异”证据，而不是只报一句场景标签。
        assert case["expect_details"], case_id
        assert set(case["phases"]["injected"]["details_found"]) == \
            set(case["expect_details"]), (case_id, case["phases"]["injected"])
        assert case["workspace_unchanged"] is True, case_id
    # 每个变异必须使用独立临时目录（互不共享副本/TMPDIR/锁根）。
    temp_dirs = {case["temp_dir"] for case in summary["cases"]}
    assert len(temp_dirs) == len(summary["cases"]), temp_dirs


def test_mutation_raw_origin_makes_probe_report_origin_defect():
    """故障注入：取消 origin 规范化，探针必须 exit 1 并报告等价 origin 缺陷。"""
    env = {**os.environ, "INDEP_MUTATE_ORIGIN_RAW": "1"}
    completed = subprocess.run(
        [sys.executable, str(REPO_ROOT / "tests" /
                             "independent_final_counterexample_probe.py")],
        cwd=str(REPO_ROOT), env=env, capture_output=True, text=True, timeout=180)
    assert completed.returncode == 1, completed.stdout + completed.stderr
    assert "等价 origin 变体未共享权威状态" in completed.stdout
    assert "DEFECT" in completed.stdout


def test_mutation_mirror_all_makes_probe_report_cross_account_defect():
    """故障注入：恢复“镜像全部旧文件”，探针必须 exit 1 并报告跨账号缺陷。"""
    env = {**os.environ, "INDEP_MUTATE_MIRROR_ALL": "1"}
    completed = subprocess.run(
        [sys.executable, str(REPO_ROOT / "tests" /
                             "independent_final_counterexample_probe.py")],
        cwd=str(REPO_ROOT), env=env, capture_output=True, text=True, timeout=180)
    assert completed.returncode == 1, completed.stdout + completed.stderr
    assert "旧哈希 journal 跨账号覆盖或丢失" in completed.stdout
    assert "DEFECT" in completed.stdout


def test_mirror_all_mutation_would_overwrite_other_account_file(tmp_path, monkeypatch):
    """直接证明 mirror-all 故障会改动另一账号旧文件（normal probe 会判 DEFECT）。"""
    base = tmp_path / "mirror-mutation"
    data_dir = base / "userdata"
    old_dir = data_dir / "sss_authoritative"
    old_dir.mkdir(parents=True)
    other = old_dir / "b.json"
    other.write_text(json.dumps({
        "version": 1,
        "records": [{
            "journal_id": "old-b", "identifier": "old-b",
            "batch_key": "2026-09-16|18758187838",
            "delivery_date": "2026-09-16", "account": "18758187838",
            "platform": "http://local.invalid", "fingerprint": {},
            "status": "unresolved",
        }],
    }, ensure_ascii=False), encoding="utf-8")
    before = other.read_bytes()
    work = base / "work"
    work.mkdir()
    (work / "hidden").write_text("1", encoding="utf-8")
    child = _spawn(work, work / "configured.json", account="18758187837",
                   set_authority=False,
                   env_extra={"YIKOU_DATA_DIR": str(data_dir),
                              "INDEP_MUTATE_MIRROR_ALL": "1",
                              "INDEP_SSS_URL": "http://local.invalid"})
    out, err = child.communicate(timeout=40)
    assert child.returncode == 0, err or out
    after = other.read_bytes()
    assert after != before, "mirror-all 故障注入未改动 B 账号文件，探针无法检测"
    text = after.decode("utf-8", errors="replace")
    assert "old-a" in text or "18758187837" in text
