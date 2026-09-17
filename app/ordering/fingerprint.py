"""闪时送订单指纹：归一化与多字段兼容匹配。

订单列表/下单报文的字段结构存在多种历史形态，指纹用于回答“站内这条订单是不是
本批刚提交的那一单”，不能只看姓名+电话+门牌+时间。
"""

from __future__ import annotations

import datetime as _dt
import re
import unicodedata
from typing import Any

from app.ordering.constants import _DOOR_RE
from app.ordering.models import OrderFingerprint
from app.ordering.records import _pick, _pick_scalar, _unwrap_scalar


def _normalise_text(value: Any, *, compact: bool = False) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).strip().casefold()
    return _DOOR_RE.sub("", text) if compact else text

def _normalise_number(value: Any) -> str:
    """把门店/商品数量/坐标规范成稳定字符串，避免 1 与 1.0 误判不同。"""
    if value in (None, ""):
        return ""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return _normalise_text(value)
    if not (number == number and number not in (float("inf"), float("-inf"))):  # NaN/Inf
        return _normalise_text(value)
    if number.is_integer():
        return str(int(number))
    return f"{number:.7f}".rstrip("0").rstrip(".")

def _normalise_int_text(value: Any) -> str:
    if value in (None, ""):
        return ""
    return _normalise_number(value)

def _normalise_delivery_time(value: Any) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, (int, float)) or re.fullmatch(r"\d{10,13}", str(value).strip()):
        number = float(value)
        if number > 10_000_000_000:
            number /= 1000.0
        try:
            parsed = _dt.datetime.fromtimestamp(number, tz=_dt.timezone(_dt.timedelta(hours=8)))
        except (OverflowError, OSError, ValueError):
            return str(value)
        return parsed.strftime("%Y-%m-%d %H:%M:%S")
    text = unicodedata.normalize("NFKC", str(value or "")).strip().replace("T", " ")
    if not text:
        return ""
    try:
        parsed = _dt.datetime.fromisoformat(text)
    except ValueError:
        return text
    return parsed.strftime("%Y-%m-%d %H:%M:%S")

def _normalise_order_type(value: Any) -> str:
    if value in (None, ""):
        return ""
    numeric = _normalise_int_text(value)
    if numeric.isdigit():
        return numeric
    text = _normalise_text(value)
    return {"预约单": "2", "即时单": "1", "外卖单": "1", "自取单": "3"}.get(text, text)

def _payload_goods(payload: dict[str, Any]) -> tuple[str, str]:
    goods = payload.get("goodsDetail")
    if isinstance(goods, list) and goods and isinstance(goods[0], dict):
        item = goods[0]
        return (_normalise_text(item.get("goodsName")),
                _normalise_int_text(item.get("goodsNum")))
    return "", ""

def _record_goods(record: dict[str, Any]) -> tuple[str, str]:
    for key in ("goodsDetail", "goods", "generalGoods", "products", "productList"):
        items = record.get(key)
        if not isinstance(items, list) or not items:
            continue
        first = items[0]
        if not isinstance(first, dict):
            continue
        name = _pick(first, ("goodsName", "name", "title", "productName", "goods_title"))
        num = _pick(first, ("goodsNum", "num", "count", "quantity", "goods_num"))
        if name not in (None, "") or num not in (None, ""):
            return _normalise_text(name), _normalise_int_text(num)
    # 兼容把商品名/数量拍平在订单顶层字段的响应。
    return (_normalise_text(_pick(record, ("goodsName", "goods_name", "productName"))),
            _normalise_int_text(_pick(record, ("goodsNum", "goods_num", "goodsCount"))))

def _record_address(record: dict[str, Any]) -> dict[str, Any]:
    address = record.get("receiveAddress")
    if not isinstance(address, dict):
        address = record.get("address") if isinstance(record.get("address"), dict) else {}
    return address

_FLAT_ADDRESS_KEYS = (
    "recipientAddress", "receiverAddress", "address", "addressDetail",
    "detailedAddress", "deliveryAddress", "position",
)

