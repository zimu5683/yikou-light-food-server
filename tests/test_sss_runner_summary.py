"""闪时送 runner 的本轮提交统计（``summary.submission``）离线回归。

驱动**真实** ``run_sss_job`` + 伪客户端（不联网、状态目录隔离），逐项锁死：

- ``target_total`` = 本批目标单数；提交前已有 / 本轮提交 / 尝试 / 成功响应 /
  技术异常 / 明确拒绝 / 未发送 / 本轮新增站内确认 / 总确认 / 未确认全部来自
  真实调用与站内对账集合，不从进度里程碑推测；
- 「总确认 = 提交前已有 + 本轮新增」：``created``（站内确认总数）绝不能被说成
  “本轮新建”；
- 站内数量没读取到（对账未完成）时一律 ``None``（未知），不冒充 0；
- 全部已存在、无单、干跑、预检、余额闸门、取消未派发、收尾对账失败都有覆盖；
- 旧字段（``confirmed`` / ``unconfirmed`` / ``success_responses`` / ``not_sent``
  / ``created`` …）原样保留，新计数只放在 ``summary.submission``。
"""
from __future__ import annotations

import datetime as dt
from threading import Event
from types import SimpleNamespace

import pytest
from openpyxl import Workbook

from app.ordering import payload as sss_payload
from app.ordering import reconcile as sss_reconcile
from app.ordering import runner as sss_runner
from app.ordering import submission as sss_submission

