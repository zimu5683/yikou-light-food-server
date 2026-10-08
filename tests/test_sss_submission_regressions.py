"""闪时送异常响应、跨餐任务身份与恢复状态的离线回归。"""
from __future__ import annotations

import datetime as dt
import json
import time
from threading import Event

import requests

import pytest

from app.integrations.api_client import ApiError, SssApiClient, SssTransportError
from app.ordering import diagnostics as sss_diagnostics
from app.ordering import payload as sss_payload
from app.ordering import reconcile as sss_reconcile
from app.ordering import submission as sss_submission
from app.ordering import uncertain as sss_uncertain


INTERNAL_ERROR = "操作失败，java.lang.IndexOutOfBoundsException: Index: 0, Size: 0"


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    monkeypatch.setenv("YIKOU_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("YIKOU_SSS_LOCK_ROOT", str(tmp_path / "locks"))
    monkeypatch.setenv("YIKOU_SSS_AUTHORITATIVE_ROOT", str(tmp_path / "authority"))
    for key in ("YIKOU_SSS_UNCERTAIN_PATH", "YIKOU_SSS_AUTHORITATIVE_PATH",
                "YIKOU_SSS_AUTHORITY_LOCATIONS"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(sss_reconcile, "_SSS_SERVER_PREFILTER", False)
    monkeypatch.setattr(sss_reconcile, "_RECONCILE_POLL_INTERVAL_S", 0.0)
    monkeypatch.setattr(sss_submission, "_PREFILTER_ZERO_RETRY_DELAY_S", 0.0)


def _tasks(*, both_meals=False):
    order = {"row": 63, "name": "测试收件人", "phone": "13800000001", "door": "A101"}
    return sss_payload._collect_tasks(
        {"午餐": [dict(order)], **({"晚餐": [dict(order)]} if both_meals else {})},
        99, {"lnt": 1.0, "lat": 2.0, "areaCode": "330110", "addressDetail": "测试地址"},
        "轻食", account="test-account", batch_id="test-batch",
        now=dt.datetime(2026, 10, 8, 7, 30),
    )


def _empty_fetch(path):
    return {"success": True, "result": {"records": [], "total": 0}}


def _record(task):
    return {**task["payload"], "id": task["client_request_id"],
            "created_at": int(time.time() * 1000)}


@pytest.mark.parametrize("response", [
    {"success": False, "message": INTERNAL_ERROR},
    {"success": False, "code": 500, "message": "操作失败"},
    {"success": False, "code": 500},
    {"success": False, "code": 422},
    {"success": False, "message": "boom"},
    {"success": False, "message": "系统繁忙，请稍后再试"},
    {"success": False, "message": "java.lang.RuntimeException: 余额不足服务异常"},
    {"success": False, "message": "java.lang.RuntimeException: token失效，请重新登陆"},
    {"success": False, "message": "地址无效", "exception": "java.lang.NullPointerException"},
    {"success": False, "message": "地址无效", "code": 500},
])
def test_unknown_rejection_is_not_evidence_of_no_order(response):
    with pytest.raises(sss_submission._SubmissionUncertain):
        sss_submission._check_success(response)


def test_java_failure_keeps_journal_and_never_offers_repost(tmp_path):
    task = _tasks()[0]
    key = sss_uncertain.batch_key("2026-10-08", "excel", "test-account")
    journal = tmp_path / "journal.json"
    posts, decisions = [], []

    def submit(body):
        posts.append(body)
        return {"success": False, "message": INTERNAL_ERROR}

    def sink(entries, meta):
        sss_uncertain.append_uncertain_records(journal, key, entries, meta=meta)

    outcome = {}
    final, reconciled = sss_submission._run_reconciled_submission(
        [task], lambda: (submit, lambda: None), _empty_fetch, Event(), None,
        lambda identifier, error: decisions.append(identifier) or "retry", 4,
        uncertain_sink=sink,
        uncertain_discard=lambda identifiers: sss_uncertain.discard_uncertain_records(
            journal, key, identifiers),
        journal_meta={"delivery_date": "2026-10-08", "account": "test-account",
                      "batch_started_at": time.time()}, outcome=outcome,
    )

    assert len(posts) == 1
    assert decisions == []
    assert reconciled and final is not None and final.missing == [task]
    assert outcome["uncertain"] == [task["identifier"]]
    assert outcome["failures"] == {}
    pending = sss_uncertain.pending_records(sss_uncertain.load_journal(journal)["records"], key)
    assert len(pending) == 1 and pending[0]["status"] == "unresolved"
    assert "java.lang.IndexOutOfBoundsException" in pending[0]["error"]
    remaining, _, resolved = sss_uncertain.resolve_pending_records(
        journal, key, _empty_fetch, attempts=1)
    assert resolved == 0 and len(remaining) == 1


def test_same_row_and_name_in_different_meals_have_distinct_identifiers():
    lunch, dinner = _tasks(both_meals=True)
    assert lunch["identifier"] != dinner["identifier"]
    assert lunch["client_request_id"] != dinner["client_request_id"]
    result = sss_reconcile._reconcile_tasks(
        [lunch, dinner], lambda path: {"success": True, "result": {
            "records": [_record(lunch), _record(dinner)], "total": 2}})
    assert len(result.confirmed) == 2
    assert not result.missing and result.duplicate_count == 0


def test_meal_failure_is_not_hidden_by_other_meals_success(tmp_path):
    lunch, dinner = _tasks(both_meals=True)
    key = sss_uncertain.batch_key("2026-10-08", "excel", "test-account")
    journal = tmp_path / "journal.json"
    station = []

    def submit(body):
        if body["expectedDeliveryTime"].endswith("11:00:00"):
            raise sss_submission._SubmissionUncertain("ReadTimeout")
        station.append(_record(dinner))
        return {"success": True}

    def sink(entries, meta):
        sss_uncertain.append_uncertain_records(journal, key, entries, meta=meta)

    outcome = {}
    final, reconciled = sss_submission._run_reconciled_submission(
        [lunch, dinner], lambda: (submit, lambda: None),
        lambda path: {"success": True, "result": {"records": list(station), "total": len(station)}},
        Event(), None, None, 2, uncertain_sink=sink,
        uncertain_clear=lambda identifiers: sss_uncertain.resolve_uncertain_records(
            journal, key, identifiers),
        uncertain_discard=lambda identifiers: sss_uncertain.discard_uncertain_records(
            journal, key, identifiers),
        journal_meta={"delivery_date": "2026-10-08", "account": "test-account",
                      "batch_started_at": time.time()}, outcome=outcome,
    )
    assert reconciled and final is not None
    assert final.confirmed == {dinner["identifier"]}
    assert final.missing == [lunch]
    assert outcome["uncertain"] == [lunch["identifier"]]
    pending = sss_uncertain.pending_records(sss_uncertain.load_journal(journal)["records"], key)
    assert len(pending) == 1
    assert pending[0]["fingerprint"]["expected_delivery_time"].endswith("11:00:00")


def test_successful_retry_clears_current_failure_state():
    task = _tasks()[0]
    posts, station = [], []

    def submit(body):
        posts.append(body)
        if len(posts) == 1:
            return {"success": False, "message": "地址无效"}
        station.append(_record(task))
        return {"success": True}

    outcome = {}
    final, reconciled = sss_submission._run_reconciled_submission(
        [task], lambda: (submit, lambda: None),
        lambda path: {"success": True, "result": {"records": list(station), "total": len(station)}},
        Event(), None, lambda *args: "retry", 4, outcome=outcome,
    )
    assert len(posts) == 2
    assert reconciled and final is not None and final.confirmed == {task["identifier"]}
    assert outcome["failure_ids"] == []
    assert outcome["failure_count"] == 0 and outcome["failures"] == {}


def test_reconciled_failure_does_not_remain_a_current_error():
    task = _tasks()[0]
    station, decisions = [], []

    def submit(body):
        station.append(_record(task))
        return {"success": False, "message": INTERNAL_ERROR}

    outcome = {}
    final, reconciled = sss_submission._run_reconciled_submission(
        [task], lambda: (submit, lambda: None),
        lambda path: {"success": True, "result": {"records": list(station), "total": len(station)}},
        Event(), None, lambda *args: decisions.append(args) or "retry", 4, outcome=outcome,
    )
    assert decisions == []
    assert reconciled and final is not None and final.confirmed == {task["identifier"]}
    assert outcome["uncertain"] == [] and outcome["failure_ids"] == []
    assert task["identifier"] not in outcome["errors"]


def test_fork_preserves_login_cookies_without_sharing_session():
    root = SssApiClient("https://example.invalid", "test-account", "test-password")
    root.token = "test-token"
    root.session.cookies.set("route", "backend-a", domain="example.invalid", path="/")
    root.session.headers["X-Test-Session"] = "login-context"
    worker = root.fork()
    try:
        assert worker.session is not root.session
        assert worker.token == root.token
        assert worker.session.cookies.get("route", domain="example.invalid", path="/") == "backend-a"
        assert worker.session.headers["X-Test-Session"] == "login-context"
        worker.session.cookies.set("route", "backend-b", domain="example.invalid", path="/")
        assert root.session.cookies.get("route", domain="example.invalid", path="/") == "backend-a"
    finally:
        worker.close()
        root.close()


def test_any_http_server_error_on_create_is_uncertain():
    class Response:
        status_code = 501

        def json(self):
            return {"success": False, "message": "地址无效"}

    client = SssApiClient("https://example.invalid", "test-account", "test-password")
    client.session.request = lambda *args, **kwargs: Response()
    try:
        with pytest.raises(SssTransportError, match="HTTP 501"):
            client.post_json("/consumer/order/one-touch-send/create-order-from-client", {})
    finally:
        client.close()


def test_http_metadata_stays_out_of_platform_payload_and_masks_context():
    client = SssApiClient("https://example.invalid", "test-account", "secret-password")
    client.token = "secret-token"
    client.session.cookies.set("route", "secret-route")
    body = _tasks()[0]["payload"]
    platform_payload = {"success": False, "message": INTERNAL_ERROR, "code": 500}

    def request(method, url, **kwargs):
        assert kwargs["headers"]["terminal"] == "web"
        response = requests.Response()
        response.status_code = 200
        response._content = json.dumps(platform_payload).encode("utf-8")
        response.request = requests.Request(method, url, json=kwargs["json"]).prepare()
        return response

    client.session.request = request
    try:
        response = client.post_json("/consumer/order/one-touch-send/create-order-from-client", body)
        assert response == platform_payload
        assert response.diagnostics["http_status"] == 200
        assert len(response.diagnostics["wire_digest"]) == 16
        assert "secret-token" not in json.dumps(response.diagnostics)
        assert "secret-route" not in json.dumps(response.diagnostics)
        worker = client.fork()
        worker.session.request = request
        try:
            cloned_response = worker.post_json("/consumer/order/one-touch-send/create-order-from-client", body)
            assert cloned_response.diagnostics == response.diagnostics
        finally:
            worker.close()
    finally:
        client.close()


def test_diagnostic_log_contains_only_safe_submission_metadata():
    task = _tasks()[0]
    response = {"success": False, "message": INTERNAL_ERROR + " secret-token 13800000001",
                "code": 500, "receiveName": "测试收件人", "token": "secret-token"}
    record = sss_diagnostics.submission_diagnostic(
        task, response, started_at=1.0, elapsed_s=0.7, state="uncertain",
        payload_hash=sss_diagnostics.payload_digest(task["payload"]),
        transport={"http_status": 200, "token": "secret-token"},
    )
    sss_diagnostics.write_submission_diagnostic(record)
    text = sss_diagnostics.diagnostic_log_path().read_text(encoding="utf-8")
    assert json.loads(text) == record
    assert record["server_exception"] == "java.lang.IndexOutOfBoundsException"
    assert record["row"] == 63 and record["sheet"] == "午餐"
    for private_value in ("测试收件人", "13800000001", "测试地址", "secret-token", "test-account"):
        assert private_value not in text


def test_diagnostic_failure_does_not_change_success_or_resend(monkeypatch):
    task = _tasks()[0]
    posts = []

    def broken_write(record):
        raise OSError("disk full")

    def submit(body):
        posts.append(body)
        return {"success": True}

    monkeypatch.setattr(sss_submission, "write_submission_diagnostic", broken_write)
    logs = []
    result = sss_submission._submit_tasks_concurrent([task], submit, Event(), logs.append, 1)
    assert len(posts) == 1
    assert result.succeeded == {task["identifier"]}
    assert any("提交诊断记录写入失败" in message for message in logs)


def test_retry_diagnostics_identify_same_payload_and_serial_retry():
    task = _tasks()[0]
    station, posts = [], []

    def submit(body):
        posts.append(body)
        if len(posts) == 1:
            return {"success": False, "message": "地址无效"}
        station.append(_record(task))
        return {"success": True}

    sss_submission._run_reconciled_submission(
        [task], lambda: (submit, lambda: None),
        lambda path: {"success": True, "result": {"records": list(station), "total": len(station)}},
        Event(), None, lambda *args: "retry", 4,
    )
    records = [json.loads(line) for line in sss_diagnostics.diagnostic_log_path().read_text(
        encoding="utf-8").splitlines()]
    assert len(records) == 2
    assert records[0]["payload_digest"] == records[1]["payload_digest"]
    assert records[0]["client_request_id"] == records[1]["client_request_id"]
    assert records[0]["workers"] == 4 and records[1]["workers"] == 1
    assert records[0]["state"] == "failure" and records[1]["state"] == "success"


@pytest.mark.parametrize("error", [
    ApiError("java.lang.RuntimeException: token失效，请重新登陆"),
    SssTransportError("请求地址 /order/40101 返回 HTTP 503"),
])
def test_technical_error_does_not_enter_auth_repost_path(error):
    class Client:
        def post_json(self, path, body):
            raise error

    with pytest.raises(sss_submission._SubmissionUncertain):
        sss_submission._post_one(Client(), {})


@pytest.mark.parametrize("response", [
    {"success": False, "code": 10000,
     "message": "java.lang.Exception: token失效，请重新登陆"},
    {"success": False, "code": 10000,
     "message": "java.lang.Error: token失效，请重新登陆"},
    {"success": False, "code": 500, "message": "token失效，请重新登陆"},
    {"success": False, "errorCode": 500, "message": "地址无效"},
    {"success": False, "error_code": 503, "message": "token失效，请重新登陆"},
])
def test_server_failure_never_relogs_or_discards_journal(response, tmp_path):
    task = _tasks()[0]
    key = sss_uncertain.batch_key("2026-10-08", "excel", "test-account")
    journal = tmp_path / "journal.json"
    posts, relogins, decisions = [], [], []
    client = SssApiClient("https://example.invalid", "test-account", "test-password")

    class Response:
        status_code = 200

        def json(self):
            return response

    def request(*args, **kwargs):
        posts.append(kwargs["json"])
        return Response()

    client.session.request = request
    outcome = {}
    try:
        sss_submission._run_reconciled_submission(
            [task], lambda: (lambda body: sss_submission._post_one(client, body), lambda: None),
            _empty_fetch, Event(), None, lambda *args: decisions.append(args) or "retry", 1,
            relogin=lambda: relogins.append(True),
            uncertain_sink=lambda entries, meta: sss_uncertain.append_uncertain_records(
                journal, key, entries, meta=meta),
            uncertain_discard=lambda identifiers: sss_uncertain.discard_uncertain_records(
                journal, key, identifiers),
            journal_meta={"delivery_date": "2026-10-08", "account": "test-account",
                          "batch_started_at": time.time()}, outcome=outcome,
        )
    finally:
        client.close()
    assert len(posts) == 1 and relogins == [] and decisions == []
    assert outcome["uncertain"] == [task["identifier"]]
    pending = sss_uncertain.pending_records(sss_uncertain.load_journal(journal)["records"], key)
    assert len(pending) == 1 and pending[0]["status"] == "unresolved"


@pytest.mark.parametrize("message", [
    "java.lang.Exception: token=secret-token 13800000001 测试地址",
    "操作失败 token=secret-token 13800000001 测试地址",
])
def test_unknown_error_details_never_echo_private_values(message):
    with pytest.raises(sss_submission._SubmissionUncertain) as caught:
        sss_submission._check_success({"success": False, "message": message})
    for private_value in ("secret-token", "13800000001", "测试地址"):
        assert private_value not in str(caught.value)