def _record_address_text(record: dict[str, Any]) -> str:
    """取拍平成一整串的收货地址（订单列表把地址拍平成 ``recipientAddress``）。

    新结构（2026-09 实测）列表记录没有 ``receiveAddress`` 子对象，只有
    ``recipientAddress`` 字符串，门牌号也被拼进这一串，因此这里原样返回，
    由对账阶段与下单报文的 ``addressDetail + doorNum`` 组合后比较。
    """
    if _record_address(record):
        return ""
    for key in _FLAT_ADDRESS_KEYS:
        value = _unwrap_scalar(record.get(key))
        if isinstance(value, str) and value.strip():
            return _normalise_text(value)
    return ""

def _record_region_code(address: dict[str, Any]) -> str:
    value = _pick(address, ("areaCode", "adcode", "area_code", "regionCode", "code"))
    if value not in (None, ""):
        return _normalise_text(value)
    region = address.get("regionId")
    if isinstance(region, list) and region:
        return _normalise_text(region[-1])
    return ""

def _payload_fingerprint(payload: dict[str, Any], *, account: str = "") -> OrderFingerprint:
    """从下单请求体生成完整订单指纹。"""
    address = payload.get("receiveAddress")
    address = address if isinstance(address, dict) else {}
    goods_name, goods_num = _payload_goods(payload)
    return OrderFingerprint(
        receive_name=_normalise_text(payload.get("receiveName")),
        receive_phone=_normalise_text(payload.get("receivePhone"), compact=True),
        door_num=_normalise_text(address.get("doorNum"), compact=True),
        expected_delivery_time=_normalise_delivery_time(payload.get("expectedDeliveryTime")),
        account=_normalise_text(account, compact=True),
        store_id=_normalise_int_text(payload.get("storeId")),
        goods_name=goods_name,
        goods_num=goods_num,
        address_detail=_normalise_text(address.get("addressDetail")),
        area_code=_normalise_text(address.get("areaCode")),
        lnt=_normalise_number(address.get("lnt")),
        lat=_normalise_number(address.get("lat")),
        order_type=_normalise_int_text(payload.get("orderType")),
    )

def _legacy_fingerprint(value: tuple[str, str, str, str]) -> OrderFingerprint:
    return OrderFingerprint(
        receive_name=value[0], receive_phone=value[1], door_num=value[2],
        expected_delivery_time=value[3],
    )

def _task_fingerprint(task: dict[str, Any]) -> OrderFingerprint:
    value = task.get("fingerprint")
    if isinstance(value, OrderFingerprint):
        return value
    if isinstance(value, tuple) and len(value) == 4:
        return _legacy_fingerprint(value)  # type: ignore[arg-type]
    return _payload_fingerprint(task["payload"], account=str(task.get("account") or ""))

