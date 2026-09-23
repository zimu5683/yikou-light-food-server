"""独立验证：闪时送未决记录只读核对 + 管理员带审计解除（对抗式复现）。

本文件由独立验证者编写，**不修改任何实现**；目标是尝试推翻下列不变量：

1. 零 POST：只读核对与解除入口全程不调用下单接口（伪客户端记录每一次请求路径）；
2. station_absent 证据门禁：无快照/过期/指纹不一致/覆盖不全/scan_failed/
   station_found_other_day 一律拒绝，且 journal 字节不变；
3. 权限：HTTP 三个新方法对普通用户 403 admin_only；直调 Bridge(is_admin=False)
   的 records/resolve 返回 forbidden 且不写文件（start_sss_review 的直调缺口见
   文末 xfail，属进程内契约不一致，不是远程绕过）；
4. station_present 只标 resolved 并写 resolved_reason/resolved_by；
5. 宽窗不改变严格语义：days_margin=3 不把“预约时间不同”的站内单算成 confirmed；
   days_margin=0 与不传等价（结果与请求查询串逐字一致）；
6. 端到端：解除后 run_sss_job 真的重新 POST 且只发一次；站内已命中时提交前的
   只读对账阻止 POST；
7. fail-closed：journal 损坏/不可读、宽窗扫描异常、写入失败都不得被当成
   “站内没有”。

全部离线：伪 SssApiClient 记录 GET/POST 路径，任何真实网络调用都会让测试失败。
"""
from __future__ import annotations

import datetime as dt
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

from app.api import bridge as bridge_module
from app.api.bridge import Bridge
from app.integrations.api_client import SssApiClient
from app.ordering import reconcile as sss_reconcile
from app.ordering import runner as sss_runner
from app.ordering import uncertain as sss_uncertain
from app.ordering.constants import _CREATE_ORDER_PATH, _ORDER_LIST_PATH
from app.ordering.models import OrderFingerprint
from app.ordering.sss import expected_delivery_date
from app.web.auth import AuthStore
from app.web.server import SESSION_COOKIE, create_server

ACCOUNT = "18758187837"
ADMIN = "admin@example.com"
ADMIN_PW = "adminpw123"
USER = "worker@example.com"
USER_PW = "workerpw123"


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    """隔离所有权威状态位置，避免读到这台机器上的真实 journal/登记文件。"""
    monkeypatch.setenv("YIKOU_DATA_DIR", str(tmp_path / "userdata"))
    monkeypatch.setenv("YIKOU_SSS_LOCK_ROOT", str(tmp_path / "locks"))
    monkeypatch.setenv("YIKOU_SSS_AUTHORITATIVE_ROOT", str(tmp_path / "authority-root"))
    monkeypatch.setenv("YIKOU_SSS_AUTHORITATIVE_PATH",
                       str(tmp_path / "authority" / "sss_uncertain_authoritative.json"))
    monkeypatch.setenv("YIKOU_SSS_AUTHORITY_LOCATIONS",
                       str(tmp_path / "userdata" / "sss-authority-locations.json"))
    monkeypatch.delenv("YIKOU_SSS_UNCERTAIN_PATH", raising=False)
    # 只读核对复用 _safe_reconcile：关掉服务端预筛与轮询等待，保持离线且快。
    monkeypatch.setattr(sss_reconcile, "_SSS_SERVER_PREFILTER", False)
    monkeypatch.setattr(sss_reconcile, "_RECONCILE_POLL_INTERVAL_S", 0.0)
    monkeypatch.setattr(sss_runner, "_REVIEW_WIDE_WINDOW_DAYS", 3)


# ----------------------------------------------------------------------
# 合成数据与通用夹具
# ----------------------------------------------------------------------
def _journal_path() -> Path:
    return Path(os.environ["YIKOU_SSS_AUTHORITATIVE_PATH"])


def _today() -> str:
    return str(expected_delivery_date())


def _seed(records: list[dict]) -> None:
    target = _journal_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({"version": 1, "records": records},
                                 ensure_ascii=False), encoding="utf-8")


def _record(identifier: str, *, name: str = "张三", phone: str = "13800000001",
            door: str = "A101", day: str | None = None, account: str = ACCOUNT,
            status: str = "inflight", fingerprint: dict | None = None,
            batch_started_at: float | None = None) -> dict:
    day = day or _today()
    return {
        "journal_id": identifier,
        "identifier": identifier,
        "batch_key": day + "|" + account,
        "delivery_date": day,
        "account": account,
        "platform": "https://sssplusnew.zhuopaikeji.com",
        "fingerprint": fingerprint if fingerprint is not None else {
            "receive_name": name,
            "receive_phone": phone,
            "door_num": door,
            "expected_delivery_time": day + " 11:00:00",
            "account": account,
        },
        "status": status,
        "error": "提交前置记录：POST 即将发出",
        "created_at": day + "T07:00:00",
        "batch_started_at": time.time() if batch_started_at is None else batch_started_at,
    }


