"""闪时送未决记录只读核对与管理员解除入口的离线回归。

覆盖：脱敏只读投影、宽窗复核默认等价、解除入口的完整拒绝矩阵（非管理员/确认串/
备注/未知 id/无证据/证据过期/journal 变更/宽窗命中/覆盖不全）、station_absent 与
station_present 的正常路径与审计留痕，以及“只读核对与解除都零 POST”。
全部使用合成任务与伪客户端，不发真实请求。
"""
from __future__ import annotations

import datetime as dt
import json
import os
import time
from pathlib import Path

import pytest

from app.api.bridge import Bridge
from app.ordering import reconcile as sss_reconcile
from app.ordering import runner as sss_runner
from app.ordering import uncertain as sss_uncertain
from app.ordering.sss import expected_delivery_date


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    monkeypatch.setenv("YIKOU_DATA_DIR", str(tmp_path / "userdata"))
    monkeypatch.setenv("YIKOU_SSS_LOCK_ROOT", str(tmp_path / "locks"))
    monkeypatch.setenv("YIKOU_SSS_AUTHORITATIVE_PATH",
                       str(tmp_path / "authority" / "sss_uncertain_authoritative.json"))
    monkeypatch.delenv("YIKOU_SSS_UNCERTAIN_PATH", raising=False)
    # 只读核对内部复用 _safe_reconcile：关掉服务端预筛与轮询等待，保持离线且快。
    monkeypatch.setattr(sss_reconcile, "_SSS_SERVER_PREFILTER", False)
    monkeypatch.setattr(sss_reconcile, "_RECONCILE_POLL_INTERVAL_S", 0.0)
    monkeypatch.setattr(sss_runner, "_REVIEW_WIDE_WINDOW_DAYS", 3)


ACCOUNT = "18758187837"


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
            status: str = "inflight", error: str = "提交前置记录：POST 即将发出") -> dict:
    day = day or _today()
    return {
        "journal_id": identifier,
        "identifier": identifier,
        "batch_key": day + "|" + account,
        "delivery_date": day,
        "account": account,
        "platform": "https://sssplusnew.zhuopaikeji.com",
        "fingerprint": {
            "receive_name": name,
            "receive_phone": phone,
            "door_num": door,
            "expected_delivery_time": day + " 11:00:00",
            "account": account,
        },
        "status": status,
        "error": error,
        "created_at": day + "T07:00:00",
        "batch_started_at": time.time(),
    }


def _bridge(tmp_path, *, is_admin: bool = True) -> Bridge:
    bridge = Bridge(config_path=str(tmp_path / "config.json"), is_admin=is_admin)
    # 请求身份是 ContextVar：其它测试（如 test_independent_final_counterexamples）
    # 会在同线程里 set 而不清，这里显式清空，保证审计里的 actor 可预期。
    bridge.set_request_identity(None)
    bridge._config.sss_url = "https://sssplusnew.zhuopaikeji.com/takeout"
    bridge._config.sss_account = ACCOUNT
    bridge._config.sss_order_source = "wps"
    return bridge


def _snapshot(bridge: Bridge, *, classifications: dict[str, str],
              checked_at: str | None = None, fingerprint: str | None = None,
              wide_window_days: int = 3) -> None:
    """模拟一次成功的只读核对留下的内存证据（写入口只认它）。"""
    bridge._remember_sss_review({
        "journal_fingerprint": (fingerprint if fingerprint is not None
                                else sss_uncertain.journal_fingerprint(_journal_path())),
        "checked_at": checked_at or dt.datetime.now().isoformat(timespec="seconds"),
        "wide_window_days": wide_window_days,
        "classifications": dict(classifications),
        "record_ids": sorted(classifications),
        "counts": {},
    })


class _FakeSssClient:
    """只实现只读核对需要的方法；任何写路径（POST）都没有对应方法。"""

    instances: list["_FakeSssClient"] = []
    orders: list[dict] = []

    def __init__(self, *args, **kwargs) -> None:
        self.requests: list[str] = []
        self.logged_in = False
        _FakeSssClient.instances.append(self)

    def fetch_captcha(self) -> bytes:
        return b"png"

    def login(self, code: str) -> None:
        self.logged_in = True

    def get_json(self, path: str) -> dict:
        self.requests.append(path)
        if "/one-touch-send/list" not in path:
            raise AssertionError("只读核对不允许访问 " + path)
        records = list(_FakeSssClient.orders)
        return {"success": True, "result": {"records": records, "total": len(records)}}

    def close(self) -> None:
        pass


