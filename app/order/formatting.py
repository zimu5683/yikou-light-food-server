"""订单处理日志的可读文本格式化。"""

from __future__ import annotations

from app.core.models import OrderInfo


def _format_order_meals(order: OrderInfo) -> str:
    parts: list[str] = []
    for meal_type, meals in (("午餐", order.lunch), ("晚餐", order.dinner)):
        for meal in meals:
            grade = meal.grade or "未标注"
            total = f"{meal.total_meals}餐" if meal.total_meals else "餐品"
            parts.append(f"{meal_type}{grade}{total} x{meal.count}")
    return "、".join(parts) if parts else "未识别"

def _format_address_change(order: OrderInfo) -> str:
    """Platform address rewritten into the sheet point, joined by an arrow.

    订单摘要里需要一眼看出「平台原地址被改成了排单用的哪个取餐点」：
    原地址与写入地址一致时（未改动或待确认）只显示原地址；有改动时
    显示 ``原地址 → 取餐点``，方便人工在日志里复核改写是否合理。
    """
    raw = (order.delivery_address or "").strip()
    point = (order.address or "").strip()
    if raw and point and raw != point:
        return f"{raw} → {point}"
    return point or raw or "未填写"

def _format_order_summary(order: OrderInfo, meal_text: str | None = None) -> str:
    """Render one compact, user-facing line for a successfully read order."""
    meals = meal_text if meal_text is not None else _format_order_meals(order)
    return "｜".join((
        order.order_no or "未知订单",
        order.name or "未填写",
        order.phone or "未填写",
        _format_address_change(order),
        meals,
    ))