def _fp() -> OrderFingerprint:
    day = _today()
    return OrderFingerprint(
        receive_name="张三", receive_phone="13800000001", door_num="A101",
        expected_delivery_time=day + " 11:00:00", account=ACCOUNT,
        store_id="7", goods_name="轻食", goods_num="1",
        address_detail="武汉市洪山区某某路1号", area_code="420111",
        lnt="119.728224", lat="30.256632", order_type="1")


def _station_record(*, name: str = "张三", phone: str = "13800000001",
                    when: str | None = None, order_id: str = "S1",
                    door: str = "A101", status: int = 2,
                    address: str | None = None) -> dict:
    when = when or (_today() + " 11:00:00")
    return {
        "id": order_id,
        "recipientName": name,
        "recipientPhone": [phone],
        "recipientAddress": address if address is not None
        else "武汉市洪山区某某路1号" + door,
        "expectedDeliveryTime": when,
        "status": status,
        "createTime": time.time(),
    }


def _bridge(tmp_path, *, is_admin: bool = True) -> Bridge:
    bridge = Bridge(config_path=str(tmp_path / "config.json"), is_admin=is_admin)
    bridge._config.sss_url = "https://sssplusnew.zhuopaikeji.com/takeout"
    bridge._config.sss_account = ACCOUNT
    bridge._config.sss_order_source = "wps"
    return bridge


def _snapshot(bridge: Bridge, classifications: dict[str, str], *,
              checked_at: str | None = None, fingerprint: str | None = None,
              wide_window_days: int = 3) -> None:
    """模拟一次成功只读核对留下的内存证据（真实流程由 run_sss_review_job 写入）。"""
    bridge._remember_sss_review({
        "journal_fingerprint": (fingerprint if fingerprint is not None
                                else sss_uncertain.journal_fingerprint(_journal_path())),
        "checked_at": checked_at or dt.datetime.now().isoformat(timespec="seconds"),
        "wide_window_days": wide_window_days,
        "classifications": dict(classifications),
        "record_ids": sorted(classifications),
        "counts": {},
    })


def _resolve_payload(record_ids, decision: str = "station_absent") -> dict:
    return {"decision": decision, "confirm": decision,
            "note": "逐条人工核对闪时送站内订单后确认", "record_ids": list(record_ids)}


class _RecordingClient:
    """记录每一次请求路径的伪客户端；任何非列表 GET 都会失败。"""

    instances: list["_RecordingClient"] = []
    orders: list[dict] = []
    post_calls: list[tuple[str, dict]] = []

    def __init__(self, *args, **kwargs) -> None:
        self.requests: list[str] = []
        self.logged_in = False
        self.closed = False
        _RecordingClient.instances.append(self)

    def fetch_captcha(self) -> bytes:
        return b"png"

    def login(self, code: str) -> None:
        self.logged_in = True

    def get_json(self, path: str) -> dict:
        self.requests.append(path)
        if not path.startswith(_ORDER_LIST_PATH):
            raise AssertionError("只读核对只允许 GET 列表，实际访问 " + path)
        return {"success": True,
                "result": {"records": list(_RecordingClient.orders),
                           "total": len(_RecordingClient.orders)}}

    def post_json(self, path: str, payload: dict) -> dict:
        _RecordingClient.post_calls.append((path, payload))
        raise AssertionError("只读核对/解除入口不允许 POST：" + path)

    def fork(self):
        return self

    def close(self) -> None:
        self.closed = True


def _reset_recording(*, orders: list[dict]) -> None:
    _RecordingClient.instances = []
    _RecordingClient.orders = list(orders)
    _RecordingClient.post_calls = []


def _run_review(monkeypatch, tmp_path, *, orders: list[dict],
                bridge: Bridge | None = None, sink=None):
    _reset_recording(orders=orders)
    monkeypatch.setattr(sss_runner, "SssApiClient", _RecordingClient)
    bridge = bridge or _bridge(tmp_path)
    result = sss_runner.run_sss_review_job(
        bridge._config, threading.Event(), lambda message: None,
        password="pw", captcha_callback=lambda image: "1234",
        snapshot_sink=sink if sink is not None else bridge._remember_sss_review)
    return bridge, result


class _SubmissionClient:
    """完整下单流程用的伪客户端：GET 列表/余额，POST 会记录并模拟落单。"""

    instances: list["_SubmissionClient"] = []
    post_calls: list[tuple[str, dict]] = []
    list_gets: list[str] = []
    initial_orders: list[dict] = []
    after_post: list[dict] = []

    def __init__(self, *args, **kwargs) -> None:
        self.orders = list(_SubmissionClient.initial_orders)
        self.logged_in = False
        _SubmissionClient.instances.append(self)

    def fetch_captcha(self) -> bytes:
        return b"png"

    def login(self, code: str) -> None:
        self.logged_in = True

    def get_json(self, path: str) -> dict:
        if path.startswith(_ORDER_LIST_PATH):
            _SubmissionClient.list_gets.append(path)
            return {"success": True,
                    "result": {"records": list(self.orders), "total": len(self.orders)}}
        if "get-login-user-account" in path:
            return {"success": True,
                    "result": {"totalAmount": 1000.0, "freezeAmount": 0.0}}
        raise AssertionError("下单流程不允许访问 " + path)

    def post_json(self, path: str, payload: dict) -> dict:
        _SubmissionClient.post_calls.append((path, payload))
        self.orders = list(_SubmissionClient.after_post)
        return {"success": True}

    def fork(self):
        return self

    def close(self) -> None:
        pass