def _station_record(name: str, phone: str, when: str, *, order_id: str,
                    door: str = "A101", status: int = 2) -> dict:
    return {
        "id": order_id,
        "recipientName": name,
        "recipientPhone": [phone],
        "recipientAddress": "武汉市洪山区某某路1号" + door,
        "expectedDeliveryTime": when,
        "status": status,
        "createTime": time.time(),
    }


# ----------------------------------------------------------------------
# 只读投影
# ----------------------------------------------------------------------
def test_records_projection_masks_pii_and_never_writes(tmp_path):
    _seed([
        _record("cr-1", name="张三", phone="13800000001"),
        _record("cr-2", name="李四", phone="13900000002", status="unresolved",
                error="ReadTimeout"),
        _record("cr-old", name="王五", phone="13700000003", status="resolved"),
        _record("cr-other", name="赵六", phone="13600000004", account="other-account"),
    ])
    before = _journal_path().read_bytes()
    bridge = _bridge(tmp_path)
    state = bridge.sss_uncertain_records()

    assert state["ok"] is True and state["read_only"] is True
    assert [record["journal_id"] for record in state["records"]] == ["cr-1", "cr-2"]
    assert state["counts"]["active"] == 2
    assert state["counts"]["inflight"] == 1 and state["counts"]["unresolved"] == 1
    assert state["counts"]["resolved"] == 1
    assert state["records"][0]["phone"] == "138****0001"
    assert state["records"][0]["name"] == "张三"
    assert state["records"][0]["door_num"] == "A101"
    assert state["account"] == "187****7837"
    assert state["review"]["available"] is False
    # 只读入口绝不允许改动权威 journal 的字节。
    assert _journal_path().read_bytes() == before


def test_records_requires_admin(tmp_path):
    _seed([_record("cr-1")])
    state = _bridge(tmp_path, is_admin=False).sss_uncertain_records()
    assert state["ok"] is False and state["code"] == "forbidden"
    assert state["records"] == []


def test_records_fail_closed_on_corrupt_journal(tmp_path):
    target = _journal_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("{ not json", encoding="utf-8")
    state = _bridge(tmp_path).sss_uncertain_records()
    assert state["ok"] is False and state["code"] == "journal_unreadable"


# ----------------------------------------------------------------------
# 解除入口：拒绝矩阵（每一种都必须不写文件）
# ----------------------------------------------------------------------
def test_resolve_rejects_bad_requests_without_writing(tmp_path):
    _seed([_record("cr-1"), _record("cr-2")])
    bridge = _bridge(tmp_path)
    before = _journal_path().read_bytes()

    def _call(**payload):
        payload.setdefault("record_ids", ["cr-1", "cr-2"])
        return bridge.sss_uncertain_resolve(payload)

    assert _call(decision="nope", confirm="nope", note="核对过了")["code"] == "invalid_payload"
    assert _call(decision="station_absent", confirm="station_absent",
                 note="短")["code"] == "note_required"
    assert _call(decision="station_absent", confirm="wrong",
                 note="人工核对完成")["code"] == "confirmation_required"
    assert bridge.sss_uncertain_resolve(
        {"decision": "station_present", "confirm": "station_present",
         "note": "人工核对完成", "record_ids": []})["code"] == "invalid_record_ids"
    assert _call(decision="station_present", confirm="station_present",
                 note="人工核对完成",
                 record_ids=["cr-1", "cr-不存在"])["code"] == "unknown_record_ids"
    assert _call(decision="station_absent", confirm="station_absent",
                 note="人工核对完成")["code"] == "review_required"
    assert _journal_path().read_bytes() == before


def test_resolve_requires_admin(tmp_path):
    _seed([_record("cr-1")])
    bridge = _bridge(tmp_path, is_admin=False)
    before = _journal_path().read_bytes()
    out = bridge.sss_uncertain_resolve({
        "decision": "station_absent", "confirm": "station_absent",
        "note": "人工核对完成", "record_ids": ["cr-1"]})
    assert out["ok"] is False and out["status"] == "forbidden"
    assert _journal_path().read_bytes() == before


