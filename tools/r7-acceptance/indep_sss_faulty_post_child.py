#!/usr/bin/env python3
"""R6-4 独立反证用“POST 超时/断线”合成子进程（不联网/不真实下单）。

与 ``tests/independent_sss_batch_child.py`` 同形，但增加一个关键能力：让
**合成平台已经收到并落库 POST**（``platform.json`` 计数 + 订单记录 + ``signal``）
之后，客户端按 ``INDEP_POST_FAULT`` 抛出传输层异常，模拟“服务端可能已落单、
客户端拿不到响应”的真实不确定窗口。

用法::

    INDEP_POST_FAULT=timeout python3 indep_sss_faulty_post_child.py \
        <work_dir> <journal_path> [mode] [account]

- ``INDEP_POST_FAULT`` 为空/未设置：POST 正常返回 ``{"success": true}``；
- ``timeout``：落库后抛 ``requests.exceptions.ReadTimeout``（读超时）；
- ``connection_reset``：落库后抛 ``requests.exceptions.ConnectionError``（断线）。

安全边界：只写 ``<shared_dir>/platform.json`` 与 ``signal`` 两个合成文件，
不联网（本文件从不导入网络会话）、不读真实凭据、不写真实 WPS/正式快照。
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None


def _read_state(platform: Path) -> dict:
    if not platform.exists():
        return {"count": 0, "records": []}
    return json.loads(platform.read_text(encoding="utf-8"))


def _mutate(platform: Path, mutator) -> None:
    """跨进程读改写 platform.json；用独立 .lock 避免 POST 计数丢失。"""
    lock_path = platform.with_suffix(".json.lock")
    with open(lock_path, "a+", encoding="utf-8") as lock:
        if fcntl is not None:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            state = _read_state(platform)
            mutator(state)
            tmp = platform.with_suffix(".tmp")
            tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, platform)
        finally:
            if fcntl is not None:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _raise_transport_fault(fault: str) -> None:
    """在 POST 已落库后抛出传输层异常，模拟“已发出但结果未知”。"""
    from requests.exceptions import ConnectionError as RequestsConnectionError
    from requests.exceptions import ReadTimeout

    if fault == "timeout":
        raise ReadTimeout("POST /takeout/... 读超时（服务端结果未知）")
    if fault == "connection_reset":
        raise RequestsConnectionError("POST /takeout/... 连接被重置（服务端结果未知）")
    raise AssertionError(f"未知 INDEP_POST_FAULT={fault!r}")


def main() -> int:
    work = Path(sys.argv[1])
    journal = Path(sys.argv[2])
    mode = sys.argv[3] if len(sys.argv) > 3 else "normal"
    account = sys.argv[4] if len(sys.argv) > 4 else "18758187837"
    fault = os.environ.get("INDEP_POST_FAULT", "").strip()
    url = os.environ.get("INDEP_SSS_URL", "http://local.invalid")
    work.mkdir(parents=True, exist_ok=True)
    shared_dir = Path(os.environ.get("INDEP_SHARED_DIR", str(work)))
    shared_dir.mkdir(parents=True, exist_ok=True)
    platform = shared_dir / "platform.json"
    signal = shared_dir / "signal"
    hidden = shared_dir / "hidden"

    repo_root = Path(__file__).resolve().parents[2]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    from openpyxl import Workbook

    excel = work / "闪时送.xlsx"
    wb = Workbook()
    ws = wb.active
    ws.title = "午餐"
    ws.append(["午餐", None, None, None])
    ws.append(["姓名", "门牌号", "电话", "送达时间"])
    ws.append(["张三", "A101", "13800000001", "11:00"])
    wb.save(excel)
    wb.close()

    from app.ordering import reconcile as sss_reconcile
    from app.ordering import runner as sss_runner
    from app.ordering import submission as sss_submission

    class _FixedDatetime(_dt.datetime):
        @classmethod
        def now(cls, tz=None):  # noqa: ANN001
            return cls(2026, 9, 16, 10, 0, 0)

    sss_runner._dt.datetime = _FixedDatetime
    sss_reconcile._SSS_SERVER_PREFILTER = False
    sss_reconcile._RECONCILE_POLL_INTERVAL_S = 0.0
    sss_submission._PREFILTER_ZERO_RETRY_DELAY_S = 0.0

    class _SyntheticClient:
        """只实现 runner 用到的客户端方法；POST 先落库再按需抛传输异常。"""

        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def fetch_captcha(self) -> bytes:
            return b"png"

        def login(self, _code: str) -> None:
            pass

        def get_json(self, path: str) -> dict:
            if "get-login-user-account" in path:
                return {"success": True,
                        "result": {"totalAmount": 1000.0, "freezeAmount": 0.0}}
            if "one-touch-send/list" in path:
                state = _read_state(platform)
                records = [] if hidden.exists() else (state.get("records") or [])
                return {"success": True,
                        "result": {"records": records, "total": len(records)}}
            raise AssertionError(f"unexpected GET {path}")

        def post_json(self, path: str, body: dict | None = None) -> dict:
            if body is None:
                body = {}
            record = {
                "id": f"order-{time.time_ns()}",
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

            def _record(state: dict) -> None:
                state["count"] = int(state.get("count", 0)) + 1
                state.setdefault("records", []).append(record)

            _mutate(platform, _record)
            signal.write_text("1", encoding="utf-8")
            # 持锁久一点，让第二个进程真实地等待 batch_submission_lock。
            time.sleep(1.0)
            if fault:
                # 服务端已收到并落库，但客户端拿不到可确认响应。
                _raise_transport_fault(fault)
            if mode == "crash":
                os._exit(17)
            return {"success": True}

        def fork(self):
            return self

        def close(self) -> None:
            pass

    sss_runner.SssApiClient = _SyntheticClient
    sss_runner._prepare_store_and_address = (
        lambda *_a, **_k: (211053, {"lnt": 1.0, "lat": 2.0,
                                   "areaCode": "330110", "addressDetail": "X"}))
    sss_runner.query_balance = lambda _fetch: (1000.0, 0.0)

    config = SimpleNamespace(
        sss_excel_path=str(excel), sss_order_source="excel",
        sss_account=account, sss_dry_run=False, sss_preflight=False,
        sss_store_name="一口轻食", sss_common_address="嗯哼",
        sss_use_fixed_address=True, sss_fixed_lnt=1.0, sss_fixed_lat=2.0,
        sss_fixed_area_code="330110", sss_fixed_address_detail="X",
        sss_product_name="轻食", sss_url=url,
        sss_store_id=211053, sss_store_name_cached="一口轻食",
        sss_max_workers=2, sss_unit_price=0.0, sss_read_timeout_s=5.0,
        sss_idempotency_field="", sss_uncertain_path=str(journal),
    )

    class _Stop:
        def is_set(self) -> bool:
            return False

        def set(self) -> None:
            pass

    result = sss_runner.run_sss_job(
        config, _Stop(), lambda _message: None,
        password="x", captcha_callback=lambda _img: "1234")
    print(json.dumps(result, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