def _install_submission(monkeypatch, tmp_path, *, orders: list[dict],
                        after_post: list[dict]) -> Path:
    excel = tmp_path / "sss.xlsx"
    excel.write_bytes(b"placeholder")
    monkeypatch.setattr(sss_runner, "load_sss_orders", lambda *args, **kwargs: {
        "午餐": [{"row": 3, "name": "张三", "door": "A101",
                  "phone": "13800000001"}]})
    monkeypatch.setattr(sss_runner, "_collect_tasks", lambda *args, **kwargs: [{
        "sheet": "午餐", "identifier": "第 3 行 张三", "payload": {},
        "fingerprint": _fp(), "account": ACCOUNT, "batch_id": "batch-1",
        "client_request_id": "cr-new"}])
    _SubmissionClient.instances = []
    _SubmissionClient.post_calls = []
    _SubmissionClient.list_gets = []
    _SubmissionClient.initial_orders = list(orders)
    _SubmissionClient.after_post = list(after_post)
    monkeypatch.setattr(sss_runner, "SssApiClient", _SubmissionClient)
    return excel


def _submission_bridge(tmp_path, excel: Path) -> Bridge:
    bridge = _bridge(tmp_path)
    bridge._config.sss_order_source = "excel"
    bridge._config.sss_excel_path = str(excel)
    bridge._config.sss_dry_run = False
    bridge._config.sss_use_fixed_address = True
    bridge._config.sss_store_name = "一口轻食"
    bridge._config.sss_store_id = 7
    bridge._config.sss_store_name_cached = "一口轻食"
    bridge._config.sss_unit_price = 0.0
    bridge._config.sss_max_workers = 1
    return bridge


def _run_sss_job(bridge: Bridge) -> dict:
    return sss_runner.run_sss_job(bridge._config, threading.Event(),
                                  lambda message: None, password="pw",
                                  captcha_callback=lambda image: "1234")


def _install_network_tripwire(monkeypatch) -> None:
    """任何真实的闪时送客户端构造/请求都会让测试失败。"""
    class _Boom:
        def __init__(self, *args, **kwargs):
            raise AssertionError("该入口不允许构造网络客户端")

    def _boom(*args, **kwargs):
        raise AssertionError("该入口不允许发起网络请求")

    monkeypatch.setattr(sss_runner, "SssApiClient", _Boom)
    monkeypatch.setattr(SssApiClient, "__init__", _Boom.__init__)
    monkeypatch.setattr(SssApiClient, "get_json", _boom)
    monkeypatch.setattr(SssApiClient, "post_json", _boom)


# ======================================================================
# 不变量 1：零 POST
# ======================================================================
def test_review_job_only_lists_and_never_posts(tmp_path, monkeypatch):
    _seed([_record("cr-1"), _record("cr-2")])
    bridge, result = _run_review(monkeypatch, tmp_path, orders=[])

    client = _RecordingClient.instances[-1]
    assert client.logged_in is True
    assert client.requests, "只读核对必须真的查了站内列表"
    assert all(path.startswith(_ORDER_LIST_PATH) for path in client.requests)
    assert _RecordingClient.post_calls == []
    assert result["post_sent"] is False
    assert result["summary"]["post_sent"] is False
    assert result["status"] == "review_blocked"
    assert result["review"]["counts"]["station_missing"] == 2
    assert "张三" not in json.dumps(result, ensure_ascii=False)
    assert "13800000001" not in json.dumps(result, ensure_ascii=False)


def test_review_job_station_hit_still_zero_post(tmp_path, monkeypatch):
    _seed([_record("cr-1")])
    bridge, result = _run_review(monkeypatch, tmp_path,
                                 orders=[_station_record(order_id="S1")])

    assert result["status"] == "review_ok"
    assert result["post_sent"] is False
    assert _RecordingClient.post_calls == []
    records = sss_uncertain.load_journal(_journal_path())["records"]
    assert [record["status"] for record in records] == ["resolved"]


def test_records_and_resolve_paths_never_touch_network(tmp_path, monkeypatch):
    _seed([_record("cr-1"), _record("cr-2")])
    bridge = _bridge(tmp_path)
    _install_network_tripwire(monkeypatch)

    state = bridge.sss_uncertain_records()
    assert state["ok"] is True and state["read_only"] is True

    _snapshot(bridge, {"cr-1": "station_missing", "cr-2": "station_missing"})
    out = bridge.sss_uncertain_resolve(_resolve_payload(["cr-1", "cr-2"]))
    assert out["ok"] is True and out["post_sent"] is False
    assert out["cloud_write"] is False

    out2 = bridge.sss_uncertain_resolve(_resolve_payload(["cr-1"], decision="station_present"))
    assert out2["code"] == "unknown_record_ids", "已解除的记录不应还能再写"