def test_resolve_keep_changes_nothing(tmp_path):
    _seed([_record("cr-1")])
    before = _journal_path().read_bytes()
    out = _bridge(tmp_path).sss_uncertain_resolve({
        "decision": "keep", "confirm": "keep", "note": "先不处理",
        "record_ids": ["cr-1"]})
    assert out["ok"] is True and out["status"] == "kept" and out["changed"] is False
    assert _journal_path().read_bytes() == before


def test_resolve_station_absent_rejects_stale_or_changed_evidence(tmp_path):
    _seed([_record("cr-1")])
    bridge = _bridge(tmp_path)
    before = _journal_path().read_bytes()
    payload = {"decision": "station_absent", "confirm": "station_absent",
               "note": "人工核对完成", "record_ids": ["cr-1"]}

    # 证据过期：即使分类正确也不能解除。
    old = (dt.datetime.now() - dt.timedelta(seconds=3600)).isoformat(timespec="seconds")
    _snapshot(bridge, classifications={"cr-1": "station_missing"}, checked_at=old)
    assert bridge.sss_uncertain_resolve(payload)["code"] == "review_stale"

    # journal 在核对之后变化：指纹对不上。
    _snapshot(bridge, classifications={"cr-1": "station_missing"},
              fingerprint="deadbeefdeadbeef")
    assert bridge.sss_uncertain_resolve(payload)["code"] == "journal_changed"

    # 宽窗内命中（送达日不同）：必须改走 station_present，不允许当缺失处理。
    _snapshot(bridge, classifications={"cr-1": "station_found_other_day"})
    assert bridge.sss_uncertain_resolve(payload)["code"] == "station_state_changed"

    # 扫描失败/覆盖不全：同样不允许解除。
    _snapshot(bridge, classifications={"cr-1": "scan_failed"})
    assert bridge.sss_uncertain_resolve(payload)["code"] == "review_required"
    assert _journal_path().read_bytes() == before


# ----------------------------------------------------------------------
# 解除入口：正常路径与审计
# ----------------------------------------------------------------------
def test_resolve_station_absent_discards_with_audit_and_unblocks(tmp_path):
    _seed([_record("cr-1"), _record("cr-2")])
    bridge = _bridge(tmp_path)
    before_fingerprint = sss_uncertain.journal_fingerprint(_journal_path())
    _snapshot(bridge, classifications={"cr-1": "station_missing",
                                       "cr-2": "station_missing"})
    out = bridge.sss_uncertain_resolve({
        "decision": "station_absent", "confirm": "station_absent",
        "note": "逐条核对了闪时送站内订单，确认这 2 单没有落单",
        "record_ids": ["cr-1", "cr-2"]})

    assert out["ok"] is True and out["status"] == "discarded"
    assert out["changed"] is True and out["affected"] == 2 and out["remaining"] == 0
    assert out["post_sent"] is False and out["cloud_write"] is False
    assert out["audit"]["note"].startswith("逐条核对")
    # 审计记录的是“证据校验时”的指纹（写入前那一刻），便于事后追溯。
    assert out["audit"]["journal_fingerprint"] == before_fingerprint

    records = sss_uncertain.load_journal(_journal_path())["records"]
    assert [record["status"] for record in records] == ["discarded", "discarded"]
    assert records[0]["discarded_note"] == out["audit"]["note"]
    assert records[0]["discarded_by"] == "local-admin"
    assert "人工核对" in records[0]["discarded_reason"]
    # 阻断确实解除：同一批次键下不再有活跃未决记录。
    assert sss_uncertain.pending_records(
        records, _today() + "|" + ACCOUNT) == []
    assert bridge.sss_uncertain_records()["counts"]["active"] == 0


def test_resolve_station_present_resolves_without_review_evidence(tmp_path):
    _seed([_record("cr-1")])
    bridge = _bridge(tmp_path)
    out = bridge.sss_uncertain_resolve({
        "decision": "station_present", "confirm": "station_present",
        "note": "站内已看到这一单，不再重发", "record_ids": ["cr-1"]})

    assert out["ok"] is True and out["status"] == "resolved" and out["affected"] == 1
    records = sss_uncertain.load_journal(_journal_path())["records"]
    assert records[0]["status"] == "resolved"
    assert records[0]["resolved_reason"] == "站内已看到这一单，不再重发"
    assert records[0]["resolved_by"] == "local-admin"


