"""闪时送下单过程的内部数据类与标记异常。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, NamedTuple


class _BalanceDepleted(LookupError):
    """余额不足：中断整批的标记异常（不重登、不补提、不重试）。"""

class _AuthExpired(RuntimeError):
    """worker 遇到 401 时交由主线程统一重登的标记异常。"""

class _SubmissionUncertain(RuntimeError):
    """POST 未得到可确认结果，必须先查站，不能直接重发。"""

@dataclass
class _SubmitResult:
    succeeded: set[str] = field(default_factory=set)
    failures: list[tuple[str, str]] = field(default_factory=list)
    uncertain: list[tuple[str, str]] = field(default_factory=list)
    auth_error: str = ""
    balance_error: str = ""
    stopped: bool = False

@dataclass
class _Reconciliation:
    confirmed: set[str]
    missing: list[dict[str, Any]]
    duplicate_count: int
    matched_count: int

class OrderFingerprint(NamedTuple):
    """订单身份指纹：仅凭姓名/电话/门牌/时间不再足够。"""

    receive_name: str = ""
    receive_phone: str = ""
    door_num: str = ""
    expected_delivery_time: str = ""
    account: str = ""
    store_id: str = ""
    goods_name: str = ""
    goods_num: str = ""
    address_detail: str = ""
    area_code: str = ""
    lnt: str = ""
    lat: str = ""
    order_type: str = ""

    def as_dict(self) -> dict[str, str]:
        """把结果对象转成普通 dict（供日志/回报使用）。"""
        return self._asdict()