# ======================================================================
# 不变量 2：station_absent 证据门禁
# ======================================================================
def test_station_absent_requires_review_snapshot(tmp_path, monkeypatch):
    _seed([_record("cr-1")])
    bridge = _bridge(tmp_path)
    _install_network_tripwire(monkeypatch)
    before = _journal_path().read_bytes()

    out = bridge.sss_uncertain_resolve(_resolve_payload(["cr-1"]))
    assert out["ok"] is False and out["code"] == "review_required"
    assert out["changed"] is False and out["post_sent"] is False
    assert _journal_path().read_bytes() == before


def test_station_absent_rejects_stale_or_unparseable_evidence(tmp_path, monkeypatch):
    _seed([_record("cr-1")])
    bridge = _bridge(tmp_path)
    _install_network_tripwire(monkeypatch)
    before = _journal_path().read_bytes()

    stale = (dt.datetime.now() - dt.timedelta(seconds=601)).isoformat(timespec="seconds")
    _snapshot(bridge, {"cr-1": "station_missing"}, checked_at=stale)
    assert bridge.sss_uncertain_resolve(_resolve_payload(["cr-1"]))["code"] == "review_stale"

    _snapshot(bridge, {"cr-1": "station_missing"}, checked_at="not-a-timestamp")
    assert bridge.sss_uncertain_resolve(_resolve_payload(["cr-1"]))["code"] == "review_stale"

    future = (dt.datetime.now() + dt.timedelta(seconds=120)).isoformat(timespec="seconds")
    _snapshot(bridge, {"cr-1": "station_missing"}, checked_at=future)
    assert bridge.sss_uncertain_resolve(_resolve_payload(["cr-1"]))["code"] == "review_stale"
    assert _journal_path().read_bytes() == before, "被拒绝的解除不得写文件"

    # 边界：600 秒内仍可用（证明确实是 TTL 判定，不是一律拒绝）。
    fresh = (dt.datetime.now() - dt.timedelta(seconds=30)).isoformat(timespec="seconds")
    _snapshot(bridge, {"cr-1": "station_missing"}, checked_at=fresh)
    assert bridge.sss_uncertain_resolve(_resolve_payload(["cr-1"]))["ok"] is True
    assert _journal_path().read_bytes() != before


def test_station_absent_rejects_journal_changed_after_review(tmp_path, monkeypatch):
    _seed([_record("cr-1")])
    bridge, result = _run_review(monkeypatch, tmp_path, orders=[])
    assert result["status"] == "review_blocked"
    assert bridge.sss_uncertain_records()["review"]["journal_matches"] is True

    _seed([_record("cr-1"), _record("cr-2")])  # 核对之后 journal 变了
    view = bridge.sss_uncertain_records()["review"]
    assert view["journal_matches"] is False

    before = _journal_path().read_bytes()
    out = bridge.sss_uncertain_resolve(_resolve_payload(["cr-1"]))
    assert out["code"] == "journal_changed"
    assert _journal_path().read_bytes() == before


def test_station_absent_rejects_incomplete_or_failed_classification(tmp_path, monkeypatch):
    _seed([_record("cr-1"), _record("cr-2")])
    bridge = _bridge(tmp_path)
    _install_network_tripwire(monkeypatch)
    before = _journal_path().read_bytes()
    payload = _resolve_payload(["cr-1", "cr-2"])

    _snapshot(bridge, {"cr-1": "station_missing"})
    assert bridge.sss_uncertain_resolve(payload)["code"] == "review_required"

    _snapshot(bridge, {"cr-1": "station_missing", "cr-2": "scan_failed"})
    assert bridge.sss_uncertain_resolve(payload)["code"] == "review_required"

    _snapshot(bridge, {"cr-1": "station_missing", "cr-2": "station_present"})
    assert bridge.sss_uncertain_resolve(payload)["code"] == "review_required"

    _snapshot(bridge, {"cr-1": "station_missing", "cr-2": "station_found_other_day"})
    assert bridge.sss_uncertain_resolve(payload)["code"] == "station_state_changed"
    assert _journal_path().read_bytes() == before, "被拒绝的解除不得写文件"

    # 只勾选被覆盖的那一条也不行：另一条仍是活跃未决，不能整批放开。
    _snapshot(bridge, {"cr-1": "station_missing"})
    assert bridge.sss_uncertain_resolve(_resolve_payload(["cr-1"]))["ok"] is True
    assert _journal_path().read_bytes() != before


# ======================================================================
# 不变量 3：权限
# ======================================================================
def test_direct_non_admin_records_and_resolve_forbidden(tmp_path, monkeypatch):
    _seed([_record("cr-1")])
    bridge = _bridge(tmp_path, is_admin=False)
    _install_network_tripwire(monkeypatch)
    before = _journal_path().read_bytes()

    state = bridge.sss_uncertain_records()
    assert state["ok"] is False and state["status"] == "forbidden"
    assert state["code"] == "forbidden" and state["records"] == []

    _snapshot(bridge, {"cr-1": "station_missing"})
    for decision in ("station_absent", "station_present", "keep"):
        out = bridge.sss_uncertain_resolve(_resolve_payload(["cr-1"], decision=decision))
        assert out["ok"] is False, decision
        assert out["status"] == "forbidden" and out["code"] == "forbidden"
        assert out["changed"] is False and out["post_sent"] is False
    assert _journal_path().read_bytes() == before
    assert sss_uncertain.pending_records(
        sss_uncertain.load_journal(_journal_path())["records"],
        _today() + "|" + ACCOUNT), "非管理员不得改变记录状态"