def test_resolve_never_logs_in_or_posts(tmp_path, monkeypatch):
    _seed([_record("cr-1")])
    bridge = _bridge(tmp_path)
    _snapshot(bridge, classifications={"cr-1": "station_missing"})

    class _Boom:
        def __init__(self, *args, **kwargs):
            raise AssertionError("解除入口不允许登录/发请求")

    monkeypatch.setattr(sss_runner, "SssApiClient", _Boom)
    out = bridge.sss_uncertain_resolve({
        "decision": "station_absent", "confirm": "station_absent",
        "note": "人工核对完成，站内没有", "record_ids": ["cr-1"]})
    assert out["ok"] is True and out["post_sent"] is False


# ----------------------------------------------------------------------
# 宽窗复核：默认行为不变，±N 天能识别“落单了但日期被改过”
# ----------------------------------------------------------------------
def _task(identifier: str, *, name: str, phone: str, day: str) -> dict:
    return {
        "identifier": identifier,
        "payload": {},
        "account": ACCOUNT,
        "fingerprint": sss_reconcile.OrderFingerprint(
            receive_name=name, receive_phone=phone, door_num="A101",
            expected_delivery_time=day + " 11:00:00", account=ACCOUNT),
    }


def _fetch_with(records: list[dict]):
    def _fetch(path: str) -> dict:
        return {"success": True, "result": {"records": list(records),
                                            "total": len(records)}}
    return _fetch


def test_expand_days_zero_is_identity():
    assert sss_reconcile._expand_days({"2026-09-22"}, 0) == {"2026-09-22"}
    assert sss_reconcile._expand_days({""}, 0) == {""}
    assert sss_reconcile._expand_days({"2026-09-22"}, 1) == {
        "2026-09-21", "2026-09-22", "2026-09-23"}
    assert sss_reconcile._expand_days({"bad-day"}, 2) == {"bad-day"}


def test_days_margin_zero_equals_legacy_default():
    day = _today()
    tasks = [_task("t1", name="张三", phone="13800000001", day=day)]
    fetch = _fetch_with([_station_record("张三", "13800000001", day + " 11:00:00",
                                        order_id="S1")])
    legacy = sss_reconcile._reconcile_tasks(tasks, fetch)
    explicit = sss_reconcile._reconcile_tasks(tasks, fetch, days_margin=0)
    assert legacy.confirmed == explicit.confirmed == {"t1"}
    assert legacy.missing == explicit.missing == []


def test_wide_window_keeps_strict_semantics_and_person_scan_finds_other_day():
    day = _today()
    other = (dt.date.fromisoformat(day) + dt.timedelta(days=1)).isoformat()
    tasks = [_task("t1", name="张三", phone="13800000001", day=day)]
    fetch = _fetch_with([_station_record("张三", "13800000001",
                                         other + " 11:00:00", order_id="S1")])
    # 严格对账按预约时间匹配：放宽日期窗口也不许把“改了送达日”的单算成已确认，
    # 否则收尾对账会把已落单的订单当成缺失，再补发一次。
    strict = sss_reconcile._reconcile_tasks(tasks, fetch, days_margin=3)
    assert strict.confirmed == set()
    assert [item["identifier"] for item in strict.missing] == ["t1"]

    # 宽窗复核（姓名 + 电话）才是用来识别这种订单的：
    wide = sss_reconcile._reconcile_person_matches(tasks, fetch, days_margin=3)
    assert list(wide) == ["t1"]
    assert wide["t1"][0]["delivery_time"] == other + " 11:00:00"
    assert wide["t1"][0]["order_id"] == "S1"
    # 不放宽日期时看不到它（与严格对账一致）。
    assert sss_reconcile._reconcile_person_matches(tasks, fetch) == {}


