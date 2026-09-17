"""管理后台订单的 Excel 写入与地址归一化落表。"""

from __future__ import annotations

import datetime as _dt
import re
from pathlib import Path
from typing import Any, Callable

from app.core.models import MealInfo, OrderInfo
from app.order.delivery import normalize_delivery_point
from app.order.parsing import get_address_base_sheet_name


SHEET_MEAL_SUFFIX = {"午餐": "中餐", "晚餐": "晚餐"}

WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")

HISTORICAL_SHEET_HEADERS = (
    "取单号", "姓名", "地址", "电话", *WEEKDAYS, "餐别", "经济/豪华", "总餐次",
)

PENDING_ADDRESS_HEADERS = (
    "取单号", "姓名", "地址", "电话", "餐别", "餐种", "餐次",
    "校区", "置信度", "原因", "候选点",
)

def _historical_sheet_name(target_date: _dt.date) -> str:
    return f"{target_date.year}年{target_date.month}月{target_date.day}日 {WEEKDAYS[target_date.weekday()]}"

def _write_historical_order(wb: Any, order: OrderInfo, meal: MealInfo, meal_type: str,
                            target_date: _dt.date) -> None:
    """Append an old order to its dedicated date sheet instead of today's plan."""
    sheet_name = _historical_sheet_name(target_date)
    sheet = wb[sheet_name] if sheet_name in wb.sheetnames else wb.create_sheet(sheet_name)
    if sheet.max_row == 1 and all(sheet.cell(1, index).value in (None, "")
                              for index in range(1, len(HISTORICAL_SHEET_HEADERS) + 1)):
        for index, title in enumerate(HISTORICAL_SHEET_HEADERS, 1):
            sheet.cell(1, index).value = title
    row = max(2, sheet.max_row + 1)
    while sheet.cell(row, 1).value not in (None, ""):
        row += 1
    weekday = WEEKDAYS[target_date.weekday()]
    values = [
        order.order_no, order.name, order.address, order.phone,
        *[1 if day == weekday else "" for day in WEEKDAYS],
        meal_type, meal.grade or "", meal.total_meals or "",
    ]
    for index, value in enumerate(values, 1):
        sheet.cell(row, index).value = value

def _write_order(wb: Any, order: OrderInfo, meal: MealInfo, meal_type: str,
                 target_date: _dt.date | None = None, today: _dt.date | None = None) -> None:
    """Write today's plan normally, or archive a selected historical date."""
    target_date = target_date or _dt.date.today()
    today = today or _dt.date.today()
    if target_date < today:
        _write_historical_order(wb, order, meal, meal_type, target_date)
        return
    base = order.address_base_sheet
    if not base:
        return
    weekday = WEEKDAYS[(today.weekday() + 1) % 7]
    weekday_sheet = wb[weekday] if weekday in wb.sheetnames else wb.create_sheet(weekday)
    target_name = f"{base}{SHEET_MEAL_SUFFIX.get(meal_type, meal_type)}"
    target = wb[target_name] if target_name in wb.sheetnames else wb.create_sheet(target_name)
    columns = ("A", "B", "C", "D", "E", "F") if meal_type == "午餐" else ("G", "H", "I", "J", "K", "L")
    # 中餐/晚餐两栏各自从第 3 行起连续填充：只找本栏首列（A 或 G）的空行，
    # 不再以整表 max_row 定位——否则两栏互相把对方顶到下一行，形成对角错位。
    row = 3
    while weekday_sheet[f"{columns[0]}{row}"].value not in (None, ""):
        row += 1
    values = (order.order_no, order.name, order.address, order.phone, meal.grade or "", meal.total_meals or "")
    for col, value in zip(columns, values):
        weekday_sheet[f"{col}{row}"] = value
    # 总餐区（N~S）逐条汇总当天每一餐：与两栏一样从第 3 行起连续向下。
    total_columns = ("N", "O", "P", "Q", "R", "S")
    total_row = 3
    while weekday_sheet[f"{total_columns[0]}{total_row}"].value not in (None, ""):
        total_row += 1
    for col, value in zip(total_columns, values):
        weekday_sheet[f"{col}{total_row}"] = value
    row2 = max(3, target.max_row + 1)
    while target[f"A{row2}"].value not in (None, ""):
        row2 += 1
    # 「类型」列沿用表名后缀（中餐/晚餐），与工作簿里手工维护的行保持一致；
    # 例如写入「衣锦中餐」表时类型填「中餐」，而不是接口分类「午餐」。
    type_label = SHEET_MEAL_SUFFIX.get(meal_type, meal_type)
    vals = [order.order_no, order.name, order.address, order.phone] + [1 if d == weekday else "" for d in WEEKDAYS] + [type_label, meal.grade or "", meal.total_meals or ""]
    for idx, value in enumerate(vals, 1):
        target.cell(row2, idx).value = value

