"""管理后台订单处理：HTTP 抓单、详情合并、地址归一化与写排单表。

调用方传入 :class:`AppConfig`、口令与取消事件；本模块不保存凭据，也不依赖
任何浏览器/桌面运行时。平台请求全部经由
:class:`app.integrations.api_client.AdminApiClient`。
"""
from __future__ import annotations

import datetime as _dt

import shutil
import time
from pathlib import Path
from typing import Any, Callable

from app.core.models import OrderInfo
from app.order.parsing import clear_campus_sub_sheets, parse_meal_rows, sort_campus_sub_sheets
from app.order.fetching import (
    ORDER_STATE_REFUND_APPLIED, ORDER_STATE_REFUND_DONE, _api_list_waimai_orders,
    _filter_rows_by_date, _group_orders_by_pick, _order_detail_by_id,
    _order_from_api_data, _order_numbers_for_date, _prefetch_order_details,
    parse_order_created_date, parse_target_date, split_refund_orders,
)
from app.order.excel_io import (
    HISTORICAL_SHEET_HEADERS, PENDING_ADDRESS_HEADERS, SHEET_MEAL_SUFFIX, WEEKDAYS,
    _MANUAL_CAMPUS_TO_BASE, _historical_sheet_name, _load_order_workbook,
    _manual_address_base_sheet, _pending_report_items, _prepare_order_address,
    _save_workbook_with_retry, _write_historical_order, _write_order, _write_unrouted_order,
)
from app.order.formatting import (
    _format_address_change, _format_order_meals, _format_order_summary,
)

from app.integrations.api_client import AdminApiClient
from app.order.common import _emit
from app.order.aliases import aliases_path, load_aliases, write_pending
from app.order.delivery import DEFAULT_ALIASES


# 订单列表接口 state 取值（2026-09-11 抓包确认）：state=8 为已退款（同意后退款
# 成功），state=7 为「用户申请退款」即退款待审批（商家还没同意，对应界面上的
# 「退款详情/申请退款」）。这两种订单都不应写入排班表格。












































