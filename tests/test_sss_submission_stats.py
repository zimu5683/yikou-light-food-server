"""闪时送提交计数（``submission_stats``）与派发诊断契约的离线回归。

覆盖：已有订单不计 submitted、未知对账一律 null、对账确认不清历史响应次数，
以及 send_seq/round_no/attempt_id/actual_interval_s 的派发语义和响应头脱敏。
"""
from __future__ import annotations

import datetime as dt
import json
import re
import time
from threading import Event, Lock

import pytest
import requests

from app.integrations import api_client as sss_api_client
from app.integrations.api_client import SssApiClient
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
    monkeypatch.setattr(sss_reconcile, "_SSS_SERVER_PREFILTER", False)
    monkeypatch.setattr(sss_reconcile, "_RECONCILE_POLL_INTERVAL_S", 0.0)
    monkeypatch.setattr(sss_submission, "_PREFILTER_ZERO_RETRY_DELAY_S", 0.0)


def _tasks(count):
    """同一餐次的多条任务：行号/电话/门牌各不相同，指纹互不干扰。"""
    orders = [
        {"row": 60 + index, "name": f"测试{index}",
         "phone": f"1380000000{index}", "door": f"A10{index}"}
        for index in range(count)
    ]
    return sss_payload._collect_tasks(
        {"午餐": orders}, 99,
        {"lnt": 1.0, "lat": 2.0, "areaCode": "330110", "addressDetail": "测试地址"},
        "轻食", account="test-account", batch_id="test-batch",
        now=dt.datetime(2026, 10, 8, 7, 30),
    )


def _record(task):
    return {**task["payload"], "id": task["client_request_id"],
            "created_at": int(time.time() * 1000)}


def _row(task):
    return int(re.search(r"第\s*(\d+)\s*行", task["identifier"]).group(1))


def _empty_fetch(path):
    return {"success": True, "result": {"records": [], "total": 0}}


def _station_fetch(station):
    return lambda path: {"success": True,
                         "result": {"records": list(station), "total": len(station)}}


def _submitter(tasks, handler):
    """按 payload 摘要把请求体还原成任务，便于 submit 里读取任务字段。"""
    by_digest = {sss_diagnostics.payload_digest(task["payload"]): task for task in tasks}

    def submit(body):
        return handler(by_digest[sss_diagnostics.payload_digest(body)], body)

    return submit


def _records():
    """读取当前进程写入的脱敏诊断记录（按文件顺序 = 完成顺序）。"""
    path = sss_diagnostics.diagnostic_log_path()
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _stats(outcome):
    stats = outcome["submission_stats"]
    # 提交层不给 target_total（由 runner 按本批目标总数补齐）。
    assert "target_total" not in stats
    return stats


# ----------------------------------------------------------------------
# 派发诊断契约：send_seq / round_no / attempt_id / actual_interval_s
# ----------------------------------------------------------------------
def test_completion_order_inversion_is_recoverable_by_send_seq():
    """并发完成顺序倒置时，仍能按 send_seq 还原本地派发起点顺序且序号唯一。"""
    tasks = _tasks(3)
    call_order: list[int] = []
    order_lock = Lock()
    # 睡眠按“第几次实际调用 submit”决定（并发下派发顺序与任务表顺序无关）：
    # 第 1 次调用最后完成，第 3 次最先完成，文件顺序因此与 send_seq 相反。
    sleeps = [0.45, 0.25, 0.01]

    def handler(task, body):
        with order_lock:
            sleeps_index = len(call_order)
            call_order.append(_row(task))
        time.sleep(sleeps[sleeps_index])
        return {"success": True}

    result = sss_submission._submit_tasks_concurrent(
        tasks, _submitter(tasks, handler), Event(), None, max_workers=3,
        min_interval_s=0.15)
    assert result.succeeded == {task["identifier"] for task in tasks}
    assert result.attempts == 3 and len(result.dispatched) == 3
    assert len(call_order) == 3

    records = _records()
    assert len(records) == 3
    seqs = [record["send_seq"] for record in records]
    assert sorted(seqs) == [1, 2, 3] and len(set(seqs)) == 3
    assert [record["round_no"] for record in records] == [1, 1, 1]
    # 文件顺序是完成顺序：与 send_seq 顺序相反，不能拿它当派发顺序。
    assert seqs == sorted(seqs, reverse=True)
    # 按 send_seq 还原出的顺序必须等于本地调用 submit 的顺序。
    ordered = sorted(records, key=lambda record: record["send_seq"])
    assert [record["row"] for record in ordered] == call_order
    # 首条没有上一条起点 → null；后续是实测间隔，不是 0。
    assert ordered[0]["actual_interval_s"] is None
    for record in ordered[1:]:
        assert 0.1 <= record["actual_interval_s"] <= 1.0
    # 每条实际调用都有独立 attempt_id。
    attempt_ids = {record["attempt_id"] for record in records}
    assert len(attempt_ids) == 3 and all(attempt_ids)


