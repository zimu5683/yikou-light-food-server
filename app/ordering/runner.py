"""闪时送任务入口：从云端/本地读取订单，走完提交与对账全流程。"""

from __future__ import annotations

import datetime as _dt
import time
import uuid
from functools import wraps
from pathlib import Path
from typing import Any, Callable

from app.integrations.api_client import SssApiClient
from app.order.runner import _emit
from app.ordering.cloud_import import prepare_day_orders
from app.ordering.constants import (
    DEFAULT_SSS_URL,
    _CLIENT_IDEMPOTENCY_FIELD,
    _SSS_RUN_LOCK,
)
from app.ordering.models import _AuthExpired
from app.ordering.payload import (
    _cached_store_id,
    _collect_tasks,
    _fixed_address_from_config,
    _prepare_store_and_address,
    _run_dry_run,
)
from app.ordering.submission import (
    _balance_precheck,
    _format_balance,
    _make_api_submitter,
    _preflight_tasks,
    _run_reconciled_submission,
    _with_auth_relogin,
    query_balance,
)
from app.ordering.workbook import (
    _validate_sss_orders,
    expected_delivery_date,
    load_sss_orders,
)
def _exclusive_sss_job(func: Callable[..., Any]) -> Callable[..., Any]:
    """同一进程内拒绝两个闪时送下单任务并发提交。"""
    @wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        if not _SSS_RUN_LOCK.acquire(blocking=False):
            raise RuntimeError("已有闪时送下单任务正在运行，拒绝并发执行")
        try:
            return func(*args, **kwargs)
        finally:
            _SSS_RUN_LOCK.release()
    return wrapper