def _order_record_fingerprint(record: dict[str, Any], *, account: str = "") -> OrderFingerprint:
    """从站内列表记录提取完整指纹；缺失字段留空，由对账阶段判定。

    列表接口（2026-09 实测）返回的是概要结构：姓名/电话/地址为
    ``recipientName``/``recipientPhone``(数组)/``recipientAddress``(整串)，
    且通常不带 ``storeId``/``goodsDetail``/坐标。缺失的字段留空，由
    ``_reconcile_tasks`` 区分“字段未返回”与“值相冲突”。
    """
    address = _record_address(record)
    flat_address = _record_address_text(record)
    user = record.get("user") if isinstance(record.get("user"), dict) else {}
    store = record.get("store") if isinstance(record.get("store"), dict) else {}
    goods_name, goods_num = _record_goods(record)
    return OrderFingerprint(
        receive_name=_normalise_text(_pick_scalar(record, (
            "receiveName", "receiverName", "recipientName", "consigneeName",
            "contactName", "contacts", "recipient", "consignee", "receiver", "name")) or
            _pick(address, ("contact", "contactName", "name"))),
        receive_phone=_normalise_text(_pick_scalar(record, (
            "receivePhone", "receiverPhone", "recipientPhone", "consigneePhone",
            "contactPhone", "recipientMobile", "receiverMobile", "mobilePhone",
            "phone", "mobile")) or
            _pick_scalar(address, ("mobile", "phone", "tel")), compact=True),
        door_num=_normalise_text(_pick_scalar(address, (
            "doorNum", "houseNum", "door", "description")) or
            _pick_scalar(record, ("recipientDoorNum", "receiverDoorNum",
                                  "doorNum", "houseNum")), compact=True),
        expected_delivery_time=_normalise_delivery_time(_pick_scalar(record, (
            "expectedDeliveryTime", "appointmentTime", "deliveryTime", "expected_time"))),
        account=_normalise_text(_pick_scalar(record, (
            "account", "sssAccount", "customerMobile", "loginMobile")) or
            _pick_scalar(user, ("mobile", "phone", "account")) or account, compact=True),
        store_id=_normalise_int_text(_pick_scalar(record, ("storeId", "storeID", "store_id")) or
                                     _pick_scalar(store, ("id", "storeId"))),
        goods_name=goods_name,
        goods_num=goods_num,
        address_detail=_normalise_text(_pick(address, (
            "addressDetail", "address_detail", "address", "position", "description")) or
            _pick(record, ("addressDetail", "address_detail", "position")) or
            flat_address),
        area_code=_record_region_code(address) or _record_region_code(record),
        lnt=_normalise_number(_pick_scalar(address, ("lnt", "lng", "longitude", "lon")) or
                              _pick_scalar(record, ("lnt", "lng", "longitude", "lon"))),
        lat=_normalise_number(_pick_scalar(address, ("lat", "latitude")) or
                              _pick_scalar(record, ("lat", "latitude"))),
        order_type=_normalise_order_type(_pick_scalar(record, ("orderType", "order_type")) or
                                         _pick_scalar(record, ("orderTypeFormat",))),
    )

def _record_core(fingerprint: OrderFingerprint) -> tuple[str, str, str]:
    """可用来判定“是否本批订单”的最小身份：姓名 + 电话 + 预约时间。"""
    return (fingerprint.receive_name, fingerprint.receive_phone,
            fingerprint.expected_delivery_time)

def _address_combo(fingerprint: OrderFingerprint) -> str:
    """把地址折成单一串：``addressDetail + doorNum``。

    列表接口把两者拼成一整串（``recipientAddress``），下单报文则分开存放，
    统一折叠后即可比较；返回空串表示该侧未提供地址。
    """
    return (_normalise_text(fingerprint.address_detail, compact=True)
            + _normalise_text(fingerprint.door_num, compact=True))

def _addresses_compatible(record_address: str, task_address: str) -> bool:
    """地址是否指向同一处：折叠后相等，或一方包含另一方。

    列表接口的地址是整串拼接（可能带省市区前缀、空格或“号”等尾缀），而任务
    地址由 ``addressDetail`` 与门牌拼成，两边很难逐字相等；用包含关系兼容
    这些格式差异，避免把同一订单误判成“其他订单”而重复提交。
    """
    if record_address == task_address:
        return True
    return record_address in task_address or task_address in record_address

def _fingerprint_compatibility(record: OrderFingerprint,
                               task: OrderFingerprint) -> tuple[bool, bool]:
    """比较站内记录与某条任务指纹，返回 ``(是否冲突, 是否因缺字段无法判定)``。

    列表接口只返回概要字段，因此缺失的字段一律忽略（视为“未提供”），不能
    当成“不同”；只有站内明确给出、且与任务不一致的字段才算冲突。地址是
    判断归属的关键，记录缺少地址时标记为无法判定，由调用方 fail-closed。
    """
    record_address = _address_combo(record)
    task_address = _address_combo(task)
    if task_address and not record_address:
        return False, True
    if record_address and task_address and not _addresses_compatible(
            record_address, task_address):
        return True, False
    for name in ("area_code", "lnt", "lat", "store_id", "goods_name",
                 "goods_num", "order_type", "account"):
        on_site = getattr(record, name)
        expected = getattr(task, name)
        if not on_site:
            continue
        if expected and on_site != expected:
            return True, False
    return False, False