def test_stopped_tasks_never_consume_a_send_seq():
    """停止等待/未派发的任务不占序号：已经停止的轮次里不存在它们的记录。"""
    tasks = _tasks(3)
    logs = []

    pre_stopped = Event()
    pre_stopped.set()
    result = sss_submission._submit_tasks_concurrent(
        tasks, lambda body: {"success": True}, pre_stopped, logs.append,
        max_workers=1, min_interval_s=0.15)
    assert result.attempts == 0 and result.dispatched == set()
    assert result.not_sent == {task["identifier"] for task in tasks}
    assert _records() == []
    assert not any("本轮发送顺序" in message for message in logs)

    stop = Event()

    def handler(task, body):
        stop.set()
        return {"success": True}

    result = sss_submission._submit_tasks_concurrent(
        tasks, _submitter(tasks, handler), stop, None, max_workers=1,
        min_interval_s=0.15)
    records = _records()
    assert [record["send_seq"] for record in records] == [1]
    assert result.attempts == 1
    assert len(result.not_sent) == 2
    assert not any(record["row"] in (61, 62) for record in records)


def test_round_no_defaults_to_one_and_can_be_set_by_the_caller():
    tasks = _tasks(2)

    def handler(task, body):
        return {"success": True}

    sss_submission._submit_tasks_concurrent(
        tasks, _submitter(tasks, handler), Event(), None, 1)
    assert [record["round_no"] for record in _records()] == [1, 1]

    sss_diagnostics.diagnostic_log_path().unlink()
    sss_submission._submit_tasks_concurrent(
        tasks, _submitter(tasks, handler), Event(), None, 1, round_no=3)
    assert [record["round_no"] for record in _records()] == [3, 3]


def test_blocked_preparation_never_inverts_dispatch_order(monkeypatch):
    """准备阶段阻塞时，send_seq/started_at 仍须还原本地 submit 调用顺序。

    第一个任务的报文摘要被卡住，直到第二个任务真正 submit 完成：纯准备必须在
    派发临界区之前完成，否则“准备交错”会让本地调用顺序与 send_seq 相反。
    """
    tasks = _tasks(2)
    first, second = tasks
    real_digest = sss_submission.payload_digest
    first_digest = real_digest(first["payload"])
    release = Event()
    submitted: list[str] = []

    def blocking_digest(payload):
        digest = real_digest(payload)
        if digest == first_digest:
            assert release.wait(5.0), "第二个任务未能先行提交"
        return digest

    def handler(task, body):
        submitted.append(task["identifier"])
        if task["identifier"] == second["identifier"]:
            release.set()
        return {"success": True}

    monkeypatch.setattr(sss_submission, "payload_digest", blocking_digest)
    result = sss_submission._submit_tasks_concurrent(
        tasks, _submitter(tasks, handler), Event(), None, max_workers=2)
    assert result.succeeded == {task["identifier"] for task in tasks}
    assert submitted == [second["identifier"], first["identifier"]]

    records = sorted(_records(), key=lambda record: record["send_seq"])
    assert [record["send_seq"] for record in records] == [1, 2]
    assert [record["client_request_id"] for record in records] == [
        second["client_request_id"], first["client_request_id"]]
    assert records[0]["started_at"] <= records[1]["started_at"]
    assert records[1]["actual_interval_s"] is not None
    assert records[1]["actual_interval_s"] >= 0


def test_stop_set_during_preparation_prevents_dispatch(monkeypatch):
    """纯准备期间收到停止：不得派发 POST，不占序号，也不写诊断。"""
    tasks = _tasks(2)
    stop = Event()
    posts: list[dict] = []
    real_digest = sss_submission.payload_digest

    def digest_then_stop(payload):
        digest = real_digest(payload)
        stop.set()
        return digest

    monkeypatch.setattr(sss_submission, "payload_digest", digest_then_stop)
    result = sss_submission._submit_tasks_concurrent(
        tasks, lambda body: posts.append(body) or {"success": True}, stop, None,
        max_workers=2)
    assert posts == []
    assert result.attempts == 0 and result.dispatched == set()
    assert result.not_sent == {task["identifier"] for task in tasks}
    assert _records() == []