@_exclusive_sss_job
def run_sss_job(config: Any, stop_event: Any,
                progress_callback: Callable[[str], Any] | None = None,
                password: str | None = None,
                decision_callback: Callable[[str, str], str] | None = None,
                captcha_callback: Callable[[bytes], str] | None = None,
                store_cache_callback: Callable[[str, int], None] | None = None) -> dict[str, Any]:
    """按配置的名单来源读取当天订单，并通过接口批量创建预约单。

    名单来源（``config.sss_order_source``）：

    - ``wps``（默认）：下单前从 WPS 云端的「东湖中餐 / 东湖晚餐」读取当天
      （运行时刻 20:00 之后识别次日，其余时刻识别运行日；见
      ``sss_import.ordering_target_date``）标 ``1`` 的人，地址是「大西 / 小」的不下单；
      名单直接用内存数据下单，同时写一份到《闪时送.xlsx》留档。云端读不到、
      数据不完整或「识别日期 ≠ 本次送达日期」时**一律拒绝下单**（不登录、不提交）。
    - ``excel``：旧行为，读《闪时送.xlsx》（人工准备名单时的兜底）。

    ``decision_callback(identifier, error)`` 返回 ``retry``/``skip``/``stop``，
    用于单个订单创建失败时的交互决策（与订单处理的 order_decision 一致）。
    ``config.sss_dry_run`` 为真时只组装并打印报文，不真实提交（且跳过
    登录与门店/地址查询，无需验证码）。
    ``captcha_callback`` 在纯接口模式接收验证码 PNG 字节，返回用户输入的验证码。
    下单阶段并发提交（``config.sss_max_workers``，默认 4），提交前后均会
    查询站内订单列表；状态不确定时绝不直接重复 POST。
    """
    load_start = time.perf_counter()
    now = _dt.datetime.now()
    source = str(getattr(config, "sss_order_source", "wps") or "wps").strip().lower()
    if source != "excel":
        source = "wps"
    configured_excel = getattr(config, "sss_excel_path", None)
    excel_path = Path(configured_excel) if configured_excel else None
    import_summary: dict[str, Any] | None = None

    if source == "wps":
        # 云端当天名单：读云端 → 地址过滤 → 校验 → 留档；任何问题都在这里
        # 抛 ImportRefused，此时还没有登录、没有提交任何订单。
        day = prepare_day_orders(config, now=now,
                                 delivery_date=expected_delivery_date(now),
                                 log=progress_callback)
        orders_by_sheet = day.orders_by_sheet
        import_summary = day.as_summary()
    else:
        if excel_path is None:
            raise FileNotFoundError("尚未选择闪时送 Excel 文件")
        if not excel_path.is_file():
            raise FileNotFoundError(f"Excel 文件不存在: {excel_path}")
        if excel_path.suffix.lower() not in {".xlsx", ".xlsm"}:
            raise ValueError("仅支持 .xlsx 和 .xlsm Excel 文件")
        orders_by_sheet = load_sss_orders(excel_path)

    def _result(payload: dict[str, Any]) -> dict[str, Any]:
        """给结果补上名单来源与云端导入摘要（界面/日志用）。"""
        payload["source"] = source
        if import_summary is not None:
            payload["import"] = import_summary
        return payload

    _validate_sss_orders(orders_by_sheet)
    total = sum(len(orders) for orders in orders_by_sheet.values())
    if total == 0:
        if source == "wps":
            _emit(progress_callback,
                  "当天云端名单为空（没有当天日期列，或标 1 的人都是「大西/小」），本次不下单")
        else:
            _emit(progress_callback, "闪时送 Excel 中没有任何订单")
        return _result({"processed": 0, "created": 0, "status": "no_orders"})
    _emit(progress_callback,
          f"读取订单表耗时 {(time.perf_counter() - load_start):.1f} 秒，共 {total} 单")
    if password is None:
        password = ""
    dry_run = bool(getattr(config, "sss_dry_run", False))
    store_name = str(getattr(config, "sss_store_name", "") or "一口轻食")
    common_address = str(getattr(config, "sss_common_address", "") or "")
    use_fixed_address = bool(getattr(config, "sss_use_fixed_address", False))
    goods_name = str(getattr(config, "sss_product_name", "") or "轻食")
    try:
        max_workers = int(getattr(config, "sss_max_workers", 4))
    except (TypeError, ValueError):
        max_workers = 4
    max_workers = max(1, min(20, max_workers))
    batch_id = uuid.uuid4().hex[:12]
    idempotency_field = str(getattr(config, "sss_idempotency_field", "") or _CLIENT_IDEMPOTENCY_FIELD).strip()

    # 干跑短路：组装报文即返回，不取验证码、不登录、不查门店地址。
    try:
        read_timeout_s = float(getattr(config, "sss_read_timeout_s", 20.0))
    except (TypeError, ValueError):
        read_timeout_s = 20.0
    read_timeout_s = max(1.0, min(120.0, read_timeout_s))
    url = str(getattr(config, "sss_url", "") or "").strip() or DEFAULT_SSS_URL
    if dry_run:
        store_id = _cached_store_id(config, store_name) or 0
        if store_id:
            _emit(progress_callback, f"门店「{store_name}」命中缓存（id={store_id}）")
        if use_fixed_address:
            address = _fixed_address_from_config(config)
        else:
            _emit(progress_callback, "干跑模式：跳过常用地址查询，用占位地址组装报文")
            address = {"lnt": 0.0, "lat": 0.0, "areaCode": "",
                       "addressDetail": common_address or "干跑占位"}
        tasks = _collect_tasks(
            orders_by_sheet, store_id, address, goods_name,
            account=str(getattr(config, "sss_account", "") or ""),
            batch_id=batch_id, idempotency_field=idempotency_field, now=now)
        return _result(_run_dry_run(tasks, progress_callback))

    account = str(getattr(config, "sss_account", "") or "")
    if not account:
        raise ValueError("尚未填写闪时送账号")

    job_start = time.perf_counter()
    _emit(progress_callback, "正在获取闪时送验证码…")
    client = SssApiClient(url, account, password or "", timeout=(5.0, read_timeout_s),
                          pool_size=max_workers)
    try:
        captcha = client.fetch_captcha()
        if captcha_callback is None:
            raise RuntimeError("纯接口模式需要验证码输入回调，当前界面未提供 captcha 弹窗")
        code = captcha_callback(captcha)
        _emit(progress_callback, "正在登录闪时送…")
        client.login(code)

        def relogin() -> None:
            _emit(progress_callback, "登录态过期，重新获取验证码并登录…")
            img = client.fetch_captcha()
            if captcha_callback is None:
                raise RuntimeError("纯接口模式需要验证码输入回调")
            client.login(captcha_callback(img))
            _emit(progress_callback, "重新登录成功")

        def prepare_session_data() -> tuple[int, dict[str, Any], tuple[float | None, float | None]]:
            _emit(progress_callback, "登录成功，读取门店与常用地址…")
            prep_start = time.perf_counter()
            # 接口模式也串行：requests.Session 不保证线程安全，门店/地址两个
            # GET 并发共用同一 Session 会出现偶发失败。
            store_id, address = _prepare_store_and_address(
                client.get_json, config, store_name, common_address,
                use_fixed_address, progress_callback, parallel=False,
                store_cache_callback=store_cache_callback)
            _emit(progress_callback,
                  f"门店与地址准备耗时 {(time.perf_counter() - prep_start):.1f} 秒")
            balance = query_balance(client.get_json)
            return store_id, address, balance

        store_id, address, (balance_total, balance_frozen) = _with_auth_relogin(
            prepare_session_data, relogin, progress_callback, "读取门店/地址/余额")
        _emit(progress_callback, _format_balance(balance_total, balance_frozen))

        tasks = _collect_tasks(
            orders_by_sheet, store_id, address, goods_name,
            account=account, batch_id=batch_id, idempotency_field=idempotency_field,
            now=now)
        if idempotency_field:
            _emit(progress_callback, f"已启用客户端幂等字段「{idempotency_field}」")
        else:
            _emit(progress_callback, "平台接口未探测到客户端幂等字段：采用“至少一次提交 + 对账确认”语义，不承诺 exactly-once")
        try:
            unit_price = float(getattr(config, "sss_unit_price", 0) or 0)
        except (TypeError, ValueError):
            unit_price = 0
        if unit_price > 0:
            estimate = round(unit_price * len(tasks), 2)
            _emit(progress_callback,
                  f"本批 {len(tasks)} 单预计送完结算约 {estimate} 元"
                  + (f"，当前可用 {balance_total}"
                     if balance_total is not None else "，当前余额未知"))

        balance_status, estimate = _balance_precheck(
            tasks, balance_total, unit_price, progress_callback)
        if balance_status != "ok":
            return _result({
                "status": balance_status,
                "processed": len(tasks), "created": 0, "submitted": 0,
                "previewed": 0, "stopped": True, "partial": False,
                "reconciled": False, "uncertain": balance_status == "balance_unknown",
                "estimate": estimate,
                "semantics": "pre-submit-balance-guard",
            })

        if bool(getattr(config, "sss_preflight", False)):
            preflight_status, preflight = _preflight_tasks(
                tasks, client.get_json, progress_callback)
            return _result({
                "status": preflight_status,
                "processed": len(tasks),
                "created": len(preflight.confirmed) if preflight else 0,
                "submitted": 0, "previewed": 0, "stopped": preflight_status != "preflight_ok",
                "partial": False,
                "reconciled": preflight is not None,
                "uncertain": preflight is None,
                "balance_total": balance_total,
                "estimate": estimate,
                "semantics": "preflight-only",
            })

        _emit(progress_callback,
              f"开始下单：共 {len(tasks)} 单，并发 {max_workers} 路，读取超时 {read_timeout_s:g}s")
        submit_start = time.perf_counter()
        final, reconciled = _run_reconciled_submission(
            tasks,
            lambda: _make_api_submitter(client),
            client.get_json,
            stop_event,
            progress_callback,
            decision_callback,
            max_workers,
            relogin=relogin,
        )
        created = len(final.confirmed) if final is not None else 0
        processed = len(tasks)
        _emit(progress_callback,
              f"下单与对账耗时 {(time.perf_counter() - submit_start):.1f} 秒，"
              f"确认 {created}/{processed}")
        try:
            end_total, end_frozen = query_balance(client.get_json)
        except _AuthExpired:
            _emit(progress_callback, "余额查询时登录态失效，正在重新登录…")
            relogin()
            try:
                end_total, end_frozen = query_balance(client.get_json)
            except _AuthExpired as exc:
                _emit(progress_callback, f"重新登录后余额查询仍失败：{exc}", "WARN")
                end_total, end_frozen = None, None
        _emit(progress_callback, "结束" + _format_balance(end_total, end_frozen))
        stopped = bool(stop_event.is_set())
        partial = bool(reconciled and final is not None and final.missing and not stopped)
        if stopped:
            _emit(progress_callback,
                  f"闪时送任务已停止：{'已完成站内对账' if reconciled else '站内对账失败'}，"
                  f"确认 {created}/{processed}")
        elif partial:
            _emit(progress_callback,
                  f"闪时送任务部分完成：确认 {created}/{processed}，"
                  f"{processed - created} 单未完成")
        else:
            _emit(progress_callback,
                  f"闪时送下单完成：确认 {created}/{processed}，"
                  f"总耗时 {(time.perf_counter() - job_start):.1f} 秒")
        result: dict[str, Any] = {
            "processed": processed,
            "created": created,
            "stopped": stopped,
            "partial": partial,
            "reconciled": reconciled,
            "uncertain": not reconciled,
            "semantics": ("idempotency-key+reconciliation"
                          if idempotency_field else "at-least-once+reconciliation"),
        }
        if balance_total is not None:
            result["balance_total"] = balance_total
        if end_total is not None:
            result["balance_end"] = end_total
        return _result(result)
    finally:
        client.close()

    if stopped:
        _emit(progress_callback,
              f"闪时送任务已停止：{'已完成站内对账' if reconciled else '站内对账失败'}，"
              f"确认 {created}/{processed}")
    elif partial:
        _emit(progress_callback,
              f"闪时送任务部分完成：确认 {created}/{processed}，"
              f"{processed - created} 单未完成")
    else:
        _emit(progress_callback,
              f"闪时送下单完成：确认 {created}/{processed}，"
              f"总耗时 {(time.perf_counter() - job_start):.1f} 秒")
    return _result({"processed": processed, "created": created,
                    "stopped": stopped, "partial": partial, "reconciled": reconciled,
                    "uncertain": not reconciled,
                    "semantics": ("idempotency-key+reconciliation"
                                  if idempotency_field else "at-least-once+reconciliation")})