# ----------------------------------------------------------------------
# 只读核对 worker：零 POST，且把逐条分类交给证据快照
# ----------------------------------------------------------------------
def _run_review(monkeypatch, tmp_path, *, orders: list[dict]):
    _FakeSssClient.instances = []
    _FakeSssClient.orders = list(orders)
    monkeypatch.setattr(sss_runner, "SssApiClient", _FakeSssClient)
    bridge = _bridge(tmp_path)
    result = sss_runner.run_sss_review_job(
        bridge._config, __import__("threading").Event(), lambda message: None,
        password="pw", captcha_callback=lambda image: "1234",
        snapshot_sink=bridge._remember_sss_review)
    return bridge, result


def test_review_reports_missing_and_never_posts(tmp_path, monkeypatch):
    _seed([_record("cr-1"), _record("cr-2")])
    bridge, result = _run_review(monkeypatch, tmp_path, orders=[])

    assert result["status"] == "review_blocked"
    assert result["post_sent"] is False
    assert result["review"]["counts"]["station_missing"] == 2
    assert result["review"]["counts"]["station_confirmed"] == 0
    assert "张三" not in json.dumps(result, ensure_ascii=False)
    client = _FakeSssClient.instances[-1]
    assert client.logged_in is True
    assert all("/one-touch-send/list" in path for path in client.requests)
    # 证据快照覆盖全部活跃记录，且分类为“站内缺失”。
    state = bridge.sss_uncertain_records()
    assert state["review"]["available"] is True
    assert state["review"]["journal_matches"] is True
    assert state["review"]["covers_active"] is True
    assert state["review"]["classifications"] == {"cr-1": "station_missing",
                                                  "cr-2": "station_missing"}


def test_review_confirms_orders_found_on_station_and_unblocks(tmp_path, monkeypatch):
    day = _today()
    _seed([_record("cr-1", name="张三", phone="13800000001"),
           _record("cr-2", name="李四", phone="13900000002")])
    bridge, result = _run_review(monkeypatch, tmp_path, orders=[
        _station_record("张三", "13800000001", day + " 11:00:00", order_id="S1"),
        _station_record("李四", "13900000002", day + " 11:00:00", order_id="S2"),
    ])

    assert result["status"] == "review_ok"
    assert result["review"]["counts"]["station_confirmed"] == 2
    assert result["review"]["counts"]["active"] == 0
    records = sss_uncertain.load_journal(_journal_path())["records"]
    assert {record["status"] for record in records} == {"resolved"}
    assert sss_uncertain.pending_records(
        records, day + "|" + ACCOUNT) == []


def test_review_marks_other_day_matches_as_found_other_day(tmp_path, monkeypatch):
    day = _today()
    other = (dt.date.fromisoformat(day) + dt.timedelta(days=1)).isoformat()
    _seed([_record("cr-1", name="张三", phone="13800000001")])
    bridge, result = _run_review(monkeypatch, tmp_path, orders=[
        _station_record("张三", "13800000001", other + " 11:00:00", order_id="S1"),
    ])

    assert result["status"] == "review_blocked"
    assert result["review"]["counts"]["station_found_other_day"] == 1
    assert result["review"]["counts"]["station_missing"] == 0
    assert bridge.sss_uncertain_records()["review"]["classifications"] == {
        "cr-1": "station_found_other_day"}
    # 宽窗命中说明“很可能已经落单”，此时不允许按缺失解除阻断。
    out = bridge.sss_uncertain_resolve({
        "decision": "station_absent", "confirm": "station_absent",
        "note": "人工核对完成", "record_ids": ["cr-1"]})
    assert out["code"] == "station_state_changed"


def test_review_failure_never_clears_block(tmp_path, monkeypatch):
    _seed([_record("cr-1")])

    class _Boom(_FakeSssClient):
        def get_json(self, path: str) -> dict:
            raise RuntimeError("列表接口 500")

    monkeypatch.setattr(sss_runner, "SssApiClient", _Boom)
    bridge = _bridge(tmp_path)
    result = sss_runner.run_sss_review_job(
        bridge._config, __import__("threading").Event(), lambda message: None,
        password="pw", captcha_callback=lambda image: "1234",
        snapshot_sink=bridge._remember_sss_review)

    assert result["status"] == "review_failed"
    assert bridge.sss_uncertain_records()["counts"]["active"] == 1
    out = bridge.sss_uncertain_resolve({
        "decision": "station_absent", "confirm": "station_absent",
        "note": "人工核对完成", "record_ids": ["cr-1"]})
    assert out["code"] == "review_required"