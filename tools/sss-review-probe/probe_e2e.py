"""独立验证探针：闪时送「只读核对 + 管理员带审计解除」端到端复现。

与 tests/test_sss_uncertain_review_independent.py 相互独立：本脚本用真实
run_sss_review_job / run_sss_job + 伪 SssApiClient 走完整流程，任何断言失败
退出码非 0。只读，不联网，不写仓库（临时目录在 TMPDIR 下）。

覆盖：
  A. 未解除前 run_sss_job 阻断（零 POST）→ run_sss_review_job 生成证据 →
     管理员 sss_uncertain_resolve(station_absent) → 重跑恰好重新 POST 一次并收敛；
  B. 站内已有匹配订单时，提交前的只读对账阻止 POST；
  C. 只读核对全流程只发 GET 列表请求（零 POST）；
  D. 观察项（非断言）：非管理员直调 Bridge.start_sss_review 的实际行为。

用法：python tools/sss-review-probe/probe_e2e.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

TMP = Path(tempfile.mkdtemp(prefix="sss-probe-", dir=os.environ.get("TMPDIR") or None))
os.environ["YIKOU_DATA_DIR"] = str(TMP / "userdata")
os.environ["YIKOU_SSS_LOCK_ROOT"] = str(TMP / "locks")
os.environ["YIKOU_SSS_AUTHORITATIVE_ROOT"] = str(TMP / "authority-root")
os.environ["YIKOU_SSS_AUTHORITATIVE_PATH"] = str(
    TMP / "authority" / "sss_uncertain_authoritative.json")
os.environ["YIKOU_SSS_AUTHORITY_LOCATIONS"] = str(
    TMP / "userdata" / "sss-authority-locations.json")
os.environ.pop("YIKOU_SSS_UNCERTAIN_PATH", None)

from app.api import bridge as bridge_module  # noqa: E402
from app.api.bridge import Bridge  # noqa: E402
from app.ordering import reconcile as sss_reconcile  # noqa: E402
from app.ordering import runner as sss_runner  # noqa: E402
from app.ordering import uncertain as sss_uncertain  # noqa: E402
from app.ordering.constants import _CREATE_ORDER_PATH, _ORDER_LIST_PATH  # noqa: E402
from app.ordering.models import OrderFingerprint  # noqa: E402
from app.ordering.sss import expected_delivery_date  # noqa: E402

sss_reconcile._SSS_SERVER_PREFILTER = False
sss_reconcile._RECONCILE_POLL_INTERVAL_S = 0.0
sss_runner._REVIEW_WIDE_WINDOW_DAYS = 3

ACCOUNT = "18758187837"
DAY = str(expected_delivery_date())
FP = OrderFingerprint(
    receive_name="张三", receive_phone="13800000001", door_num="A101",
    expected_delivery_time=DAY + " 11:00:00", account=ACCOUNT,
    store_id="7", goods_name="轻食", goods_num="1",
    address_detail="武汉市洪山区某某路1号", area_code="420111",
    lnt="119.728224", lat="30.256632", order_type="1")

CHECKS: list[tuple[str, bool, str]] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    CHECKS.append((label, bool(ok), detail))
    print(("PASS  " if ok else "FAIL  ") + label + (("  [" + detail + "]") if detail else ""))


def journal_path() -> Path:
    return Path(os.environ["YIKOU_SSS_AUTHORITATIVE_PATH"])


def seed(records: list[dict]) -> None:
    target = journal_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({"version": 1, "records": records},
                                 ensure_ascii=False), encoding="utf-8")


def record(identifier: str) -> dict:
    return {
        "journal_id": identifier, "identifier": identifier,
        "batch_key": DAY + "|" + ACCOUNT, "delivery_date": DAY, "account": ACCOUNT,
        "platform": "https://sssplusnew.zhuopaikeji.com",
        "fingerprint": FP.as_dict(), "status": "inflight",
        "error": "提交前置记录：POST 即将发出",
        "created_at": DAY + "T07:00:00", "batch_started_at": time.time(),
    }


def station_record(when: str | None = None) -> dict:
    return {
        "id": "S1", "recipientName": "张三", "recipientPhone": ["13800000001"],
        "recipientAddress": "武汉市洪山区某某路1号A101",
        "expectedDeliveryTime": when or (DAY + " 11:00:00"),
        "status": 2, "createTime": time.time(),
    }


class Fake:
    """记录所有请求；GET 只服务列表/余额，POST 记录并模拟落单。"""

    last: "Fake | None" = None
    post_calls: list[tuple[str, dict]] = []

    def __init__(self, *args, **kwargs) -> None:
        self.gets: list[str] = []
        self.orders: list[dict] = []
        self.after_post: list[dict] = []
        self.logged_in = False
        Fake.last = self

    def fetch_captcha(self) -> bytes:
        return b"png"

    def login(self, code: str) -> None:
        self.logged_in = True

    def get_json(self, path: str) -> dict:
        self.gets.append(path)
        if path.startswith(_ORDER_LIST_PATH):
            return {"success": True,
                    "result": {"records": list(self.orders), "total": len(self.orders)}}
        if "get-login-user-account" in path:
            return {"success": True,
                    "result": {"totalAmount": 1000.0, "freezeAmount": 0.0}}
        raise AssertionError("探针不允许访问 " + path)

    def post_json(self, path: str, payload: dict) -> dict:
        Fake.post_calls.append((path, payload))
        self.orders = list(self.after_post)
        return {"success": True}

    def fork(self):
        return self

    def close(self) -> None:
        pass


def install(*, orders: list[dict], after_post: list[dict]) -> None:
    excel = TMP / "sss.xlsx"
    excel.write_bytes(b"placeholder")
    sss_runner.load_sss_orders = lambda *a, **k: {
        "午餐": [{"row": 3, "name": "张三", "door": "A101", "phone": "13800000001"}]}
    sss_runner._collect_tasks = lambda *a, **k: [{
        "sheet": "午餐", "identifier": "第 3 行 张三", "payload": {},
        "fingerprint": FP, "account": ACCOUNT, "batch_id": "batch-1",
        "client_request_id": "cr-new"}]
    Fake.post_calls = []
    Fake.last = None

    def factory(*args, **kwargs):
        client = Fake()
        client.orders = list(orders)
        client.after_post = list(after_post)
        return client

    sss_runner.SssApiClient = factory


def bridge(*, is_admin: bool = True) -> Bridge:
    b = Bridge(config_path=str(TMP / "config.json"), is_admin=is_admin)
    b._config.sss_url = "https://sssplusnew.zhuopaikeji.com/takeout"
    b._config.sss_account = ACCOUNT
    b._config.sss_order_source = "excel"
    b._config.sss_excel_path = str(TMP / "sss.xlsx")
    b._config.sss_dry_run = False
    b._config.sss_use_fixed_address = True
    b._config.sss_store_name = "一口轻食"
    b._config.sss_store_id = 7
    b._config.sss_store_name_cached = "一口轻食"
    b._config.sss_unit_price = 0.0
    b._config.sss_max_workers = 1
    return b


def run_job(b: Bridge) -> dict:
    return sss_runner.run_sss_job(b._config, threading.Event(), lambda m: None,
                                  password="pw", captcha_callback=lambda i: "1234")


def run_review(b: Bridge) -> dict:
    return sss_runner.run_sss_review_job(
        b._config, threading.Event(), lambda m: None, password="pw",
        captcha_callback=lambda i: "1234", snapshot_sink=b._remember_sss_review)


def scenario_a() -> None:
    print("== A. 阻断 -> 只读核对 -> 管理员解除 -> 重跑恰好一次 POST ==")
    seed([record("cr-1")])
    install(orders=[], after_post=[station_record()])
    b = bridge()

    blocked = run_job(b)
    check("A1 解除前 run_sss_job 阻断且零 POST",
          blocked["status"] == "blocked_uncertain" and Fake.post_calls == [],
          "status=" + str(blocked["status"]))

    review = run_review(b)
    check("A2 只读核对判 station_missing 且零 POST",
          review["status"] == "review_blocked"
          and review["review"]["counts"]["station_missing"] == 1
          and Fake.post_calls == [],
          str(review["review"]["counts"]))
    view = b.sss_uncertain_records()["review"]
    check("A3 证据覆盖全部活跃记录且指纹匹配",
          view["available"] and view["journal_matches"] and view["covers_active"]
          and view["classifications"] == {"cr-1": "station_missing"})

    out = b.sss_uncertain_resolve({
        "decision": "station_absent", "confirm": "station_absent",
        "note": "逐条人工核对闪时送站内订单后确认", "record_ids": ["cr-1"]})
    check("A4 管理员解除成功且自身零 POST",
          out["ok"] and out["status"] == "discarded" and out["post_sent"] is False
          and Fake.post_calls == [], "affected=" + str(out["affected"]))

    final = run_job(b)
    posts = [path for path, _ in Fake.post_calls]
    check("A5 重跑恰好重新 POST 一次并收敛",
          posts == [_CREATE_ORDER_PATH] and final["status"] == "confirmed"
          and final["created"] == 1,
          "posts=" + str(posts) + " status=" + str(final["status"]))
    records = sss_uncertain.load_journal(journal_path())["records"]
    check("A6 结束后无活跃未决记录",
          sss_uncertain.pending_records(records, DAY + "|" + ACCOUNT) == [],
          str([(r["journal_id"], r["status"]) for r in records]))


def scenario_b() -> None:
    print("== B. 站内已命中 -> 提交前只读对账阻止 POST ==")
    seed([record("cr-1")])
    install(orders=[station_record()], after_post=[station_record()])
    b = bridge()
    result = run_job(b)
    check("B1 站内命中时零 POST", Fake.post_calls == [],
          "posts=" + str([p for p, _ in Fake.post_calls]))
    check("B2 结果被站内对账确认", result["status"] == "confirmed"
          and result["created"] == 1, "status=" + str(result["status"]))
    check("B3 未决记录被站内确认并清理",
          [r["status"] for r in sss_uncertain.load_journal(journal_path())["records"]]
          == ["resolved"])


def scenario_c() -> None:
    print("== C. 只读核对零 POST，且只访问列表 GET ==")
    seed([record("cr-1")])
    install(orders=[], after_post=[])
    b = bridge()
    run_review(b)
    client = Fake.last
    gets = list(client.gets)
    check("C1 只读核对确实查询了列表", bool(gets))
    check("C2 全部请求都是 GET 列表", all(p.startswith(_ORDER_LIST_PATH) for p in gets),
          "n=" + str(len(gets)))
    check("C3 零 POST", Fake.post_calls == [])


def scenario_d() -> None:
    print("== D. 观察项：非管理员直调 Bridge.start_sss_review ==")
    seed([record("cr-1")])
    b = bridge(is_admin=False)
    before = journal_path().read_bytes()
    bridge_module.run_sss_review_job = lambda *a, **k: {
        "status": "review_ok", "review": {"counts": {}}}
    out = b.start_sss_review({"password": "pw"})
    deadline = time.monotonic() + 5
    while b.worker_alive() and time.monotonic() < deadline:
        time.sleep(0.01)
    print("      start_sss_review -> " + json.dumps(out, ensure_ascii=False)[:200])
    print("      journal unchanged = " + str(journal_path().read_bytes() == before))
    if out.get("ok") is True:
        print("      OBSERVED DEFECT(bridge.py:933): 直调非管理员实例未被拦截，"
              "返回 ok=true 并启动了只读 worker；HTTP 层已 403 admin_only。")


if __name__ == "__main__":
    scenario_a()
    scenario_b()
    scenario_c()
    scenario_d()
    failed = [label for label, ok, _ in CHECKS if not ok]
    print("----")
    print("checks: " + str(len(CHECKS)) + " total, " + str(len(failed)) + " failed")
    if failed:
        for label in failed:
            print("FAILED: " + label)
        sys.exit(1)
    print("probe OK（临时目录 " + str(TMP) + "）")