def test_resolve_rejects_records_outside_current_batch(tmp_path, monkeypatch):
    _seed([_record("cr-1"), _record("cr-other", account="other-account")])
    bridge = _bridge(tmp_path)
    _install_network_tripwire(monkeypatch)
    before = _journal_path().read_bytes()
    _snapshot(bridge, {"cr-1": "station_missing", "cr-other": "station_missing"})

    out = bridge.sss_uncertain_resolve(_resolve_payload(["cr-1", "cr-other"]))
    assert out["ok"] is False and out["code"] == "unknown_record_ids"
    assert out["unknown_record_ids"] == ["cr-other"]
    assert out["changed"] is False
    assert _journal_path().read_bytes() == before


@pytest.mark.xfail(strict=False, reason=(
    "DEFECT(bridge.py:933): 直调 Bridge(is_admin=False).start_sss_review 未做 "
    "is_admin 检查，返回 ok=true 并启动只读 worker；HTTP 层已 403 admin_only，"
    "影响仅限同进程调用（契约/文档不一致，非远程绕过）"))
def test_direct_non_admin_start_sss_review_must_be_forbidden(tmp_path, monkeypatch):
    _seed([_record("cr-1")])
    bridge = _bridge(tmp_path, is_admin=False)
    before = _journal_path().read_bytes()
    launched: list[int] = []

    def _stub(config, stop_event, *args, **kwargs):
        launched.append(1)
        return {"status": "review_ok", "review": {"counts": {}}}

    monkeypatch.setattr(bridge_module, "run_sss_review_job", _stub)
    out = bridge.start_sss_review({"password": "pw"})

    deadline = time.monotonic() + 5
    while bridge.worker_alive() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert out["ok"] is False and out["status"] == "forbidden"
    assert _journal_path().read_bytes() == before
    assert launched == []


def _token(server, username: str, password: str) -> str:
    port = server.server_address[1]
    data = urllib.parse.urlencode(
        {"action": "login", "username": username, "password": password}).encode("utf-8")
    request = urllib.request.Request(f"http://127.0.0.1:{port}/login",
                                     data=data, method="POST")
    request.add_header("Content-Type", "application/x-www-form-urlencoded")

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None

    try:
        with urllib.request.build_opener(_NoRedirect).open(request, timeout=15) as response:
            raw = response.headers.get("Set-Cookie", "")
    except urllib.error.HTTPError as exc:
        raw = exc.headers.get("Set-Cookie", "")
    for part in raw.split(";"):
        name, sep, value = part.strip().partition("=")
        if sep and name == SESSION_COOKIE:
            return value
    raise AssertionError("未取到会话令牌：" + repr(raw))


def _api(server, cookie: str, method: str, payload: dict | None = None):
    port = server.server_address[1]
    body = json.dumps([] if payload is None else [payload]).encode("utf-8")
    request = urllib.request.Request(f"http://127.0.0.1:{port}/api/{method}",
                                     data=body, method="POST",
                                     headers={"Content-Type": "application/json",
                                              "Cookie": f"{SESSION_COOKIE}={cookie}"})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


@pytest.fixture()
def web_server(tmp_path):
    store = AuthStore(tmp_path / "cfg")
    store.create_admin(ADMIN, ADMIN_PW)
    store.register(USER, USER_PW)
    store.approve(USER, by=ADMIN)
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html><title>app</title>",
                                     encoding="utf-8")
    httpd = create_server("127.0.0.1", 0, dist_dir=dist, token="legacy",
                          config_path=tmp_path / "config.json", auth=store)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield httpd
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize("method", ["sss_uncertain_records", "start_sss_review",
                                    "sss_uncertain_resolve"])
def test_http_non_admin_gets_403_admin_only(web_server, method):
    _seed([_record("cr-1")])
    before = _journal_path().read_bytes()
    cookie = _token(web_server, USER, USER_PW)
    status, body = _api(web_server, cookie, method, {})
    assert status == 403
    assert body["code"] == "admin_only"
    assert body.get("ok") is not True
    assert _journal_path().read_bytes() == before


@pytest.mark.parametrize("method,payload", [
    ("sss_uncertain_records", None),
    ("start_sss_review", {}),
    ("sss_uncertain_resolve", {}),
])
def test_http_admin_is_not_blocked_by_whitelist(web_server, method, payload):
    cookie = _token(web_server, ADMIN, ADMIN_PW)
    status, body = _api(web_server, cookie, method, payload)
    assert status == 200
    assert body.get("code") != "admin_only"