def test_preparation_failure_is_not_sent_and_never_diagnosed(monkeypatch):
    """本地准备失败（如摘要抛 ValueError）：算「未发送」，不占序号/不算技术异常。"""
    tasks = _tasks(3)
    posts: list[dict] = []
    logs: list[str] = []
    real_digest = sss_submission.payload_digest
    broken_digest = real_digest(tasks[1]["payload"])

    def failing_digest(payload):
        if real_digest(payload) == broken_digest:
            raise ValueError("payload 不可序列化：secret-token 13800000001")
        return real_digest(payload)

    monkeypatch.setattr(sss_submission, "payload_digest", failing_digest)
    result = sss_submission._submit_tasks_concurrent(
        tasks, lambda body: posts.append(body) or {"success": True}, Event(),
        logs.append, max_workers=1)
    assert len(posts) == 2
    assert result.not_sent == {tasks[1]["identifier"]}
    assert result.succeeded == {tasks[0]["identifier"], tasks[2]["identifier"]}
    assert result.attempts == 2 and result.dispatched == result.succeeded
    assert result.uncertain == [] and result.failures == []
    assert result.explicit_rejections == 0

    text = "\n".join(logs)
    assert "提交准备失败" in text and "ValueError" in text
    for private in ("secret-token", "13800000001", "不可序列化", "payload"):
        assert private not in text
    counts = [message for message in logs if message.startswith("本轮统计：")]
    assert counts == ["本轮统计：实际发起 2 次 POST（2 单）；成功响应 2，技术异常 0，"
                      "明确拒绝 0，登录失效 0，余额不足 0，未发送 1"]
    # 未派发不写诊断，序号也不给未发送任务。
    assert [record["send_seq"] for record in _records()] == [1, 2]


def test_preparation_failure_is_discarded_from_the_journal(monkeypatch, tmp_path):
    """本地准备失败 = 明确未发送：journal 必须 discard，不能留成「已发送未知」。"""
    tasks = _tasks(2)
    good, broken_task = tasks
    journal = tmp_path / "journal.json"
    key = sss_uncertain.batch_key("2026-10-08", "excel", "test-account")
    station: list[dict] = []
    real_digest = sss_submission.payload_digest
    broken_digest = real_digest(broken_task["payload"])

    def failing_digest(payload):
        if real_digest(payload) == broken_digest:
            raise ValueError("boom")
        return real_digest(payload)

    def handler(task, body):
        station.append(_record(task))
        return {"success": True}

    monkeypatch.setattr(sss_submission, "payload_digest", failing_digest)
    outcome: dict = {}
    final, reconciled = sss_submission._run_reconciled_submission(
        tasks, lambda: (_submitter(tasks, handler), lambda: None),
        _station_fetch(station), Event(), None, None, 2,
        uncertain_sink=lambda entries, meta: sss_uncertain.append_uncertain_records(
            journal, key, entries, meta=meta),
        uncertain_clear=lambda identifiers: sss_uncertain.resolve_uncertain_records(
            journal, key, identifiers),
        uncertain_discard=lambda identifiers: sss_uncertain.discard_uncertain_records(
            journal, key, identifiers),
        journal_meta={"delivery_date": "2026-10-08", "account": "test-account",
                      "batch_started_at": time.time()},
        outcome=outcome)
    assert reconciled and final is not None
    assert final.confirmed == {good["identifier"]}
    stats = _stats(outcome)
    assert stats["attempts"] == 1 and stats["submitted"] == 1
    assert stats["not_sent"] == 1 and stats["technical_errors"] == 0
    assert stats["explicit_rejections"] == 0

    records = sss_uncertain.load_journal(journal)["records"]
    assert sss_uncertain.pending_records(records, key) == []
    assert all(record["status"] != "inflight" for record in records)
    broken_records = [record for record in records
                      if record["identifier"] == broken_task["identifier"]]
    assert broken_records
    assert all(record["status"] != "unresolved" for record in broken_records)


