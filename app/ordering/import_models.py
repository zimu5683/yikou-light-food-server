"""云端名单导入的内部数据类与拒绝异常。"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from typing import Any


# 《闪时送.xlsx》的写入位置：A/B/C 三列，数据从第 3 行开始；日期写 E1。
COL_NAME, COL_ADDRESS, COL_PHONE = 1, 2, 3
FIRST_DATA_ROW = 3
DATE_ROW, DATE_COL = 1, 5          # E1
CELL_MARK = "1"


class ImportRefused(RuntimeError):
    """云端名单不可用或日期不匹配：调用方必须在下单之前中断。"""

@dataclass
class MealImport:
    """一餐（午餐/晚餐）的云端当天名单。"""

    meal: str                                   # 午餐 / 晚餐
    table: str = ""                             # 东湖中餐 / 东湖晚餐
    file_id: str = ""
    target: _dt.date | None = None
    date_text: str = ""                         # 云端日期列表头原文；"" = 没有当天列
    marked_total: int = 0                       # 该列标 1 的总人数
    skipped_address: int = 0                    # 其中因地址是大西/小被跳过的人数
    orders: list[dict[str, Any]] = field(default_factory=list)
    skipped: bool = False                       # 没有当天列 / 该表未配置 → 这一餐不下单
    skip_reason: str = ""
    warnings: list[str] = field(default_factory=list)

    @property
    def order_count(self) -> int:
        """该餐实际要下单的人数（已剔除被地址过滤掉的人）。"""
        return len(self.orders)

@dataclass
class DayOrders:
    """一次「当天名单」导入的完整结果。"""

    target_date: _dt.date
    meals: list[MealImport] = field(default_factory=list)
    orders_by_sheet: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    archive: dict[str, Any] = field(default_factory=dict)
    archive_error: str = ""

    @property
    def total(self) -> int:
        """两张表合计要下单的人数。"""
        return sum(len(orders) for orders in self.orders_by_sheet.values())

    @property
    def date_text(self) -> str:
        """留档 E1 用的日期原文（取第一张有当天列的表头原文）。"""
        for meal in self.meals:
            if meal.date_text:
                return meal.date_text
        return ""

    def as_summary(self) -> dict[str, Any]:
        """给界面/日志用的精简结构。"""
        return {
            "target_date": self.target_date.isoformat(),
            "date_text": self.date_text,
            "total": self.total,
            "date_gate": "ok",
            "meals": {
                meal.meal: {
                    "table": meal.table,
                    "marked": meal.marked_total,
                    "skipped_address": meal.skipped_address,
                    "orders": meal.order_count,
                    "date_text": meal.date_text,
                    "skipped": meal.skipped,
                    "reason": meal.skip_reason,
                    "warnings": list(meal.warnings),
                }
                for meal in self.meals
            },
            "archive": self.archive,
            "archive_error": self.archive_error,
        }