# ======================================================================
# 不变量 4：station_present 只标 resolved + 审计
# ======================================================================
def test_station_present_resolves_subset_with_audit(tmp_path, monkeypatch):
    _seed([_record("cr-1"), _record("cr-2")])
    bridge = _bridge(tmp_path)
    _install_network_tripwire(monkeypatch)

    out = bridge.sss_uncertain_resolve(_resolve_payload(["cr-1"],
                                                        decision="station_present"))
    assert out["ok"] is True and out["status"] == "resolved"
    assert out["changed"] is True and out["affected"] == 1 and out["remaining"] == 1
    assert out["post_sent"] is False and out["cloud_write"] is False
    assert out["decision"] == "station_present"
    assert out["audit"]["actor"] == "local-admin"
    assert out["audit"]["note"].startswith("逐条人工核对")

    records = sss_uncertain.load_journal(_journal_path())["records"]
    by_id = {record["journal_id"]: record for record in records}
    assert by_id["cr-1"]["status"] == "resolved"
    assert by_id["cr-1"]["resolved_reason"] == out["audit"]["note"]
    assert by_id["cr-1"]["resolved_by"] == "local-admin"
    assert by_id["cr-1"]["resolved_at"]
    assert by_id["cr-2"]["status"] == "inflight"
    pending = sss_uncertain.pending_records(records, _today() + "|" + ACCOUNT)
    assert [record["journal_id"] for record in pending] == ["cr-2"]


# ======================================================================
# 不变量 5：宽窗不改变严格语义
# ======================================================================
def _task(identifier: str, *, name: str = "张三", phone: str = "13800000001",
          day: str | None = None) -> dict:
    day = day or _today()
    return {
        "identifier": identifier,
        "payload": {},
        "account": ACCOUNT,
        "fingerprint": OrderFingerprint(
            receive_name=name, receive_phone=phone, door_num="A101",
            expected_delivery_time=day + " 11:00:00", account=ACCOUNT),
    }


def _fetch_with(records: list[dict]):
    seen: list[str] = []

    def _fetch(path: str) -> dict:
        seen.append(path)
        return {"success": True,
                "result": {"records": list(records), "total": len(records)}}

    return _fetch, seen


def test_expand_days_zero_and_negative_are_identity():
    days = {"2026-09-22", "", "bad-day"}
    assert sss_reconcile._expand_days(days, 0) == days
    assert sss_reconcile._expand_days(days, -5) == days
    assert sss_reconcile._expand_days({"2026-09-22"}, 1) == {
        "2026-09-21", "2026-09-22", "2026-09-23"}
    assert sss_reconcile._expand_days({"bad-day"}, 3) == {"bad-day"}


def test_days_margin_zero_is_byte_identical_to_default():
    day = _today()
    tasks = [_task("t1", day=day), _task("t2", name="李四", phone="13900000002", day=day)]
    records = [_station_record(order_id="S1"),
               _station_record(name="李四", phone="13900000002", order_id="S2")]
    fetch_legacy, seen_legacy = _fetch_with(records)
    fetch_zero, seen_zero = _fetch_with(records)
    legacy = sss_reconcile._reconcile_tasks(tasks, fetch_legacy)
    explicit = sss_reconcile._reconcile_tasks(tasks, fetch_zero, days_margin=0)
    assert legacy == explicit
    assert legacy.confirmed == {"t1", "t2"}
    assert seen_legacy == seen_zero

    fetch_a, seen_a = _fetch_with(records)
    fetch_b, seen_b = _fetch_with(records)
    sss_reconcile._list_pending_orders(fetch_a, tasks)
    sss_reconcile._list_pending_orders(fetch_b, tasks, days_margin=0)
    assert seen_a == seen_b


def test_wide_margin_never_confirms_different_appointment_time():
    day = _today()
    other = (dt.date.fromisoformat(day) + dt.timedelta(days=1)).isoformat()
    tasks = [_task("t1", day=day)]

    fetch_other, _ = _fetch_with([_station_record(when=other + " 11:00:00",
                                                  order_id="S-other")])
    strict = sss_reconcile._reconcile_tasks(tasks, fetch_other, days_margin=3)
    assert strict.confirmed == set()
    assert strict.matched_count == 0
    assert [item["identifier"] for item in strict.missing] == ["t1"]

    fetch_same, _ = _fetch_with([_station_record(when=day + " 11:00:00",
                                                 order_id="S-same")])
    same = sss_reconcile._reconcile_tasks(tasks, fetch_same, days_margin=3)
    assert same.confirmed == {"t1"} and same.missing == []


def test_list_pending_orders_wide_window_boundary():
    day = _today()
    base = dt.date.fromisoformat(day)
    tasks = [_task("t1", day=day)]
    records = [
        _station_record(when=(base + dt.timedelta(days=offset)).isoformat() + " 11:00:00",
                        order_id=f"S{offset}")
        for offset in (-4, -3, 0, 3, 4)
    ]
    fetch_zero, _ = _fetch_with(records)
    out_zero = sss_reconcile._list_pending_orders(fetch_zero, tasks)
    assert {record["id"] for record in out_zero} == {"S0"}

    fetch_wide, _ = _fetch_with(records)
    out_wide = sss_reconcile._list_pending_orders(fetch_wide, tasks, days_margin=3)
    assert {record["id"] for record in out_wide} == {"S-3", "S0", "S3"}