@pytest.mark.parametrize("outcome_state,expected", [
    ("success", {"succeeded": 1, "uncertain": 0, "failures": 0}),
    ("reject", {"succeeded": 0, "uncertain": 0, "failures": 1}),
    ("unknown", {"succeeded": 0, "uncertain": 1, "failures": 0}),
])
def test_diagnostic_failure_never_changes_the_classification(monkeypatch,
                                                             outcome_state, expected):
    """诊断构造失败只丢记录：成功 / 明确拒绝 / 未知三种分类都不改，也不重发。"""
    tasks = _tasks(1)
    posts: list[dict] = []
    logs: list[str] = []

    def handler(task, body):
        posts.append(body)
        if outcome_state == "reject":
            return {"success": False, "message": "地址无效"}
        if outcome_state == "unknown":
            return {"success": False, "message": INTERNAL_ERROR}
        return {"success": True}

    def broken_diagnostic(*args, **kwargs):
        raise ValueError("diagnostic boom 13800000001")

    monkeypatch.setattr(sss_submission, "submission_diagnostic", broken_diagnostic)
    result = sss_submission._submit_tasks_concurrent(
        tasks, _submitter(tasks, handler), Event(), logs.append, max_workers=1)
    assert len(posts) == 1  # 诊断失败绝不触发重发
    assert (len(result.succeeded), len(result.uncertain), len(result.failures)) == (
        expected["succeeded"], expected["uncertain"], expected["failures"])
    assert result.attempts == 1
    assert any("提交诊断构造失败" in message and "ValueError" in message
               for message in logs)
    assert not any("diagnostic boom" in message or "13800000001" in message
                   for message in logs)
    assert _records() == []


def test_non_string_trace_metadata_is_ignored_without_side_effects():
    """白名单元数据只接受合法字符串：非法对象（含 __str__ 抛异常）一律忽略。"""
    class Exploding:
        def __str__(self):
            raise RuntimeError("no __str__ for you")

    assert sss_api_client._valid_server_id(Exploding()) == ""
    assert sss_api_client._valid_server_id(123) == ""
    assert sss_api_client._server_identity({"X-Request-ID": Exploding()}) == {}
    assert sss_api_client._server_identity({"X-Trace-ID": Exploding(),
                                            "X-Correlation-ID": "corr-2"}) == {
        "server_request_id": "corr-2"}

    task = _tasks(1)[0]
    record = sss_diagnostics.submission_diagnostic(
        task, {"success": True}, started_at=1.0, elapsed_s=0.1, state="success",
        payload_hash="x", transport={"server_request_id": Exploding(),
                                     "server_node_id": 42})
    assert "server_request_id" not in record and "server_node_id" not in record
    assert record["state"] == "success"


def test_exploding_header_value_does_not_break_the_request():
    """响应头取值不可字符串化时也绝不能影响请求结果。"""
    class Exploding:
        def __str__(self):
            raise RuntimeError("no __str__ for you")

    client = SssApiClient("https://example.invalid", "test-account", "secret-password")
    body = _tasks(1)[0]["payload"]

    def request(method, url, **kwargs):
        response = requests.Response()
        response.status_code = 200
        response._content = json.dumps({"success": True}).encode("utf-8")
        response.headers["X-Request-ID"] = Exploding()
        return response

    client.session.request = request
    try:
        payload = client.post_json(
            "/consumer/order/one-touch-send/create-order-from-client", body)
    finally:
        client.close()
    assert payload == {"success": True}
    assert "server_request_id" not in payload.diagnostics


def test_paced_dispatch_keeps_interval_when_preparation_is_blocked(monkeypatch):
    """准备阶段阻塞时，两次实际发送的起点仍须遵守配置的固定间隔。

    纯准备发生在节流等待之前，因此阻塞不会占掉发送槽位（否则第二个请求会在
    第一个释放后紧邻调用，低于 min_interval）。
    """
    tasks = _tasks(2)
    first, second = tasks
    real_digest = sss_submission.payload_digest
    first_digest = real_digest(first["payload"])
    release = Event()
    starts: list[float] = []

    def blocking_digest(payload):
        digest = real_digest(payload)
        if digest == first_digest:
            assert release.wait(5.0), "第二个任务未能先完成发送"
        return digest

    def handler(task, body):
        starts.append(time.perf_counter())
        if task["identifier"] == second["identifier"]:
            release.set()
        return {"success": True}

    monkeypatch.setattr(sss_submission, "payload_digest", blocking_digest)
    result = sss_submission._submit_tasks_concurrent(
        tasks, _submitter(tasks, handler), Event(), None, max_workers=2,
        min_interval_s=0.3)
    assert result.succeeded == {task["identifier"] for task in tasks}
    assert len(starts) == 2
    assert starts[1] - starts[0] >= 0.25  # 0.3 秒配置 + 小额测量容差

    records = sorted(_records(), key=lambda record: record["send_seq"])
    assert [record["send_seq"] for record in records] == [1, 2]
    assert records[0]["actual_interval_s"] is None
    assert records[1]["actual_interval_s"] >= 0.25


