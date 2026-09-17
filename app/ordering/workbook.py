"""闪时送本地 Excel 的读取、校验与送达时间计算。"""

from __future__ import annotations

import datetime as _dt
import unicodedata
from pathlib import Path
from typing import Any

from app.ordering.common import _clean
from app.ordering.constants import (
    DEFAULT_SHEETS,
    DINNER_TIME,
    LUNCH_TIME,
    _BLANK_ROWS_TO_STOP,
    _DOOR_RE,
    _PHONE_RE,
)


def load_sss_orders(excel_path: str | Path, sheets=DEFAULT_SHEETS) -> dict[str, list[dict[str, Any]]]:
    """读取《闪时送.xlsx》的订单（A=姓名 B=门牌号 C=电话 D=送达时间）。

    每张工作表从第 3 行开始，连续 ``_BLANK_ROWS_TO_STOP`` 个 A/B/C 全空
    的行才终止（兼容中间偶发空行）。使用 ``read_only`` + ``iter_rows``
    批量取值，避免逐格 ``ws[f"A{n}"]`` 的开销。返回
    ``{工作表名: [订单字典, ...]}``，每个订单含 ``row`` 与四列原始值。
    """
    from openpyxl import load_workbook

    wb = load_workbook(excel_path, data_only=True, read_only=True)
    result: dict[str, list[dict[str, Any]]] = {}
    try:
        for sheet_name in sheets:
            if sheet_name not in wb.sheetnames:
                continue
            ws = wb[sheet_name]
            orders: list[dict[str, Any]] = []
            blank_streak = 0
            for row_num, row in enumerate(
                ws.iter_rows(min_row=3, max_col=4, values_only=True), start=3
            ):
                name = _clean(row[0]) if len(row) > 0 else None
                door = _clean(row[1]) if len(row) > 1 else None
                phone = _clean(row[2]) if len(row) > 2 else None
                delivery_time = row[3] if len(row) > 3 else None
                if not any([name, door, phone]):
                    blank_streak += 1
                    if blank_streak >= _BLANK_ROWS_TO_STOP:
                        break
                    continue
                blank_streak = 0
                orders.append({
                    "row": row_num,
                    "name": name,
                    "door": door,
                    "phone": phone,
                    "delivery_time": delivery_time,
                })
            result[sheet_name] = orders
        return result
    finally:
        wb.close()

def _normalise_phone(value: Any) -> str:
    """把 Excel 电话字段规范成 11 位 ASCII 数字，拒绝含糊格式。"""
    if isinstance(value, bool) or value is None:
        return ""
    if isinstance(value, int):
        text = str(value)
    elif isinstance(value, float):
        # openpyxl 会把没有文本格式的整数字段读成 float；仅接受精确整数，
        # 防止 138... .5 一类值被悄悄截断后发送到平台。
        if not value.is_integer():
            return ""
        text = str(int(value))
    else:
        text = unicodedata.normalize("NFKC", str(value)).strip()
    return _DOOR_RE.sub("", text)

def _validate_sss_orders(orders_by_sheet: dict[str, list[dict[str, Any]]]) -> None:
    """校验并规范所有订单，任何一行异常都阻止整批继续。"""
    invalid: list[str] = []
    for sheet_name, orders in orders_by_sheet.items():
        for order in orders:
            name = unicodedata.normalize("NFKC", str(order.get("name") or "")).strip()
            door = unicodedata.normalize("NFKC", str(order.get("door") or "")).strip()
            phone = _normalise_phone(order.get("phone"))
            missing = []
            if not name:
                missing.append("姓名")
            if not door:
                missing.append("门牌")
            if not _PHONE_RE.fullmatch(phone):
                missing.append("11 位电话")
            if missing:
                invalid.append(f"{sheet_name}第 {order.get('row', '?')} 行（{'、'.join(missing)}）")
                continue
            order["name"] = name
            order["door"] = door
            order["phone"] = phone
    if invalid:
        raise ValueError("闪时送 Excel 存在无效订单：" + "；".join(invalid))

def compute_delivery_time(is_dinner: bool, now: _dt.datetime | None = None) -> str:
    """按原脚本规则计算送达时间：午餐 11:00 / 晚餐 17:00，16 点后顺延次日。"""
    now = now or _dt.datetime.now()
    target_date = now
    hour = now.hour
    if 20 <= hour < 24 or 16 <= hour < 20:  # 原脚本的等价写法：16~23 点顺延次日
        target_date = now + _dt.timedelta(days=1)
    time_str = DINNER_TIME if is_dinner else LUNCH_TIME
    return target_date.strftime("%Y-%m-%d ") + time_str

def expected_delivery_date(now: _dt.datetime | None = None) -> _dt.date:
    """本次下单会给平台报的送达日期。

    午餐与晚餐使用同一条顺延规则，因此只有时间不同、日期相同；云端名单的
    「识别日期」必须与它一致，否则拒绝下单（见 ``sss_import.check_target_day``）。
    """
    return _dt.date.fromisoformat(compute_delivery_time(False, now)[:10])