def run_job(config: Any, order_count: int | None, stop_event: Any, progress_callback: Callable[[str], Any] | None = None, password: str | None = None,
            order_decision_callback: Callable[[str, str], str] | None = None,
            save_decision_callback: Callable[[str], str] | None = None,
            pending_address_callback: Callable[[list[dict[str, Any]]], dict[str, str]] | None = None,
            target_date: object = None) -> dict[str, Any]:
    """Process the newest W orders and append their meals to the workbook.

    ``order_count`` 为空或 0 时表示自动处理目标日期当天的全部订单。
    ``pending_address_callback`` 在写入 Excel 前收到去重后的待确认地址，
    返回 ``{原始地址: 用户填写的最终地址}``；返回空串/缺失的写法保留待确认流程。
    """
    configured_excel = getattr(config, "excel_path", None)
    if not configured_excel:
        raise FileNotFoundError("尚未选择 Excel 文件")
    excel_path = Path(configured_excel)
    if not excel_path.is_file():
        raise FileNotFoundError(f"Excel 文件不存在: {excel_path}")
    if excel_path.suffix.lower() not in {".xlsx", ".xlsm"}:
        raise ValueError("仅支持 .xlsx 和 .xlsm Excel 文件")
    selected_date = parse_target_date(
        target_date if target_date is not None else getattr(config, "order_date", "")
    )
    today = _dt.date.today()
    if password is None:
        password = getattr(config, "password", "")
    # Validate aliases before backing up or touching the workbook.
    user_aliases = load_aliases()
    _emit(progress_callback,
          f"地址别名：{aliases_path()}（用户 {len(user_aliases)} 条，内置 {len(DEFAULT_ALIASES)} 条）")
    backup_dir = excel_path.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = _dt.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    shutil.copy2(excel_path, backup_dir / f"{excel_path.stem}_{stamp}{excel_path.suffix}")
    # Preserve embedded VBA when the user explicitly selects an .xlsm file.
    wb = _load_order_workbook(excel_path)
    processed = 0
    found = 0
    # 退款订单汇总（外层容器供结尾统一打印）：申请退款中 / 已退款 两类 W 编号。
    refunded_numbers: list[int] = []
    applied_numbers: list[int] = []
    pending_orders: list[OrderInfo] = []

    def process_orders(api_get: Callable[[str], dict[str, Any]]) -> None:
        nonlocal processed, refunded_numbers, applied_numbers, pending_orders
        list_start = time.perf_counter()
        rows = _api_list_waimai_orders(api_get, selected_date, progress_callback,
                                        concurrent_pages=True)
        _emit(progress_callback,
              f"拉取订单列表耗时 {(time.perf_counter() - list_start):.1f} 秒")

        # 先按目标日期筛选，再遍历当天实际存在的订单，避免先遍历全部 W 编号。
        rows_by_pick = _group_orders_by_pick(_filter_rows_by_date(rows, selected_date))
        numbers = _order_numbers_for_date(rows_by_pick, order_count)
        if not numbers:
            _emit(progress_callback,
                  f"目标日期 {selected_date.isoformat()} 没有找到订单，未写入 Excel")
            return
        # 退款订单（state=7 申请退款中 / state=8 已退款）不写入表格：从实际处理
        # 列表剔除，但保留编号用于结尾日志汇总「退款成功 / 申请退款中」两类。
        normal_numbers, refunded_numbers, applied_numbers = split_refund_orders(
            rows_by_pick, numbers)
        refund_label = []
        if refunded_numbers:
            refund_label.append(f"已退款 {len(refunded_numbers)} 单："
                                + "、".join(f"W{n}" for n in refunded_numbers))
        if applied_numbers:
            refund_label.append(f"申请退款中 {len(applied_numbers)} 单："
                                + "、".join(f"W{n}" for n in applied_numbers))
        if order_count:
            _emit(progress_callback,
                  f"已按目标日期筛选，共 {len(numbers)} 个订单待处理："
                  + "、".join(f"W{n}" for n in numbers))
        else:
            _emit(progress_callback,
                  f"留空模式：自动识别目标日期订单，共 {len(numbers)} 个："
                  + "、".join(f"W{n}" for n in numbers))
        if refund_label:
            _emit(progress_callback, "；".join(refund_label) + "，已跳过不写入表格")
        if not normal_numbers:
            # 目标日期的订单全部处于退款状态（申请中或已退款），无需拉详情与写表。
            _emit(progress_callback, "目标日期订单全部为退款状态，未写入 Excel")
            return

        # 纯接口模式下并发预取订单详情；失败项仍走下方串行重试/决策流程。
        # 退款订单不预取也不写表，只对正常订单发起。
        prefetched: dict[int, OrderInfo] | None = None
        # 纯 HTTP 接口：多页并发拉取，缩短大订单列表的读取时间。
        if len(normal_numbers) > 1:
            detail_start = time.perf_counter()
            prefetched = _prefetch_order_details(api_get, rows_by_pick, normal_numbers, max_workers=5)
            _emit(progress_callback,
                  f"并发预取订单详情完成：成功 {len(prefetched)}/{len(normal_numbers)}，"
                  f"耗时 {(time.perf_counter() - detail_start):.1f} 秒")

        def commit_order(order: OrderInfo, *, pending: bool) -> None:
            nonlocal found
            written = 0
            for typ, meals in (("午餐", order.lunch), ("晚餐", order.dinner)):
                for meal in meals:
                    for _ in range(max(1, meal.count)):
                        if pending and not order.address_base_sheet:
                            _write_unrouted_order(wb, order, meal, typ)
                        else:
                            _write_order(wb, order, meal, typ, target_date=selected_date, today=today)
                        written += 1
            meal_text = _format_order_meals(order)
            _emit(progress_callback, _format_order_summary(order, meal_text))
            if written:
                found += 1
            else:
                _emit(progress_callback, f"{order.order_no} 未识别到午餐或晚餐，未写入表格")
            _emit(progress_callback, "-------")

        write_start = time.perf_counter()
        collected: list[OrderInfo] = []
        for number in normal_numbers:
            if stop_event.is_set():
                break
            code = f"W{number}"

            # 已并发预取成功：直接复用，跳过详情请求。
            if prefetched is not None and number in prefetched:
                processed += 1
                collected.append(prefetched[number])
                continue

            order: OrderInfo | None = None
            candidates = list(rows_by_pick.get(code, []))
            while candidates and not stop_event.is_set():
                entry = candidates.pop(0)
                try:
                    order = _order_detail_by_id(api_get, code, entry["order_id"], entry["store_id"])
                    if order is None:
                        raise LookupError(
                            f"订单 {code}（接口编号 {entry['order_id']}）未读到姓名/电话/地址/餐品")
                except Exception as exc:
                    _emit(progress_callback, f"{code} 处理失败：{exc}")
                    decision = "skip"
                    if order_decision_callback:
                        decision = order_decision_callback(code, str(exc)).lower()
                    if decision == "retry":
                        _emit(progress_callback, f"重试 {code}")
                        candidates.insert(0, entry)
                        continue
                    if decision == "stop":
                        _emit(progress_callback, f"{code} 已选择停止，本轮结束")
                        stop_event.set()
                        break
                    _emit(progress_callback, f"{code} 未找到，跳过")
                    order = None
                    break
                break
            if stop_event.is_set():
                break
            processed += 1
            if order is None:
                _emit(progress_callback, "-------")
                continue
            collected.append(order)

        certain_orders: list[OrderInfo] = []
        pending_orders = []
        for order in collected:
            result = _prepare_order_address(order, user_aliases)
            is_pending = result.get("confidence") != "high" or not order.address_base_sheet
            if is_pending:
                order.address = order.delivery_address
                pending_orders.append(order)
            else:
                certain_orders.append(order)

        # 写表前先让用户在对话框中手动填写待确认地址；确认后视为 high，
        # 直接按最终地址写入对应校区表，后续地址排序会自动归位。
        if pending_orders and pending_address_callback is not None and not stop_event.is_set():
            pending_items = _pending_report_items(pending_orders)
            _emit(progress_callback,
                  f"检测到 {len(pending_items)} 种地址无法自动识别，等待手动填写…")
            try:
                overrides = pending_address_callback(pending_items)
            except Exception as exc:
                overrides = {}
                _emit(progress_callback, f"手动填写地址已取消或失败：{exc}")
            if not isinstance(overrides, dict):
                overrides = {}
            unresolved: list[OrderInfo] = []
            resolved_count = 0
            for order in pending_orders:
                raw = (order.delivery_address or order.address or "").strip()
                value = str(overrides.get(raw, "") or "").strip()
                if not value:
                    unresolved.append(order)
                    continue
                base = _manual_address_base_sheet(order, value)
                if not base:
                    unresolved.append(order)
                    _emit(progress_callback,
                          f"{order.order_no} 手动填写「{value}」无法判断校区，仍保留在待确认地址")
                    continue
                order.address = value
                order.address_base_sheet = base
                dp = order.metadata.setdefault("delivery_point", {})
                dp.update({
                    "point": value,
                    "confidence": "high",
                    "reason": "用户手动填写",
                    "candidates": {value: 1},
                })
                certain_orders.append(order)
                resolved_count += 1
            pending_orders = unresolved
            if resolved_count:
                _emit(progress_callback, f"已按手动填写修正 {resolved_count} 个订单的地址")

        # 每次写入前先清空六张校区子表第 2 行后的旧数据，避免新旧混排；
        # 清空只发生在确认当天有订单要写之后（numbers 为空已提前返回）。
        try:
            if clear_campus_sub_sheets(wb):
                _emit(progress_callback, "已清空六张校区子表旧数据")
        except Exception as exc:  # 清空失败则按原样追加写入，避免丢单。
            _emit(progress_callback, f"清空旧数据失败（已跳过，继续写入）：{exc}")

        for order in certain_orders:
            commit_order(order, pending=False)
        for order in pending_orders:
            dp = order.metadata.get("delivery_point") or {}
            _emit(progress_callback,
                  f"{order.order_no} 地址待确认（{dp.get('reason') or '无法确定点位'}），"
                  "已以原始地址追加到表尾")
            commit_order(order, pending=True)

        if not stop_event.is_set():
            _emit(progress_callback,
                  f"写入 Excel 耗时 {(time.perf_counter() - write_start):.1f} 秒")

    try:
        _emit(progress_callback, "正在通过接口登录管理后台…")
        client = AdminApiClient(
            str(getattr(config, "target_url", getattr(config, "url", ""))),
            str(getattr(config, "phone_number", getattr(config, "phone", "")) or ""),
            password or "",
        )
        client.login()
        _emit(progress_callback, "登录成功，开始处理订单")

        process_orders(client.get_json)

        report_items = _pending_report_items(pending_orders)
        report_path = write_pending(report_items, target_date=selected_date)
        _emit(progress_callback, f"待确认地址报告：{report_path}（{len(report_items)} 种写法）")
        try:
            if sort_campus_sub_sheets(wb):
                _emit(progress_callback, "已按地址整理")
        except Exception as exc:  # 排序失败不应阻断主流程，原样保存并提示。
            _emit(progress_callback, f"地址排序失败（已跳过，按原顺序保存）：{exc}")
        _save_workbook_with_retry(wb, excel_path, save_decision_callback)
    finally:
        wb.close()
    _emit(progress_callback, f"处理完成：找到 {found}/{processed} 个订单")
    # 结尾统一汇总退款订单：哪些已退款、哪些仍在申请退款中（均为本次目标日期内）。
    if refunded_numbers or applied_numbers:
        _emit(progress_callback, "----------")
        if refunded_numbers:
            _emit(progress_callback, "退款成功（已退款，不排单）："
                  + "、".join(f"W{n}" for n in refunded_numbers))
        if applied_numbers:
            _emit(progress_callback, "还在申请退款（待审批，不排单）："
                  + "、".join(f"W{n}" for n in applied_numbers))
    pending_numbers = [order.order_no for order in pending_orders]
    if pending_numbers:
        _emit(progress_callback, "待确认地址（已用原始地址写到表尾）：" + "、".join(pending_numbers))
    return {"processed": processed, "found": found,
            "refunded": refunded_numbers, "refund_applied": applied_numbers,
            "address_pending": len(pending_numbers),
            "address_pending_orders": pending_numbers}