def test_halt_after_wait_return_prevents_dispatch(monkeypatch):
    """节流等待返回 True 但此时已停止/中止：不得派发 POST、不占序号与调用次数。"""
    tasks = _tasks(2)
    stop = Event()
    posts: list[dict] = []

    def wait_returns_true_while_stopping(stop_event=None, halted=None):
        # 模拟“等待期间恰好收到停止”，但 wait 自己仍返回 True
        stop_event.set()
        return True

    monkeypatch.setattr(sss_submission._SubmitPacer, "wait",
                        staticmethod(wait_returns_true_while_stopping))
    result = sss_submission._submit_tasks_concurrent(
        tasks, lambda body: posts.append(body) or {"success": True}, stop, None,
        max_workers=2)
    assert posts == []
    assert result.attempts == 0 and result.dispatched == set()
    assert result.succeeded == set() and result.uncertain == []
    assert result.not_sent == {task["identifier"] for task in tasks}
    assert _records() == []


@pytest.mark.parametrize("first_outcome", ["balance", "auth"])
def test_abort_after_wait_return_never_dispatches_the_next_post(monkeypatch,
                                                                first_outcome):
    """中止在 wait 返回前一刻生效时，等待中的任务不得再发出（也不占序号）。

    确定性编排：节流关闭时 ``wait`` 直接返回 True（不做中止检查），第二个调用
    先等到中止确实生效再返回 True，因此这条路径只能由派发边界检查兜住。
    """
    tasks = _tasks(2)
    posts: list[str] = []
    entered: list[bool] = []

    def wait_until_aborted(self, stop_event=None, halted=None):
        if entered:
            deadline = time.time() + 5.0
            while time.time() < deadline and not (halted and halted()):
                time.sleep(0.005)
            assert halted and halted(), "中止未生效，测试编排失效"
        entered.append(True)
        return True

    def handler(task, body):
        posts.append(task["identifier"])
        if first_outcome == "auth":
            raise sss_submission._AuthExpired("401")
        return {"success": False, "message": "余额不足"}

    monkeypatch.setattr(sss_submission._SubmitPacer, "wait", wait_until_aborted)
    result = sss_submission._submit_tasks_concurrent(
        tasks, _submitter(tasks, handler), Event(), None, max_workers=2)
    assert len(entered) == 2 and len(posts) == 1
    sent = posts[0]
    if first_outcome == "auth":
        assert result.auth == {sent} and result.auth_error
    else:
        assert result.balance == {sent} and result.balance_error
    assert result.attempts == 1 and result.dispatched == {sent}
    assert result.not_sent == {task["identifier"] for task in tasks} - {sent}
    assert [record["send_seq"] for record in _records()] == [1]


def test_same_client_request_id_manual_retry_gets_a_new_attempt_id():
    """人工重试复用同一个 client_request_id，但必须是两次独立调用。"""
    task = _tasks(1)[0]
    station: list[dict] = []
    calls = {"n": 0}

    def handler(inner_task, body):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"success": False, "message": "地址无效"}
        station.append(_record(inner_task))
        return {"success": True}

    outcome: dict = {}
    final, reconciled = sss_submission._run_reconciled_submission(
        [task], lambda: (_submitter([task], handler), lambda: None),
        _station_fetch(station), Event(), None, lambda *args: "retry", 4,
        outcome=outcome)
    assert reconciled and final is not None and final.confirmed == {task["identifier"]}

    records = _records()
    assert len(records) == 2
    first, second = records
    assert first["client_request_id"] == second["client_request_id"]
    assert first["payload_digest"] == second["payload_digest"]
    assert first["attempt_id"] != second["attempt_id"]
    assert (first["round_no"], second["round_no"]) == (1, 2)
    # 序号是“当前轮内”的起点序号：两轮各自从 1 开始。
    assert (first["send_seq"], second["send_seq"]) == (1, 1)
    assert (first["state"], second["state"]) == ("failure", "success")


