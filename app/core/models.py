"""公共业务数据模型（纯数据，可直接序列化）。

模型只保存订单/餐次等业务值，不依赖 HTTP 框架、openpyxl 或任何 UI/存储实现，
方便领域层与 API 层安全传递。
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional


@dataclass
class MealInfo:
    """A meal extracted from an order line."""

    total_meals: Optional[int] = None
    grade: Optional[str] = None
    count: int = 1
    meal_type: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """转成普通 dict（订单侧模型）。"""
        return asdict(self)


@dataclass
class OrderInfo:
    """Normalised order data consumed by the Excel writer."""

    order_no: str
    name: str = ""
    phone: str = ""
    address: str = ""
    address_base_sheet: Optional[str] = None
    lunch: List[MealInfo] = field(default_factory=list)
    dinner: List[MealInfo] = field(default_factory=list)
    # Original delivery address and arbitrary metadata are useful for logs.
    delivery_address: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """转成普通 dict（闪时送侧模型）。"""
        data = asdict(self)
        data["lunch"] = [m.to_dict() for m in self.lunch]
        data["dinner"] = [m.to_dict() for m in self.dinner]
        return data