__all__ = [
    "AdminApiClient",
    "DEFAULT_ALIASES",
    "HISTORICAL_SHEET_HEADERS",
    "ORDER_STATE_REFUND_APPLIED",
    "ORDER_STATE_REFUND_DONE",
    "OrderInfo",
    "PENDING_ADDRESS_HEADERS",
    "SHEET_MEAL_SUFFIX",
    "WEEKDAYS",
    "_MANUAL_CAMPUS_TO_BASE",
    "_api_list_waimai_orders",
    "_emit",
    "_filter_rows_by_date",
    "_format_address_change",
    "_format_order_meals",
    "_format_order_summary",
    "_group_orders_by_pick",
    "_historical_sheet_name",
    "_load_order_workbook",
    "_manual_address_base_sheet",
    "_order_detail_by_id",
    "_order_from_api_data",
    "_order_numbers_for_date",
    "_pending_report_items",
    "_prefetch_order_details",
    "_prepare_order_address",
    "_save_workbook_with_retry",
    "_write_historical_order",
    "_write_order",
    "_write_unrouted_order",
    "aliases_path",
    "clear_campus_sub_sheets",
    "load_aliases",
    "parse_meal_rows",
    "parse_order_created_date",
    "parse_target_date",
    "run_job",
    "sort_campus_sub_sheets",
    "split_refund_orders",
    "write_pending",
]