def test_round_emits_exact_counts_and_send_markers_without_customer_data():
    tasks = _tasks(12)
    logs = []
    responses = {"n": 0}

    def handler(task, body):
        responses["n"] += 1
        if responses["n"] == 2:
            return {"success": False, "message": INTERNAL_ERROR}
        if responses["n"] == 3:
            return {"success": False, "message": "地址无效"}
        return {"success": True}

    result = sss_submission._submit_tasks_concurrent(
        tasks, _submitter(tasks, handler), Event(), logs.append, max_workers=1)

    counts = [message for message in logs if message.startswith("本轮统计：")]
    assert len(counts) == 1
    assert ("本轮统计：实际发起 12 次 POST（12 单）；成功响应 10，技术异常 1，"
            "明确拒绝 1，登录失效 0，余额不足 0，未发送 0") == counts[0]
    assert (result.attempts, len(result.succeeded), len(result.uncertain),
            result.explicit_rejections) == (12, 10, 1, 1)

    markers = [message for message in logs if message.startswith("本轮发送顺序")]
    assert len(markers) == 1
    marks = markers[0].rsplit("（", 1)[0]  # 最后一个括号后是图例，不能混进计数
    assert marks.count("✔") == 10 and marks.count("✖") == 1
    assert marks.count("⚠") == 1
    assert marks.split("：", 1)[1].split() == [
        f"{seq}{mark}" for seq, mark in zip(
            range(1, 13), ["✔", "✖", "⚠"] + ["✔"] * 9)]

    # 技术异常提示必须指向“结果待对账”，不得声称（或暗示）服务端已回滚。
    notes = [message for message in logs if "未获可确认结果" in message]
    assert len(notes) == 1
    assert "结果待站内对账确认" in notes[0]
    assert "不代表订单未创建" in notes[0]
    assert not any("回滚" in message for message in logs)
    assert not any("自动放宽" in message for message in logs)
    # 十单进度日志保持兼容。
    assert any("本轮收到成功响应 10/12 单" in message for message in logs)
    # 精简结果与计数都不含客户姓名/电话。
    for private in ("测试0", "测试11", "13800000000", "13800000011"):
        assert private not in "".join(logs)


def test_whitelisted_server_identity_headers_are_sanitized():
    """只从白名单响应头取追踪/节点标识，其余响应头与凭据一律不落盘。"""
    client = SssApiClient("https://example.invalid", "test-account", "secret-password")
    client.token = "secret-token"
    body = _tasks(1)[0]["payload"]

    def request(method, url, **kwargs):
        response = requests.Response()
        response.status_code = 200
        response._content = json.dumps({"success": True}).encode("utf-8")
        response.headers.update({
            "X-Request-ID": "req-abc123",
            "X-Node-ID": "node-7",
            "X-Secret-Internal": "secret-header-value",
            "Authorization": "Bearer secret-token",
            "Set-Cookie": "session=secret-cookie",
        })
        return response

    client.session.request = request
    try:
        response = client.post_json(
            "/consumer/order/one-touch-send/create-order-from-client", body)
    finally:
        client.close()
    assert response.diagnostics["server_request_id"] == "req-abc123"
    assert response.diagnostics["server_node_id"] == "node-7"
    dumped = json.dumps(response.diagnostics, ensure_ascii=False)
    for secret in ("secret-header-value", "secret-token", "secret-cookie"):
        assert secret not in dumped
    assert "X-Secret-Internal" not in dumped and "Authorization" not in dumped

    record = sss_diagnostics.submission_diagnostic(
        _tasks(1)[0], response, started_at=1.0, elapsed_s=0.5, state="success",
        payload_hash=sss_diagnostics.payload_digest(body))
    assert record["server_request_id"] == "req-abc123"
    assert record["server_node_id"] == "node-7"
    text = json.dumps(record, ensure_ascii=False)
    for secret in ("secret-header-value", "secret-token", "secret-cookie"):
        assert secret not in text


def test_invalid_or_missing_trace_metadata_is_ignored_not_trusted():
    """长度/字符不合法、结构异常的元数据一律忽略，且不影响结果分类。"""
    assert sss_api_client._server_identity(None) == {}
    assert sss_api_client._server_identity(object()) == {}
    assert sss_api_client._server_identity({"X-Request-ID": "bad value with spaces"}) == {}
    assert sss_api_client._server_identity({"X-Request-ID": "x" * 65}) == {}
    assert sss_api_client._server_identity(
        {"X-Request-ID": "bad value", "X-Correlation-ID": "corr-1"}) == {
        "server_request_id": "corr-1"}
    assert sss_api_client._server_identity({"X-Trace-ID": "trace:1.2-3"}) == {
        "server_request_id": "trace:1.2-3"}
    assert sss_api_client._server_identity({"X-Backend-ID": "backend_9"}) == {
        "server_node_id": "backend_9"}

    task = _tasks(1)[0]
    response = {"success": False, "message": INTERNAL_ERROR}
    record = sss_diagnostics.submission_diagnostic(
        task, response, started_at=1.0, elapsed_s=0.4, state="uncertain",
        payload_hash=sss_diagnostics.payload_digest(task["payload"]),
        transport={"http_status": 200, "server_request_id": "x" * 80,
                   "server_node_id": "bad value"})
    assert "server_request_id" not in record and "server_node_id" not in record
    assert record["server_exception"] == "java.lang.IndexOutOfBoundsException"

    # 响应不是对象时忽略元数据，但记录本身仍可写出。
    array_record = sss_diagnostics.submission_diagnostic(
        task, ["not", "an", "object"], started_at=1.0, elapsed_s=0.4,
        state="uncertain", payload_hash=sss_diagnostics.payload_digest(task["payload"]),
        send_seq=1, attempt_id="nope!")
    assert array_record["response_kind"] == "array"
    assert "send_seq" in array_record and "actual_interval_s" in array_record
    assert "attempt_id" not in array_record  # 非法 attempt_id 不落盘


