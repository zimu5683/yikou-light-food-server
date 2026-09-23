"""前端「未决记录面板」契约锁：bridge.ts 声明的字段，Python 必须真的返回。

**为什么需要这个文件**：frontend/src/lib/uncertainReview.ts 与
frontend/src/components/UncertainPanel.tsx 完全按 bridge.ts 里的 TS 接口读字段，
而 Python 侧（app/api/bridge.py 的 sss_uncertain_records / start_sss_review /
sss_uncertain_resolve）与它是**不同语言、没有编译期或运行期链接**。字段名一旦漂移：

* pytest 全绿（Python 测试断言的是新名字）；
* 前端 node --test 也全绿（纯逻辑测试喂的是手写 fixture）；
* 界面上读到 undefined，**静默失灵**，只有人肉点开才发现。

本文件仿照 tests/test_frontend_contract.py：用正则从 bridge.ts 解析接口字段，
再调用**真实** Bridge 方法（合成 journal，全程离线），断言 TS 标记为必需（无问号）
的字段一个都不少。Python 多返回字段无害，因此只做单向校验。

与 test_sss_uncertain_review.py 的分工：那边校验**值语义**（拒绝矩阵、审计留痕、
零 POST），本文件只校验**字段存在**；两者互补，不互相替代。
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import time
from pathlib import Path

import pytest

from app.api.bridge import Bridge
from app.ordering import uncertain as sss_uncertain
from app.ordering.sss import expected_delivery_date

BRIDGE_TS = Path(__file__).resolve().parent.parent / "frontend" / "src" / "lib" / "bridge.ts"
UNCERTAIN_TS = (Path(__file__).resolve().parent.parent
                / "frontend" / "src" / "lib" / "uncertainReview.ts")


# ----------------------------------------------------------------------
# 解析器（与 test_frontend_contract.py 同一套规则，独立实现以免互相掩盖）
# ----------------------------------------------------------------------
def ts_fields(interface: str) -> dict[str, bool]:
    """返回 字段名 -> 是否可选。"""
    source = BRIDGE_TS.read_text(encoding="utf-8")
    match = re.search(rf"export interface {interface} \{{(.*?)\n\}}", source, re.S)
    assert match, f"bridge.ts 里找不到接口 {interface}"
    body = re.sub(r"/\*.*?\*/", "", match.group(1), flags=re.S)
    body = re.sub(r"//.*", "", body)
    fields: dict[str, bool] = {}
    for line in body.splitlines():
        found = re.match(r"\s*([A-Za-z_][A-Za-z0-9_]*)(\?)?\s*:", line)
        if found:
            fields[found.group(1)] = found.group(2) == "?"
    return fields


def required_fields(interface: str) -> list[str]:
    return [name for name, optional in ts_fields(interface).items() if not optional]


def assert_required_present(interface: str, payload: dict) -> None:
    required = required_fields(interface)
    missing = [name for name in required if name not in payload]
    assert not missing, (
        f"前端接口 {interface} 要求的字段在 Python 返回值里缺失：{missing}；"
        f"这会让界面读到 undefined（Python 实际返回：{sorted(payload)}）")


# ----------------------------------------------------------------------
# 隔离环境 + 合成 journal（与 test_sss_uncertain_review.py 同款，不发任何请求）
# ----------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    monkeypatch.setenv("YIKOU_DATA_DIR", str(tmp_path / "userdata"))
    monkeypatch.setenv("YIKOU_SSS_LOCK_ROOT", str(tmp_path / "locks"))
    monkeypatch.setenv("YIKOU_SSS_AUTHORITATIVE_PATH",
                       str(tmp_path / "authority" / "sss_uncertain_authoritative.json"))
    monkeypatch.delenv("YIKOU_SSS_UNCERTAIN_PATH", raising=False)


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
            status: str = "inflight",
            error: str = "提交前置记录：POST 即将发出") -> dict:
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
    bridge.set_request_identity(None)
    bridge._config.sss_url = "https://sssplusnew.zhuopaikeji.com/takeout"
    bridge._config.sss_account = ACCOUNT
    bridge._config.sss_order_source = "wps"
    return bridge


# ----------------------------------------------------------------------
# 1) sss_uncertain_records -> SssUncertainState / SssUncertainRecord / SssUncertainReview
# ----------------------------------------------------------------------
def test_records_state_satisfies_contract(tmp_path):
    _seed([_record("cr-1"), _record("cr-2", status="unresolved")])
    state = _bridge(tmp_path).sss_uncertain_records()

    assert state["ok"] is True, "合成 journal 应能只读读出未决记录"
    assert_required_present("SssUncertainState", state)
    assert state["records"], "至少要有一条记录才能校验行结构"
    for record in state["records"]:
        assert_required_present("SssUncertainRecord", record)
    # 没有核对快照时也必须满足 SssUncertainReview 的必需字段。
    assert_required_present("SssUncertainReview", state["review"])
    assert state["review"]["available"] is False
    assert state["review"]["classifications"] == {}


def test_records_with_snapshot_satisfies_review_contract(tmp_path):
    """有核对快照（available=True）是另一条分支，字段同样不能少。"""
    _seed([_record("cr-1")])
    bridge = _bridge(tmp_path)
    bridge._remember_sss_review({
        "journal_fingerprint": "ab12cd34ef56",
        "checked_at": "2026-09-22T07:41:10",
        "wide_window_days": 3,
        "classifications": {"cr-1": "station_missing"},
        "record_ids": ["cr-1"],
        "counts": {"station_missing": 1, "station_found_other_day": 0,
                   "station_confirmed": 0, "scan_failed": 0},
    })
    state = bridge.sss_uncertain_records()
    assert state["ok"] is True
    assert_required_present("SssUncertainState", state)
    assert_required_present("SssUncertainReview", state["review"])
    assert state["review"]["available"] is True
    assert state["review"]["classifications"] == {"cr-1": "station_missing"}


@pytest.mark.parametrize("kind", ["forbidden", "corrupt"])
def test_records_failure_shapes_satisfy_state_contract(tmp_path, kind):
    """失败形状（非管理员 / journal 损坏）也必须给全 SssUncertainState 必需字段。"""
    if kind == "corrupt":
        target = _journal_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("{ not json", encoding="utf-8")
        state = _bridge(tmp_path).sss_uncertain_records()
        assert state["code"] == "journal_unreadable"
    else:
        _seed([_record("cr-1")])
        state = _bridge(tmp_path, is_admin=False).sss_uncertain_records()
        assert state["code"] == "forbidden"

    assert state["ok"] is False
    assert_required_present("SssUncertainState", state)


def test_failure_shape_returns_partial_review_and_frontend_fallback_covers_it(tmp_path):
    """失败形状返回 review={}：前端必须自带同字段的兜底对象。

    SssUncertainReview 的必需字段在失败形状里并不齐全（后端故意返回空 dict）。
    前端只有在 uncertainReviewView 里对缺字段全部兜底时才安全 —— 这里把
    EMPTY_SSS_REVIEW 的键与 TS 必需字段做集合比对，锁住这份兜底。
    """
    target = _journal_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("{ not json", encoding="utf-8")
    state = _bridge(tmp_path).sss_uncertain_records()
    assert state["review"] == {}, "后端失败形状仍是空 dict（前端兜底的前提）"

    source = UNCERTAIN_TS.read_text(encoding="utf-8")
    match = re.search(r"export const EMPTY_SSS_REVIEW: SssUncertainReview = \{(.*?)\n\}",
                      source, re.S)
    assert match, "uncertainReview.ts 里找不到 EMPTY_SSS_REVIEW 字面量"
    fallback = set(re.findall(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*:", match.group(1),
                              re.M))
    assert fallback == set(required_fields("SssUncertainReview")), (
        "EMPTY_SSS_REVIEW 的字段必须与 SssUncertainReview 的必需字段完全一致，"
        f"否则失败形状会让界面读到 undefined：兜底 {sorted(fallback)} vs "
        f"必需 {sorted(required_fields('SssUncertainReview'))}")


# ----------------------------------------------------------------------
# 2) sss_uncertain_resolve -> SssUncertainResolveResult
# ----------------------------------------------------------------------
def test_resolve_failure_shapes_satisfy_contract(tmp_path):
    _seed([_record("cr-1"), _record("cr-2")])
    bridge = _bridge(tmp_path)

    def _call(**payload):
        payload.setdefault("record_ids", ["cr-1", "cr-2"])
        return bridge.sss_uncertain_resolve(payload)

    failures = [
        _call(decision="nope", confirm="nope", note="核对过了"),
        _call(decision="station_absent", confirm="station_absent", note="短"),
        _call(decision="station_absent", confirm="wrong", note="人工核对完成"),
        _call(decision="station_absent", confirm="station_absent", note="人工核对完成",
              record_ids=[]),
        _call(decision="station_present", confirm="station_present", note="人工核对完成",
              record_ids=["cr-1", "cr-不存在"]),
        _call(decision="station_absent", confirm="station_absent", note="人工核对完成"),
    ]
    codes = [out["code"] for out in failures]
    assert codes == ["invalid_payload", "note_required", "confirmation_required",
                     "invalid_record_ids", "unknown_record_ids", "review_required"], codes
    for out in failures:
        assert out["ok"] is False
        assert_required_present("SssUncertainResolveResult", out)


def test_resolve_forbidden_shape_satisfies_contract(tmp_path):
    _seed([_record("cr-1")])
    out = _bridge(tmp_path, is_admin=False).sss_uncertain_resolve({
        "decision": "station_absent", "confirm": "station_absent",
        "note": "人工核对完成", "record_ids": ["cr-1"]})
    assert out["status"] == "forbidden"
    assert_required_present("SssUncertainResolveResult", out)


def test_resolve_success_shapes_satisfy_contract(tmp_path):
    _seed([_record("cr-1"), _record("cr-2")])
    bridge = _bridge(tmp_path)

    kept = bridge.sss_uncertain_resolve({
        "decision": "keep", "confirm": "keep", "note": "先不处理",
        "record_ids": ["cr-1"]})
    assert kept["ok"] is True and kept["status"] == "kept"
    assert_required_present("SssUncertainResolveResult", kept)

    resolved = bridge.sss_uncertain_resolve({
        "decision": "station_present", "confirm": "station_present",
        "note": "站内已看到这一单", "record_ids": ["cr-1"]})
    assert resolved["ok"] is True and resolved["status"] == "resolved"
    assert_required_present("SssUncertainResolveResult", resolved)

    # station_absent 需要新鲜且指纹一致的快照；这里给一份合成证据。
    bridge._remember_sss_review({
        "journal_fingerprint": sss_uncertain.journal_fingerprint(_journal_path()),
        "checked_at": dt.datetime.now().isoformat(timespec="seconds"),
        "wide_window_days": 3,
        "classifications": {"cr-2": "station_missing"},
        "record_ids": ["cr-2"],
        "counts": {"station_missing": 1},
    })
    discarded = bridge.sss_uncertain_resolve({
        "decision": "station_absent", "confirm": "station_absent",
        "note": "逐条核对确认站内没有这一单", "record_ids": ["cr-2"]})
    assert discarded["ok"] is True and discarded["status"] == "discarded"
    assert_required_present("SssUncertainResolveResult", discarded)


# ----------------------------------------------------------------------
# 3) start_sss_review -> SssReviewStartResult
# ----------------------------------------------------------------------
def test_start_review_validation_failure_satisfies_contract(tmp_path):
    """缺网址/账号/密码走 validation_failed：不启动 worker、不联网。"""
    bridge = _bridge(tmp_path)
    bridge._config.sss_url = ""
    bridge._config.sss_account = ""
    out = bridge.start_sss_review({})
    assert out["ok"] is False and out["reason"] == "validation_failed"
    assert_required_present("SssReviewStartResult", out)
    assert set(out["fields"]) == {"url", "account", "password"}

    bridge._config.sss_url = "https://sssplusnew.zhuopaikeji.com/takeout"
    bridge._config.sss_account = ACCOUNT
    out2 = bridge.start_sss_review({"password": ""})
    assert out2["reason"] == "validation_failed"
    assert_required_present("SssReviewStartResult", out2)


def test_start_review_launch_shape_satisfies_contract(tmp_path, monkeypatch):
    """启动成功形状：把 _launch 换成假实现，绝不真的开线程/联网。"""
    bridge = _bridge(tmp_path)
    calls: list[str] = []

    def _fake_launch(mode, config, count, password):
        calls.append(str(mode))
        return True

    monkeypatch.setattr(bridge, "_launch", _fake_launch)
    out = bridge.start_sss_review({"password": "pw"})
    assert calls == ["sss_review"], "必须走 sss_review 只读核对入口"
    assert out["ok"] is True
    assert_required_present("SssReviewStartResult", out)


def test_start_review_conflict_shape_satisfies_contract(tmp_path):
    """已有活动操作 -> operation_conflict（同一形状也要给全必需字段）。"""
    bridge = _bridge(tmp_path)
    reservation = bridge._operations.try_reserve("sss", summary={})
    assert reservation.granted
    out = bridge.start_sss_review({"password": "pw"})
    assert out["ok"] is False and out["code"] == "operation_conflict"
    assert_required_present("SssReviewStartResult", out)


# ----------------------------------------------------------------------
# 4) 解析器与「假通过」自检
# ----------------------------------------------------------------------
def test_interface_parser_reads_real_definitions():
    state = ts_fields("SssUncertainState")
    assert len(state) >= 12, "解析到的字段太少，正则很可能失效了"
    assert state["ok"] is False and state["read_only"] is False
    assert state["contract_version"] is False
    assert state["records"] is True and state["review"] is True

    record = ts_fields("SssUncertainRecord")
    assert record["journal_id"] is False and record["batch_started_at"] is False
    assert len(required_fields("SssUncertainRecord")) >= 14

    review = ts_fields("SssUncertainReview")
    assert review["journal_matches"] is False and review["age_s"] is False
    assert review["counts"] is False and review["classifications"] is False

    result = ts_fields("SssUncertainResolveResult")
    assert {"ok", "status", "code", "reason", "next_action"} <= set(
        required_fields("SssUncertainResolveResult"))
    assert result["changed"] is True and result["post_sent"] is True

    assert set(required_fields("SssReviewStartResult")) == {
        "ok", "status", "reason", "next_action"}

    with pytest.raises(AssertionError):
        ts_fields("这个接口不存在")


def test_contract_check_would_catch_a_renamed_field(tmp_path, monkeypatch):
    """自检：把 Python 返回的必需字段改名，契约校验必须报错。

    没有这条，「契约测试」可能只是恰好通过而已。
    """
    _seed([_record("cr-1")])
    bridge = _bridge(tmp_path)
    original = bridge.sss_uncertain_records

    def renamed():
        state = original()
        # read_only 是 SssUncertainState 的必需字段（records/counts/review 反而是可选的）。
        state["readOnly"] = state.pop("read_only")
        return state

    monkeypatch.setattr(bridge, "sss_uncertain_records", renamed)
    with pytest.raises(AssertionError, match="read_only"):
        assert_required_present("SssUncertainState", bridge.sss_uncertain_records())


def test_contract_check_would_catch_a_renamed_record_field(tmp_path, monkeypatch):
    _seed([_record("cr-1")])
    bridge = _bridge(tmp_path)
    original = bridge.sss_uncertain_records

    def renamed():
        state = original()
        record = state["records"][0]
        record["journalId"] = record.pop("journal_id")
        return state

    monkeypatch.setattr(bridge, "sss_uncertain_records", renamed)
    with pytest.raises(AssertionError, match="journal_id"):
        assert_required_present("SssUncertainRecord",
                                bridge.sss_uncertain_records()["records"][0])