def test_person_scan_only_finds_other_day_when_window_widened():
    day = _today()
    other = (dt.date.fromisoformat(day) + dt.timedelta(days=1)).isoformat()
    tasks = [_task("t1", day=day)]
    fetch, _ = _fetch_with([_station_record(when=other + " 11:00:00",
                                            order_id="S-other")])
    assert sss_reconcile._reconcile_person_matches(tasks, fetch) == {}
    wide = sss_reconcile._reconcile_person_matches(tasks, fetch, days_margin=3)
    assert list(wide) == ["t1"]
    assert wide["t1"][0]["order_id"] == "S-other"
    assert wide["t1"][0]["delivery_time"] == other + " 11:00:00"


# ======================================================================
# 不变量 6：端到端（解除后重发 / 站内命中不重发）
# ======================================================================
def test_gate_blocks_then_admin_unblocks_then_exactly_one_post(tmp_path, monkeypatch):
    _seed([_record("cr-1", fingerprint=_fp().as_dict())])
    excel = _install_submission(monkeypatch, tmp_path, orders=[],
                                after_post=[_station_record(order_id="S1")])
    bridge = _submission_bridge(tmp_path, excel)

    # 1) 解除之前：闸门必须阻断，零 POST。
    blocked = _run_sss_job(bridge)
    assert blocked["status"] == "blocked_uncertain"
    assert _SubmissionClient.post_calls == []
    assert sss_uncertain.pending_records(
        sss_uncertain.load_journal(_journal_path())["records"],
        _today() + "|" + ACCOUNT)

    # 2) 只读核对（真实 worker）生成证据，仍然零 POST。
    review = sss_runner.run_sss_review_job(
        bridge._config, threading.Event(), lambda message: None, password="pw",
        captcha_callback=lambda image: "1234",
        snapshot_sink=bridge._remember_sss_review)
    assert review["status"] == "review_blocked"
    assert review["review"]["counts"]["station_missing"] == 1
    assert review["review"]["counts"]["scan_failed"] == 0
    assert _SubmissionClient.post_calls == []
    view = bridge.sss_uncertain_records()["review"]
    assert view["available"] is True and view["journal_matches"] is True
    assert view["covers_active"] is True
    assert view["classifications"] == {"cr-1": "station_missing"}

    # 3) 管理员带审计解除：只写本地 journal，零 POST。
    out = bridge.sss_uncertain_resolve(_resolve_payload(["cr-1"]))
    assert out["ok"] is True and out["status"] == "discarded"
    assert out["post_sent"] is False
    assert _SubmissionClient.post_calls == []
    assert sss_uncertain.pending_records(
        sss_uncertain.load_journal(_journal_path())["records"],
        _today() + "|" + ACCOUNT) == []

    # 4) 重跑：本批必须重新 POST 且只发一次，最后被站内对账确认。
    final = _run_sss_job(bridge)
    assert [path for path, _ in _SubmissionClient.post_calls] == [_CREATE_ORDER_PATH]
    assert final["status"] == "confirmed"
    assert final["created"] == 1
    records = sss_uncertain.load_journal(_journal_path())["records"]
    statuses = sorted(record["status"] for record in records)
    assert statuses == ["discarded", "resolved"]
    assert sss_uncertain.pending_records(records, _today() + "|" + ACCOUNT) == []


def test_station_present_then_run_does_not_post_when_on_station(tmp_path, monkeypatch):
    _seed([_record("cr-1", fingerprint=_fp().as_dict())])
    excel = _install_submission(monkeypatch, tmp_path,
                                orders=[_station_record(order_id="S1")],
                                after_post=[_station_record(order_id="S1")])
    bridge = _submission_bridge(tmp_path, excel)

    out = bridge.sss_uncertain_resolve(_resolve_payload(["cr-1"],
                                                        decision="station_present"))
    assert out["ok"] is True and out["status"] == "resolved"
    assert _SubmissionClient.post_calls == []

    result = _run_sss_job(bridge)
    assert _SubmissionClient.post_calls == [], "站内命中时不得 POST"
    assert result["status"] == "confirmed" and result["created"] == 1


def test_pre_submit_readonly_reconcile_blocks_post_when_on_station(tmp_path, monkeypatch):
    _seed([_record("cr-1", fingerprint=_fp().as_dict())])
    excel = _install_submission(monkeypatch, tmp_path,
                                orders=[_station_record(order_id="S1")],
                                after_post=[_station_record(order_id="S1")])
    bridge = _submission_bridge(tmp_path, excel)

    result = _run_sss_job(bridge)

    assert _SubmissionClient.post_calls == [], "站内已命中时不得 POST"
    assert result["status"] == "confirmed" and result["created"] == 1
    assert any(path.startswith(_ORDER_LIST_PATH) for path in _SubmissionClient.list_gets)
    records = sss_uncertain.load_journal(_journal_path())["records"]
    assert [record["status"] for record in records] == ["resolved"]
    assert records[0]["resolved_reason"]