# ----------------------------------------------------------------------
# submission_stats：真实调用计数与当前最终状态分离
# ----------------------------------------------------------------------
def test_existing_order_is_preconfirmed_not_submitted():
    tasks = _tasks(2)
    existing, missing = tasks
    station = [_record(existing)]
    posts = []

    def handler(task, body):
        posts.append(task["identifier"])
        station.append(_record(task))
        return {"success": True}

    outcome: dict = {}
    final, reconciled = sss_submission._run_reconciled_submission(
        tasks, lambda: (_submitter(tasks, handler), lambda: None),
        _station_fetch(station), Event(), None, None, 4, outcome=outcome)
    assert reconciled and final is not None
    assert final.confirmed == {existing["identifier"], missing["identifier"]}
    assert posts == [missing["identifier"]]

    stats = _stats(outcome)
    assert stats["preconfirmed"] == 1
    assert stats["submitted"] == 1 and stats["attempts"] == 1
    assert stats["success_responses"] == 1
    assert stats["technical_errors"] == 0 and stats["explicit_rejections"] == 0
    assert stats["auth_rejections"] == 0 and stats["balance_rejections"] == 0
    assert stats["not_sent"] == 0
    assert stats["confirmed"] == 2 and stats["unconfirmed"] == 0
    assert stats["newly_confirmed"] == 1
    assert stats["reconciled"] is True


def test_all_orders_already_exist_sends_nothing():
    tasks = _tasks(2)
    station = [_record(task) for task in tasks]
    posts = []
    outcome: dict = {}
    final, reconciled = sss_submission._run_reconciled_submission(
        tasks, lambda: (lambda body: posts.append(body) or {"success": True}, lambda: None),
        _station_fetch(station), Event(), None, None, 4, outcome=outcome)
    assert reconciled and posts == []
    assert final is not None and final.missing == []
    stats = _stats(outcome)
    assert stats["preconfirmed"] == 2 and stats["submitted"] == 0
    assert stats["attempts"] == 0 and stats["not_sent"] == 0
    assert stats["confirmed"] == 2 and stats["newly_confirmed"] == 0
    assert stats["unconfirmed"] == 0 and stats["reconciled"] is True


def test_unknown_reconciliation_is_null_not_zero():
    tasks = _tasks(2)

    def broken_fetch(path):
        raise RuntimeError("列表接口不可用")

    outcome: dict = {}
    final, reconciled = sss_submission._run_reconciled_submission(
        tasks, lambda: (lambda body: {"success": True}, lambda: None),
        broken_fetch, Event(), None, None, 4, outcome=outcome)
    assert final is None and reconciled is False
    stats = _stats(outcome)
    assert stats["preconfirmed"] is None
    assert stats["newly_confirmed"] is None
    assert stats["confirmed"] is None and stats["unconfirmed"] is None
    assert stats["reconciled"] is False
    assert stats["submitted"] == 0 and stats["attempts"] == 0
    # 一次 POST 都没发出：全部目标任务都属于“始终没有 POST”，而不是 0。
    assert stats["not_sent"] == len(tasks)


def test_final_reconciliation_failure_keeps_response_counts():
    """收尾对账失败：数量字段为 null，但本轮真实响应次数必须保留。"""
    tasks = _tasks(2)
    calls = {"n": 0}

    def flaky_fetch(path):
        calls["n"] += 1
        if calls["n"] > 1:
            raise RuntimeError("收尾列表接口不可用")
        return {"success": True, "result": {"records": [], "total": 0}}

    def handler(task, body):
        return {"success": True}

    outcome: dict = {}
    final, reconciled = sss_submission._run_reconciled_submission(
        tasks, lambda: (_submitter(tasks, handler), lambda: None),
        flaky_fetch, Event(), None, None, 4, outcome=outcome)
    assert final is None and reconciled is False
    stats = _stats(outcome)
    assert stats["attempts"] == 2 and stats["submitted"] == 2
    assert stats["success_responses"] == 2
    assert stats["preconfirmed"] == 0
    assert stats["confirmed"] is None and stats["unconfirmed"] is None
    assert stats["newly_confirmed"] is None and stats["reconciled"] is False
    assert stats["not_sent"] == 0