INTERNAL_ERROR = "操作失败，java.lang.IndexOutOfBoundsException: Index: 0, Size: 0"
ACCOUNT = "test-account"
STORE_ID = 211053
STORE_NAME = "一口轻食"
ADDRESS = {"lnt": 119.728224, "lat": 30.256632, "areaCode": "330110",
           "addressDetail": "浙江农林大学东湖校区"}


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    """状态目录全部落在 tmp_path；并清掉跨用例的窗口自检缓存。"""
    monkeypatch.setenv("YIKOU_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("YIKOU_SSS_LOCK_ROOT", str(tmp_path / "locks"))
    monkeypatch.setenv("YIKOU_SSS_AUTHORITATIVE_ROOT", str(tmp_path / "authority"))
    monkeypatch.setenv("YIKOU_SSS_AUTHORITY_LOCATIONS",
                       str(tmp_path / "authority-locations.json"))
    for key in ("YIKOU_SSS_UNCERTAIN_PATH", "YIKOU_SSS_AUTHORITATIVE_PATH"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(sss_reconcile, "_SSS_SERVER_PREFILTER", False)
    monkeypatch.setattr(sss_reconcile, "_RECONCILE_POLL_INTERVAL_S", 0.0)
    monkeypatch.setattr(sss_submission, "_PREFILTER_ZERO_RETRY_DELAY_S", 0.0)
    sss_reconcile._WINDOW_CHECK_CACHE.update({"verdict": "", "at": 0.0})
    yield
    sss_reconcile._WINDOW_CHECK_CACHE.update({"verdict": "", "at": 0.0})


def _rows(count):
    """名单行：行号/姓名/门牌/电话互不相同，指纹不会互相干扰。"""
    return [{"row": 3 + index, "name": f"测试{index}",
             "door": f"A1{index:03d}", "phone": f"1380000{index:04d}"}
            for index in range(count)]


def _write_excel(path, rows):
    wb = Workbook()
    ws = wb.active
    ws.title = "午餐"
    ws.append(["姓名", "门牌号", "电话", "送达时间"])
    ws.append(["占位", "", "", ""])
    for row in rows:
        ws.append([row["name"], row["door"], row["phone"], "11:00"])
    wb.save(path)
    wb.close()
    return path


def _tasks(rows):
    """与 runner 同参构造任务：送达时间按当前时钟计算（16:00 后顺延次日）。"""
    return sss_payload._collect_tasks({"午餐": rows}, STORE_ID, ADDRESS, "轻食",
                                      account=ACCOUNT, batch_id="test-batch",
                                      now=dt.datetime.now())


def _station_record(task):
    """站内列表记录：字段与真实商品列表一致，指纹才能与任务对上。"""
    payload = task["payload"]
    return {
        "id": task["identifier"], "orderSn": "SN-1",
        "receiveName": payload["receiveName"],
        "receivePhone": payload["receivePhone"],
        "expectedDeliveryTime": payload["expectedDeliveryTime"],
        "orderType": payload["orderType"], "storeId": payload["storeId"],
        "goodsDetail": payload["goodsDetail"],
        "receiveAddress": dict(payload["receiveAddress"]),
        "user": {"mobile": task["account"]},
        "created_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def _task_by_phone(tasks, phone):
    return next(task for task in tasks
                if task["payload"]["receivePhone"] == phone)


def _install_fake_client(monkeypatch, *, station, posts, respond, tasks,
                         balance=500.0, fail_list_after_posts=False,
                         fail_lists_always=False):
    """把 runner 的 ``SssApiClient`` 换成离线伪客户端（绝不联网）。"""

    class FakeClient:
        """离线伪客户端：get_json 按路径返回站内剧本，post_json 交给测试剧本。"""

        def __init__(self, *args, **kwargs):
            pass

        def fork(self):
            return self

        def fetch_captcha(self):
            return b"\x89PNG fake"

        def login(self, code):
            assert code == "1234"

        def close(self):
            pass

        def get_json(self, path):
            if "one-touch-send/list" in path:
                if fail_lists_always or (fail_list_after_posts and posts):
                    raise RuntimeError("订单列表接口不可用")
                return {"success": True,
                        "result": {"records": list(station), "total": len(station)}}
            if "get-login-user-account" in path:
                return {"success": True,
                        "result": {"totalAmount": balance, "freezeAmount": 0.0}}
            if "queryStoreAddresses" in path:
                return {"success": True,
                        "result": {"records": [{"id": STORE_ID, "name": STORE_NAME}],
                                   "total": 1}}
            raise AssertionError(f"未预期的 GET：{path}")

        def post_json(self, path, body=None):
            posts.append(body)
            return respond(body, len(posts) - 1, tasks)

    monkeypatch.setattr(sss_runner, "SssApiClient", FakeClient)


def _run_batch(tmp_path, monkeypatch, *, rows, station,
               respond=None, balance=500.0, dry_run=False, preflight=False,
               unit_price=0.0, stop_before_dispatch=False,
               fail_list_after_posts=False, fail_lists_always=False):
    """跑一次真实 ``run_sss_job``：伪客户端 + 站内剧本，返回 (result, logs, posts)。

    ``respond(body, index, tasks)`` 决定每次 POST 的响应（默认：成功且立即可见）。
    ``station`` 是站内列表的可变列表（测试可在 respond 里追加记录）。
    """
    excel = _write_excel(tmp_path / "闪时送.xlsx", rows)
    tasks = _tasks(rows)
    posts: list[dict] = []
    logs: list[str] = []

    def default_respond(body, index, tasks):
        match = _task_by_phone(tasks, body["receivePhone"])
        station.append(_station_record(match))
        return {"success": True}

    _install_fake_client(monkeypatch, station=station, posts=posts,
                         respond=respond or default_respond, tasks=tasks,
                         balance=balance, fail_list_after_posts=fail_list_after_posts,
                         fail_lists_always=fail_lists_always)

    stop_event = Event()
    if stop_before_dispatch:
        real_digest = sss_submission.payload_digest

        def digest_then_stop(payload):
            # 纯准备期间收到停止：请求不得发出，也不占序号。
            digest = real_digest(payload)
            stop_event.set()
            return digest

        monkeypatch.setattr(sss_submission, "payload_digest", digest_then_stop)

    cfg = SimpleNamespace(
        sss_excel_path=str(excel), sss_order_source="excel", sss_account=ACCOUNT,
        sss_dry_run=dry_run, sss_preflight=preflight, sss_store_name=STORE_NAME,
        sss_common_address="嗯哼", sss_use_fixed_address=True,
        sss_fixed_lnt=ADDRESS["lnt"], sss_fixed_lat=ADDRESS["lat"],
        sss_fixed_area_code=ADDRESS["areaCode"],
        sss_fixed_address_detail=ADDRESS["addressDetail"], sss_product_name="轻食",
        sss_url="https://example.invalid", sss_store_id=STORE_ID,
        sss_store_name_cached=STORE_NAME, sss_max_workers=1,
        sss_unit_price=unit_price, sss_read_timeout_s=20.0,
        sss_idempotency_field="", sss_submit_min_interval_s=0,
    )
    result = sss_runner.run_sss_job(cfg, stop_event, logs.append, password="x",
                                    captcha_callback=lambda img: "1234")
    return result, logs, posts


# ----------------------------------------------------------------------
# 现场量级：目标 99 · 提交前已有 26 · 本轮提交 73 · 成功 25 · 技术异常 48
# → 本轮新增站内确认 25，总确认 51，未确认 48
# ----------------------------------------------------------------------
def test_live_batch_reports_exact_counts_and_never_claims_new_creations(
        tmp_path, monkeypatch):
    rows = _rows(99)
    tasks = _tasks(rows)
    station = [_station_record(task) for task in tasks[:26]]
    posts_seen: list[str] = []

    def respond(body, index, tasks):
        posts_seen.append(body["receivePhone"])
        if index < 25:
            station.append(_station_record(_task_by_phone(tasks, body["receivePhone"])))
            return {"success": True}
        return {"success": False, "message": INTERNAL_ERROR}

    result, logs, posts = _run_batch(tmp_path, monkeypatch, rows=rows,
                                     station=station, respond=respond)

    assert len(posts) == 73 and len(posts_seen) == 73
    stats = result["summary"]["submission"]
    assert stats == {
        "target_total": 99, "preconfirmed": 26, "submitted": 73, "attempts": 73,
        "success_responses": 25, "technical_errors": 48, "explicit_rejections": 0,
        "auth_rejections": 0, "balance_rejections": 0, "not_sent": 0,
        "newly_confirmed": 25, "confirmed": 51, "unconfirmed": 48,
        "reconciled": True,
    }
    assert result["status"] == "unconfirmed"
    # 旧字段保留兼容：created 仍是站内确认总数，不是“本轮新建”。
    assert result["processed"] == 99 and result["created"] == 51
    for key in ("status", "confirmed", "unconfirmed", "stopped", "failed",
                "next_action", "uncertain", "explicit_failures", "not_sent",
                "success_responses", "reconciled"):
        assert key in result["summary"], key
    assert result["summary"]["confirmed"] == 51
    assert result["summary"]["unconfirmed"] == 48
    assert result["summary"]["success_responses"] == 25
    assert result["summary"]["not_sent"] == 0
    assert result["summary"]["reconciled"] is True

    text = "\n".join(logs)
    counts = [line for line in logs if line.startswith("本轮提交统计：")]
    assert counts == [
        "本轮提交统计：目标 99 单；提交前已有 26 单；本轮提交 73 单（73 次 POST）；"
        "成功响应 25 次；技术异常（结果未知）48 次；明确拒绝 0 次；未发送 0 单；"
        "本轮新增站内确认 25 单；总确认 51/99；未确认 48 单",
    ]
    assert "站内确认 51/99（提交前已有 26，本轮新增 25）" in text
    # 26 + 25 = 51：不能说成“本轮新建 51”，也不能说“已创建 51 单”。
    assert "新建" not in text
    assert "已创建 51" not in text
    # 精确计数行不含客户姓名/电话。
    assert "测试0" not in counts[0] and "1380000" not in counts[0]
    # 未确认的 48 单有保守收尾文案，不承诺零重复。
    assert "核对并补单" in result["next_action"]
    assert "仍可能重复下单" in result["next_action"]


def test_all_orders_already_in_station_reports_zero_submission(
        tmp_path, monkeypatch):
    rows = _rows(5)
    tasks = _tasks(rows)
    station = [_station_record(task) for task in tasks]

    result, logs, posts = _run_batch(tmp_path, monkeypatch, rows=rows,
                                     station=station)

    assert posts == []
    assert result["status"] == "confirmed"
    assert result["summary"]["submission"] == {
        "target_total": 5, "preconfirmed": 5, "submitted": 0, "attempts": 0,
        "success_responses": 0, "technical_errors": 0, "explicit_rejections": 0,
        "auth_rejections": 0, "balance_rejections": 0, "not_sent": 0,
        "newly_confirmed": 0, "confirmed": 5, "unconfirmed": 0, "reconciled": True,
    }
    # created = 站内确认总数（这里全部是提交前已有的），不是本轮新建。
    assert result["created"] == 5
    assert result["summary"]["submission"]["submitted"] == 0
    assert "均已在站内确认" in "\n".join(logs)


def test_unknown_response_confirmed_later_keeps_the_error_count(
        tmp_path, monkeypatch):
    """技术异常后由站内确认：最终状态清零，但响应异常次数不许消失。"""
    rows = _rows(2)
    station = []

    def respond(body, index, tasks):
        # 两单其实都落单了；第二单的响应是平台内部异常。
        station.append(_station_record(_task_by_phone(tasks, body["receivePhone"])))
        if index == 0:
            return {"success": True}
        return {"success": False, "message": INTERNAL_ERROR}

    result, logs, posts = _run_batch(tmp_path, monkeypatch, rows=rows,
                                     station=station, respond=respond)

    assert len(posts) == 2  # 结果未知绝不自动重发
    assert result["status"] == "confirmed"
    stats = result["summary"]["submission"]
    assert (stats["attempts"], stats["submitted"]) == (2, 2)
    assert stats["success_responses"] == 1 and stats["technical_errors"] == 1
    assert stats["confirmed"] == 2 and stats["unconfirmed"] == 0
    assert stats["newly_confirmed"] == 2 and stats["not_sent"] == 0
    assert "技术异常（结果未知）1 次" in "\n".join(logs)


def test_stop_before_dispatch_reports_zero_attempts_and_all_not_sent(
        tmp_path, monkeypatch):
    """取消发生在派发之前：零 POST、零尝试、全部计入未发送。"""
    rows = _rows(2)
    station = []

    result, logs, posts = _run_batch(tmp_path, monkeypatch, rows=rows,
                                     station=station, stop_before_dispatch=True)

    assert posts == []
    assert result["status"] == "stopped"
    stats = result["summary"]["submission"]
    assert (stats["attempts"], stats["submitted"]) == (0, 0)
    assert stats["success_responses"] == 0 and stats["technical_errors"] == 0
    assert stats["not_sent"] == 2
    assert stats["confirmed"] == 0 and stats["unconfirmed"] == 2
    assert stats["reconciled"] is True
    assert ("本轮提交统计：目标 2 单；提交前已有 0 单；本轮提交 0 单（0 次 POST）；"
            "成功响应 0 次；技术异常（结果未知）0 次；明确拒绝 0 次；未发送 2 单；"
            "本轮新增站内确认 0 单；总确认 0/2；未确认 2 单") in "\n".join(logs)


def test_final_reconciliation_failure_keeps_counts_and_unknown_state(
        tmp_path, monkeypatch):
    """收尾对账失败：数量字段未知（不写 0），真实响应次数必须保留。"""
    rows = _rows(2)
    station = []

    def respond(body, index, tasks):
        station.append(_station_record(_task_by_phone(tasks, body["receivePhone"])))
        return {"success": True}

    result, logs, posts = _run_batch(tmp_path, monkeypatch, rows=rows,
                                     station=station, respond=respond,
                                     fail_list_after_posts=True)

    assert len(posts) == 2
    assert result["status"] == "failed"
    stats = result["summary"]["submission"]
    assert (stats["attempts"], stats["submitted"], stats["success_responses"]) == (2, 2, 2)
    assert stats["technical_errors"] == 0 and stats["not_sent"] == 0
    assert stats["preconfirmed"] == 0
    assert stats["confirmed"] is None and stats["unconfirmed"] is None
    assert stats["newly_confirmed"] is None and stats["reconciled"] is False
    text = "\n".join(logs)
    assert "站内确认 待核对/2" in text
    assert "核对并补单" in result["next_action"]


def test_initial_reconciliation_failure_posts_nothing_and_keeps_state_unknown(
        tmp_path, monkeypatch):
    """开局对账就失败：一条 POST 都不发，站内数量未知（不写 0）。"""
    rows = _rows(3)
    station = []

    result, logs, posts = _run_batch(tmp_path, monkeypatch, rows=rows,
                                     station=station, fail_lists_always=True)

    assert posts == []
    assert result["status"] == "failed"
    stats = result["summary"]["submission"]
    assert stats["target_total"] == 3
    assert (stats["attempts"], stats["submitted"]) == (0, 0)
    assert stats["not_sent"] == 3
    assert stats["preconfirmed"] is None and stats["confirmed"] is None
    assert stats["unconfirmed"] is None and stats["newly_confirmed"] is None
    assert stats["reconciled"] is False
    assert "核对并补单" in result["next_action"]


def test_dry_run_reports_zero_posts_and_unknown_station(tmp_path, monkeypatch):
    rows = _rows(3)
    station = []

    result, logs, posts = _run_batch(tmp_path, monkeypatch, rows=rows,
                                     station=station, dry_run=True)

    assert posts == []
    assert result["status"] == "dry_run"
    stats = result["summary"]["submission"]
    assert stats["target_total"] == 3
    assert (stats["submitted"], stats["attempts"]) == (0, 0)
    assert stats["preconfirmed"] is None and stats["confirmed"] is None
    assert stats["unconfirmed"] is None and stats["newly_confirmed"] is None
    assert stats["not_sent"] == 3 and stats["reconciled"] is False
    # 干跑不冒充正式新建或不存在的量：旧字段仍是 0 单提交、0 单创建。
    assert result["submitted"] == 0 and result["created"] == 0
    assert result["previewed"] == 3


def test_preflight_reports_station_counts_without_any_post(tmp_path, monkeypatch):
    rows = _rows(3)
    tasks = _tasks(rows)
    station = [_station_record(tasks[0])]

    result, logs, posts = _run_batch(tmp_path, monkeypatch, rows=rows,
                                     station=station, preflight=True)

    assert posts == []
    assert result["status"] == "preflight_ok"
    stats = result["summary"]["submission"]
    assert stats["target_total"] == 3 and stats["preconfirmed"] == 1
    assert (stats["submitted"], stats["attempts"]) == (0, 0)
    assert stats["not_sent"] == 2 and stats["newly_confirmed"] == 0
    assert stats["confirmed"] == 1 and stats["unconfirmed"] == 2
    assert stats["reconciled"] is True
    # 预检的 created 是站内匹配数，不是本轮新建。
    assert result["created"] == 1 and result["submitted"] == 0


def test_balance_guard_never_posts_and_keeps_station_unknown(
        tmp_path, monkeypatch):
    rows = _rows(2)
    station = []

    result, logs, posts = _run_batch(tmp_path, monkeypatch, rows=rows,
                                     station=station, unit_price=1.9, balance=1.0)

    assert posts == []
    assert result["status"] == "insufficient_balance"
    stats = result["summary"]["submission"]
    assert stats["target_total"] == 2
    assert (stats["submitted"], stats["attempts"]) == (0, 0)
    assert stats["not_sent"] == 2
    assert stats["confirmed"] is None and stats["unconfirmed"] is None
    assert stats["reconciled"] is False
    assert "余额不足" in "\n".join(logs)


def test_no_orders_reports_zero_target_and_unknown_station(tmp_path, monkeypatch):
    result, logs, posts = _run_batch(tmp_path, monkeypatch, rows=[], station=[])

    assert posts == []
    assert result["status"] == "no_orders"
    assert result["summary"]["submission"] == {
        "target_total": 0, "preconfirmed": None, "submitted": 0, "attempts": 0,
        "success_responses": 0, "technical_errors": 0, "explicit_rejections": 0,
        "auth_rejections": 0, "balance_rejections": 0, "not_sent": 0,
        "newly_confirmed": None, "confirmed": None, "unconfirmed": None,
        "reconciled": False,
    }


# ----------------------------------------------------------------------
# 计数文案本身：未知写「待核对」，登录失效/余额不足只在真的发生时出现。
# ----------------------------------------------------------------------
def test_count_line_marks_unknown_and_only_lists_real_rejections():
    stats = {"target_total": 3, "preconfirmed": None, "submitted": 1, "attempts": 2,
             "success_responses": 0, "technical_errors": 0, "explicit_rejections": 1,
             "auth_rejections": 10, "balance_rejections": 0, "not_sent": 2,
             "newly_confirmed": None, "confirmed": None, "unconfirmed": None,
             "reconciled": False}
    text = sss_runner._format_submission_stats(stats)
    assert text.startswith("目标 3 单；提交前已有 待核对；本轮提交 1 单（2 次 POST）；")
    assert "明确拒绝 1 次；登录失效 10 次；未发送 2 单" in text
    assert "本轮新增站内确认 待核对；总确认 待核对/3；未确认 待核对" in text
    # 余额不足 0 次不占篇幅；站内数量未知时写「待核对」，不写 0。
    assert "余额不足" not in text
    assert sss_runner._confirmed_clause(stats, "站内确认 1/2 单") == \
        "站内确认 待核对/3（未完成对账）"
    assert sss_runner._pending_clause(stats, 99) == "未确认数量待核对"
    # 只有完全没有计数（旧结果）时才用调用方给的旧口径。
    assert sss_runner._confirmed_clause(None, "站内确认 1/2 单") == "站内确认 1/2 单"
    assert sss_runner._pending_clause(None, 5) == "5 单未确认"


# ----------------------------------------------------------------------
# 端到端接线：真实 runner（伪客户端）→ Bridge 消息与 operation.summary.submission
# ----------------------------------------------------------------------
def test_end_to_end_runner_counts_reach_bridge_message_and_operation_summary(
        tmp_path, monkeypatch):
    """一次真实批次的计数必须原样进入 Bridge 消息与 operation.summary。

    这里把两侧拼起来跑：runner 用伪客户端真的提交、对账，Bridge 用真实
    ``_finish_task`` 收尾；界面（前端）读的就是 ``summary.submission`` 与消息。
    """
    import threading
    import time

    from app.api.bridge import Bridge

    bridge = Bridge(config_path=str(tmp_path / "config.json"), is_admin=True)
    reservation = bridge._operations.try_reserve("sss")
    assert reservation.granted
    bridge._task_operation_id = reservation.operation.operation_id
    bridge._worker = None
    bridge._interaction_timeout_s = 10.0

    rows = _rows(3)
    tasks = _tasks(rows)
    station = [_station_record(tasks[0])]  # 提交前已有 1 单

    def respond(body, index, tasks):
        if index == 0:
            station.append(_station_record(_task_by_phone(tasks, body["receivePhone"])))
            return {"success": True}
        return {"success": False, "message": INTERNAL_ERROR}

    posts: list[dict] = []
    _install_fake_client(monkeypatch, station=station, posts=posts,
                         respond=respond, tasks=tasks)

    config = bridge._config
    excel = _write_excel(tmp_path / "闪时送.xlsx", rows)
    config.sss_excel_path = str(excel)
    config.sss_order_source = "excel"
    config.sss_account = ACCOUNT
    config.sss_dry_run = False
    config.sss_preflight = False
    config.sss_store_name = STORE_NAME
    config.sss_use_fixed_address = True
    config.sss_fixed_lnt = ADDRESS["lnt"]
    config.sss_fixed_lat = ADDRESS["lat"]
    config.sss_fixed_area_code = ADDRESS["areaCode"]
    config.sss_fixed_address_detail = ADDRESS["addressDetail"]
    config.sss_product_name = "轻食"
    config.sss_url = "https://example.invalid"
    config.sss_store_id = STORE_ID
    config.sss_store_name_cached = STORE_NAME
    config.sss_max_workers = 1
    config.sss_unit_price = 0
    config.sss_submit_min_interval_s = 0
    config.sss_idempotency_field = ""

    worker = threading.Thread(target=lambda: bridge._run_sss(config, "pw"), daemon=True)
    worker.start()
    captcha_id = ""
    deadline = time.time() + 10.0
    while time.time() < deadline and not captcha_id:
        for event in bridge.drain_events(0)["events"]:
            if event["event"] == "captcha":
                captcha_id = str(event["payload"]["id"])
        time.sleep(0.01)
    assert captcha_id, "runner 没有请求验证码"
    assert bridge.resolve_captcha(captcha_id, "1234")["ok"] is True
    worker.join(20.0)
    assert not worker.is_alive(), "闪时送任务没有在超时内结束"

    assert len(posts) == 2  # 只提交站内缺失的 2 单，结果未知绝不重发
    operation = bridge.operation_status(reservation.operation.operation_id)
    assert operation["status"] != "success"
    submission = operation["summary"]["submission"]
    assert submission == {
        "target_total": 3, "preconfirmed": 1, "submitted": 2, "attempts": 2,
        "success_responses": 1, "technical_errors": 1, "explicit_rejections": 0,
        "auth_rejections": 0, "balance_rejections": 0, "not_sent": 0,
        "newly_confirmed": 1, "confirmed": 2, "unconfirmed": 1, "reconciled": True,
    }
    error_events = [event for event in bridge.drain_events(0)["events"]
                    if event["event"] == "task:error"]
    assert error_events and error_events[-1]["payload"]["ok"] is False
    message = error_events[-1]["payload"]["message"]
    assert "提交前已有 1 单" in message
    assert "本轮提交 2 单（2 次 POST）" in message
    assert "技术异常（结果未知）1 次" in message
    assert "本轮新增站内确认 1 单" in message
    assert "总确认 2/3" in message and "未确认 1 单" in message
    assert "新建" not in message