# ======================================================================
# 不变量 7：fail-closed
# ======================================================================
def test_corrupt_journal_fails_closed_everywhere(tmp_path, monkeypatch):
    target = _journal_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("{ not json", encoding="utf-8")
    before = target.read_bytes()
    bridge = _bridge(tmp_path)

    with pytest.raises(sss_uncertain.UncertainJournalError):
        sss_uncertain.pending_record_views(target)

    state = bridge.sss_uncertain_records()
    assert state["ok"] is False and state["code"] == "journal_unreadable"

    out = bridge.sss_uncertain_resolve(_resolve_payload(["cr-1"]))
    assert out["ok"] is False and out["changed"] is False

    # journal 已损坏，快照只能伪造指纹；门禁必须仍 fail-closed。
    _snapshot(bridge, {"cr-1": "station_missing"}, fingerprint="deadbeefdeadbeef")
    out2 = bridge.sss_uncertain_resolve(_resolve_payload(["cr-1"]))
    assert out2["ok"] is False and out2["changed"] is False

    _reset_recording(orders=[])
    monkeypatch.setattr(sss_runner, "SssApiClient", _RecordingClient)
    review = sss_runner.run_sss_review_job(
        bridge._config, threading.Event(), lambda message: None, password="pw",
        captcha_callback=lambda image: "1234",
        snapshot_sink=bridge._remember_sss_review)
    assert review["status"] == "review_failed"
    assert _RecordingClient.post_calls == []
    assert target.read_bytes() == before


def test_unreadable_journal_path_is_directory_fails_closed(tmp_path, monkeypatch):
    target = _journal_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.mkdir()
    bridge = _bridge(tmp_path)

    state = bridge.sss_uncertain_records()
    assert state["ok"] is False and state["code"] == "journal_unreadable"
    out = bridge.sss_uncertain_resolve(_resolve_payload(["cr-1"]))
    assert out["ok"] is False and out["changed"] is False

    _reset_recording(orders=[])
    monkeypatch.setattr(sss_runner, "SssApiClient", _RecordingClient)
    review = sss_runner.run_sss_review_job(
        bridge._config, threading.Event(), lambda message: None, password="pw",
        captcha_callback=lambda image: "1234",
        snapshot_sink=bridge._remember_sss_review)
    assert review["status"] == "review_failed"
    assert _RecordingClient.post_calls == []


def test_wide_window_scan_failure_is_not_missing(tmp_path, monkeypatch):
    _seed([_record("cr-1"), _record("cr-2")])
    bridge = _bridge(tmp_path)
    _reset_recording(orders=[])
    monkeypatch.setattr(sss_runner, "SssApiClient", _RecordingClient)

    def _boom(*args, **kwargs):
        raise RuntimeError("宽窗列表查询 500")

    monkeypatch.setattr(sss_runner, "_reconcile_person_matches", _boom)
    before = _journal_path().read_bytes()
    result = sss_runner.run_sss_review_job(
        bridge._config, threading.Event(), lambda message: None, password="pw",
        captcha_callback=lambda image: "1234",
        snapshot_sink=bridge._remember_sss_review)

    counts = result["review"]["counts"]
    assert counts["scan_failed"] == 2
    assert counts["station_missing"] == 0
    assert result["status"] == "review_failed"
    assert _RecordingClient.post_calls == []
    # 扫描失败不产生证据，解除入口必须拒绝。
    assert bridge.sss_uncertain_records()["review"]["available"] is False
    out = bridge.sss_uncertain_resolve(_resolve_payload(["cr-1", "cr-2"]))
    assert out["ok"] is False and out["code"] == "review_required"
    assert _journal_path().read_bytes() == before


def test_partial_discard_write_is_not_success(tmp_path, monkeypatch):
    _seed([_record("cr-1")])
    bridge = _bridge(tmp_path)
    before = _journal_path().read_bytes()
    _snapshot(bridge, {"cr-1": "station_missing"})
    monkeypatch.setattr(bridge_module, "discard_uncertain_records",
                        lambda *args, **kwargs: 0)

    out = bridge.sss_uncertain_resolve(_resolve_payload(["cr-1"]))
    assert out["ok"] is False and out["code"] == "journal_write_failed"
    assert out["status"] == "error" and out["changed"] is False
    assert out["post_sent"] is False
    assert _journal_path().read_bytes() == before


def test_journal_write_exception_is_not_success(tmp_path, monkeypatch):
    _seed([_record("cr-1")])
    bridge = _bridge(tmp_path)
    before = _journal_path().read_bytes()
    _snapshot(bridge, {"cr-1": "station_missing"})

    def _raise(*args, **kwargs):
        raise sss_uncertain.UncertainJournalError("磁盘写入失败（模拟）")

    monkeypatch.setattr(sss_uncertain, "_atomic_write", _raise)
    out = bridge.sss_uncertain_resolve(_resolve_payload(["cr-1"]))
    assert out["ok"] is False and out["changed"] is False
    assert out["code"] in ("journal_unreadable", "journal_write_failed")
    assert _journal_path().read_bytes() == before


def test_partial_resolve_write_is_not_success(tmp_path, monkeypatch):
    _seed([_record("cr-1")])
    bridge = _bridge(tmp_path)
    before = _journal_path().read_bytes()
    monkeypatch.setattr(bridge_module, "resolve_uncertain_records",
                        lambda *args, **kwargs: 0)

    out = bridge.sss_uncertain_resolve(_resolve_payload(["cr-1"],
                                                        decision="station_present"))
    assert out["ok"] is False and out["code"] == "journal_write_failed"
    assert out["changed"] is False and out["post_sent"] is False
    assert _journal_path().read_bytes() == before