def test_java_exception_then_station_confirmation_keeps_history():
    """平台内部异常后由站内确认：当前状态清零，历史异常次数保留。"""
    task = _tasks(1)[0]
    station: list[dict] = []
    posts = []

    def handler(inner_task, body):
        posts.append(body)
        station.append(_record(inner_task))
        return {"success": False, "message": INTERNAL_ERROR}

    outcome: dict = {}
    final, reconciled = sss_submission._run_reconciled_submission(
        [task], lambda: (_submitter([task], handler), lambda: None),
        _station_fetch(station), Event(), None, None, 4, outcome=outcome)
    assert len(posts) == 1  # 结果未知绝不自动重发
    assert reconciled and final is not None and final.confirmed == {task["identifier"]}
    assert outcome["uncertain"] == []
    stats = _stats(outcome)
    assert stats["technical_errors"] == 1 and stats["success_responses"] == 0
    assert stats["attempts"] == 1 and stats["submitted"] == 1
    assert stats["confirmed"] == 1 and stats["newly_confirmed"] == 1
    assert stats["unconfirmed"] == 0 and stats["not_sent"] == 0


def test_stats_count_explicit_rejection_and_manual_retry():
    task = _tasks(1)[0]
    station: list[dict] = []
    calls = {"n": 0}

    def handler(inner_task, body):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"success": False, "message": "地址无效"}
        station.append(_record(inner_task))
        return {"success": True}

    outcome: dict = {}
    final, reconciled = sss_submission._run_reconciled_submission(
        [task], lambda: (_submitter([task], handler), lambda: None),
        _station_fetch(station), Event(), None, lambda *args: "retry", 4,
        outcome=outcome)
    assert reconciled and final is not None and final.confirmed == {task["identifier"]}
    stats = _stats(outcome)
    assert stats["explicit_rejections"] == 1 and stats["technical_errors"] == 0
    # submitted 数“不同任务”，attempts 数“真实调用”：人工重试只加 attempts。
    assert stats["submitted"] == 1 and stats["attempts"] == 2
    assert stats["success_responses"] == 1
    assert stats["confirmed"] == 1 and stats["newly_confirmed"] == 1


def test_stats_count_balance_abort_and_unattempted_tasks():
    tasks = _tasks(3)
    posts = []

    def handler(task, body):
        posts.append(task["identifier"])
        return {"success": False, "message": "余额不足"}

    outcome: dict = {}
    final, reconciled = sss_submission._run_reconciled_submission(
        tasks, lambda: (_submitter(tasks, handler), lambda: None),
        _empty_fetch, Event(), None, None, 1, outcome=outcome)
    assert len(posts) == 1
    assert reconciled and final is not None
    stats = _stats(outcome)
    assert stats["balance_rejections"] == 1
    assert stats["attempts"] == 1 and stats["submitted"] == 1
    assert stats["not_sent"] == 2
    assert stats["confirmed"] == 0 and stats["unconfirmed"] == 3
    assert stats["newly_confirmed"] == 0 and stats["reconciled"] is True


def test_stats_count_auth_rejection_and_relogin_resend():
    tasks = _tasks(2)
    station: list[dict] = []
    calls = {"n": 0}
    relogins = []

    def handler(task, body):
        calls["n"] += 1
        if calls["n"] == 1:
            raise sss_submission._AuthExpired("401")
        station.append(_record(task))
        return {"success": True}

    outcome: dict = {}
    final, reconciled = sss_submission._run_reconciled_submission(
        tasks, lambda: (_submitter(tasks, handler), lambda: None),
        _station_fetch(station), Event(), None, None, 1,
        relogin=lambda: relogins.append(True), outcome=outcome)
    assert relogins == [True]
    assert calls["n"] == 3  # 1 次 401 + 重登后补发 2 单
    assert reconciled and final is not None
    assert final.confirmed == {task["identifier"] for task in tasks}
    stats = _stats(outcome)
    assert stats["auth_rejections"] == 1 and stats["success_responses"] == 2
    assert stats["attempts"] == 3 and stats["submitted"] == 2
    assert stats["not_sent"] == 0
    assert stats["confirmed"] == 2 and stats["newly_confirmed"] == 2