def _write_unrouted_order(wb: Any, order: OrderInfo, meal: MealInfo, meal_type: str) -> None:
    """Write an order whose campus is unknown to a dedicated review sheet."""
    sheet = wb["待确认地址"] if "待确认地址" in wb.sheetnames else wb.create_sheet("待确认地址")
    if sheet.max_row == 1 and all(sheet.cell(1, i).value in (None, "")
                                  for i in range(1, len(PENDING_ADDRESS_HEADERS) + 1)):
        for i, title in enumerate(PENDING_ADDRESS_HEADERS, 1):
            sheet.cell(1, i).value = title
    row = max(2, sheet.max_row + 1)
    while sheet.cell(row, 1).value not in (None, ""):
        row += 1
    dp = order.metadata.get("delivery_point") or {}
    values = [
        order.order_no, order.name, order.delivery_address or order.address, order.phone,
        meal_type, meal.grade or "", meal.total_meals or "", dp.get("campus", "未知"),
        dp.get("confidence", "unknown"), dp.get("reason", ""),
        "、".join((dp.get("candidates") or {}).keys()),
    ]
    for i, value in enumerate(values, 1):
        sheet.cell(row, i).value = value

def _prepare_order_address(order: OrderInfo, aliases: dict[str, str]) -> dict[str, Any]:
    """Apply canonical address rules while retaining the platform address."""
    raw = order.delivery_address or order.address or ""
    result = normalize_delivery_point(raw, aliases=aliases)
    campus = result.get("campus")
    base = order.address_base_sheet
    if campus == "东湖农林":
        base = "东湖"
    elif campus == "医学院":
        base = "医学院"
    elif campus == "衣锦联建":
        base = "衣锦"
    order.delivery_address = raw
    order.address_base_sheet = base
    if campus == "衣锦联建":
        point = order.address or "校门口"
        result = {
            **result, "point": point, "confidence": "high",
            "reason": "衣锦沿用商品备注规则", "candidates": {point: 1},
        }
    order.metadata["delivery_point"] = result
    if campus in {"东湖农林", "医学院"}:
        order.address = result.get("point") or raw
    elif campus == "衣锦联建":
        order.address = order.address or "校门口"
    else:
        order.address = raw
    return result

def _pending_report_items(orders: list[OrderInfo]) -> list[dict[str, Any]]:
    """Deduplicate pending addresses while retaining every affected order."""
    grouped: dict[str, dict[str, Any]] = {}
    for order in orders:
        dp = order.metadata.get("delivery_point") or {}
        raw = order.delivery_address or order.address or ""
        item = grouped.setdefault(raw, {
            "order_numbers": [], "campus": dp.get("campus", "未知"),
            "raw_address": raw, "confidence": dp.get("confidence", "unknown"),
            "reason": dp.get("reason", ""), "candidates": dp.get("candidates") or {},
            "suggested_point": dp.get("point") or "",
        })
        if order.order_no not in item["order_numbers"]:
            item["order_numbers"].append(order.order_no)
    return list(grouped.values())

_MANUAL_CAMPUS_TO_BASE = {
    "东湖农林": "东湖", "医学院": "医学院", "衣锦联建": "衣锦",
}

def _manual_address_base_sheet(order: OrderInfo, value: str) -> str:
    """手动填写的地址优先按自身校区/点位判断，其次沿用订单原校区。"""
    detected = get_address_base_sheet_name(value)
    if detected:
        return detected
    if order.address_base_sheet:
        return order.address_base_sheet
    dp = order.metadata.get("delivery_point") or {}
    base = _MANUAL_CAMPUS_TO_BASE.get(str(dp.get("campus") or ""), "")
    if base:
        return base
    # 用户直接输入排单短名时，按点位自身推断校区。
    compact = re.sub(r"\s+", "", value)
    if re.fullmatch(r"[A-Da-d]\d{1,2}", compact):
        return "东湖"
    if re.fullmatch(r"医\d+号", compact):
        return "医学院"
    return ""

def _load_order_workbook(excel_path: Path, loader: Callable[..., Any] | None = None) -> Any:
    if loader is None:
        from openpyxl import load_workbook
        loader = load_workbook
    return loader(excel_path, keep_vba=excel_path.suffix.lower() == ".xlsm")

def _save_workbook_with_retry(workbook: Any, excel_path: Path,
                              decision_callback: Callable[[str], str] | None = None) -> None:
    """Save an Excel workbook, allowing the user to close a locked file and retry."""
    while True:
        try:
            workbook.save(str(excel_path))
            return
        except PermissionError as exc:
            if decision_callback is None:
                raise
            decision = decision_callback(str(exc)).strip().lower()
            if decision not in {"retry", "重试", "再次保存"}:
                raise PermissionError(f"已取消保存 Excel 文件：{excel_path}") from exc
