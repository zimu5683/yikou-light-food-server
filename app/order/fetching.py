"""管理后台订单抓取：分页列表、详情预取、日期过滤与退款拆分。"""

from __future__ import annotations

import datetime as _dt
import re
from concurrent.futures import ThreadPoolExecutor
import time
from typing import Any, Callable

from app.core.models import OrderInfo
from app.order.parsing import (
    get_address_base_sheet_name,
    get_yijin_address_from_product_note,
    parse_meal_rows,
)


def parse_target_date(value: object = None, *, today: _dt.date | None = None) -> _dt.date:
    """Parse a target date, defaulting to the local current date."""
    current = today or _dt.date.today()
    if value is None:
        return current
    if isinstance(value, _dt.datetime):
        result = value.date()
    elif isinstance(value, _dt.date):
        result = value
    else:
        text = str(value).strip()
        if not text:
            return current
        try:
            result = _dt.date.fromisoformat(text)
        except ValueError as exc:
            raise ValueError("目标日期格式必须为 YYYY-MM-DD") from exc
    if result > current:
        raise ValueError("目标日期不能晚于今天")
    return result

def parse_order_created_date(value: object) -> _dt.date | None:
    """Normalize common API date strings and Unix timestamps to a local date."""
    if value in (None, ""):
        return None
    if isinstance(value, _dt.datetime):
        return value.date()
    if isinstance(value, _dt.date):
        return value
    if isinstance(value, (int, float)):
        try:
            timestamp = float(value)
            if timestamp > 10_000_000_000:
                timestamp /= 1000
            return _dt.datetime.fromtimestamp(timestamp).date()
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip()
    if not text:
        return None
    if text.isdigit():
        try:
            return parse_order_created_date(int(text))
        except ValueError:
            return None
    match = re.search(r"(\d{4})[-/.年](\d{1,2})[-/.月](\d{1,2})", text)
    if match:
        try:
            return _dt.date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        except ValueError:
            return None
    return None

ORDER_STATE_REFUND_DONE = 8

ORDER_STATE_REFUND_APPLIED = 7

def _order_from_api_data(code: str, data: dict[str, Any]) -> OrderInfo | None:
    """把 /channel/order/{id} 的 data 字段解析为订单；字段全空时返回 None。

    字段映射与页面渲染一致：address.contact=收货人、address.mobile=电话、
    address.address+description=配送地址、goods[].name/num=商品与数量
    （名称自带（午餐）/（晚餐）标签）、attrData.matal=商品备注。
    """
    address_info = data.get("address") or {}
    raw_address = " ".join(
        str(address_info.get(k) or "").strip() for k in ("address", "description")
    ).strip()
    address = raw_address
    name = str(address_info.get("contact") or "").strip()
    phone = str(address_info.get("mobile") or data.get("mobile") or "").strip()
    if not name:
        name = str((data.get("user") or {}).get("nickname") or "").strip()
    rows = [{"product": str(g.get("name") or ""), "qty": f"x {g.get('num') or 1}"}
            for g in (data.get("goods") or []) if g.get("name")]
    notes: list[str] = []
    for goods in data.get("goods") or []:
        attr = goods.get("attrData") or {}
        if attr.get("matal"):
            notes.append(str(attr["matal"]))
        for material in attr.get("material") or []:
            if isinstance(material, dict) and material.get("name"):
                notes.append(str(material["name"]))
    base = get_address_base_sheet_name(address)
    if base == "衣锦":
        address = get_yijin_address_from_product_note(" ".join(notes))
    metadata = {
        "order_id": str(data.get("id") or ""),
        "created_at": next((data.get(key) for key in
                             ("created_at", "createdAt", "create_time", "createTime", "order_time", "orderTime")
                             if data.get(key) not in (None, "")), None),
    }
    candidate = OrderInfo(
        code, name, phone, address, base, delivery_address=raw_address, metadata=metadata
    )
    candidate.lunch = parse_meal_rows(rows, "午餐")
    candidate.dinner = parse_meal_rows(rows, "晚餐")
    if not (name or phone or address or candidate.lunch or candidate.dinner):
        return None
    return candidate

def _api_list_waimai_orders(api_get: Callable[[str], dict[str, Any]],
                            selected_date: _dt.date,
                            callback: Callable[[str], Any] | None = None,
                            page_size: int = 200, max_pages: int = 200,
                            concurrent_pages: bool = False) -> list[dict[str, Any]]:
    """分页拉取外送订单（scene=1，新单在前）。

    实测 m.icall.me 的 ``/channel/order`` GET 接口不支持 ``startTime/endTime``
    服务端过滤，只能整表分页拉取后由本程序按目标日期筛选。因此这里直接
    并发拉取全部分页，并使用较大的 ``pageSize=200`` 减少请求次数。

    ``concurrent_pages=True`` 时剩余分页并发拉取（HTTP 客户端每次调用独立，
    不会像浏览器会话那样受单页并发限制）。
    """
    def parse_batch(batch: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for item in batch:
            created = item.get("created_at")
            out.append({
                "order_id": str(item.get("id") or ""),
                "pick_no": str(item.get("pickNo") or "").strip(),
                "store_id": str(item.get("storeId") or ""),
                "created_at": created,
                "date": parse_order_created_date(created),
                # 退款状态字段：state=8 已退款 / state=7 用户申请退款（待审批）。
                # 列表接口完整响应里直接携带，详情页反而没有统一字段，故在此保留。
                "state": item.get("state"),
            })
        return out

    def fetch_page(page_no: int, page_size_arg: int,
                   start_time: str = "", end_time: str = "") -> tuple[list[dict[str, Any]], int | None]:
        payload = api_get(
            "/channel/order?scene=1&storeId=&orderSn=&userKeyword=&state=&payType=&source="
            f"&pageNo={page_no}&pageSize={page_size_arg}&startTime={start_time}&endTime={end_time}",
        )
        data = payload.get("data") or {}
        batch = data.get("list") or []
        total = data.get("total")
        return parse_batch(batch), (int(total) if isinstance(total, int) else None)

    def fetch_serial(page_size_arg: int, start_time: str = "", end_time: str = "") -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for page_no in range(1, max_pages + 1):
            batch, total = fetch_page(page_no, page_size_arg, start_time, end_time)
            if not batch:
                break
            rows.extend(batch)
            if total is not None and page_no * page_size_arg >= total:
                break
        return rows

    def fetch_concurrent(page_size_arg: int, start_time: str = "", end_time: str = "") -> list[dict[str, Any]]:
        first_batch, total = fetch_page(1, page_size_arg, start_time, end_time)
        if not first_batch:
            return []
        rows = list(first_batch)
        effective = len(first_batch)
        if total is None or total <= len(rows) or effective <= 0:
            return rows
        total_pages = (total + effective - 1) // effective
        remaining = list(range(2, min(total_pages, max_pages) + 1))
        if not remaining:
            return rows

        def load(page_no: int) -> list[dict[str, Any]]:
            batch, _ = fetch_page(page_no, page_size_arg, start_time, end_time)
            return batch

        # 实测 pageSize=200 + 10 并发时，6150 条订单约 3~4 秒拉完。
        with ThreadPoolExecutor(max_workers=10) as executor:
            for batch in executor.map(load, remaining):
                rows.extend(batch)
        return rows

    def fetch_safe(page_size_arg: int) -> list[dict[str, Any]]:
        try:
            if concurrent_pages:
                return fetch_concurrent(page_size_arg)
            return fetch_serial(page_size_arg)
        except Exception:
            # 大 pageSize 不被支持时逐级回退，优先保证能完成任务。
            for fallback in (100, 50):
                if page_size_arg > fallback:
                    try:
                        return fetch_serial(fallback)
                    except Exception:
                        continue
            raise

    return _dedupe_order_rows(fetch_safe(page_size))

def _row_identity(row: dict[str, Any]) -> tuple[Any, ...]:
    """同一接口记录的身份；优先 order_id，缺失时用取单号+创建时间+日期兜底。"""
    order_id = str(row.get("order_id") or "").strip()
    if order_id:
        return ("order_id", order_id)
    return ("fallback", str(row.get("pick_no") or ""), str(row.get("created_at") or ""),
            str(row.get("date") or ""))


def _dedupe_order_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """去掉分页重叠/重试造成的重复记录，保留第一次出现的顺序。"""
    seen: set[tuple[Any, ...]] = set()
    result: list[dict[str, Any]] = []
    for row in rows:
        key = _row_identity(row)
        if key in seen:
            continue
        seen.add(key)
        result.append(row)
    return result


def _state_code(value: Any) -> int | None:
    """把 state 字段规范成整数；非数字/缺失返回 None，不抛异常。"""
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        match = re.search(r"\d+", str(value))
        return int(match.group()) if match else None


def _group_orders_by_pick(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """按取单号分组（保持接口的新单在前顺序）；无取单号的忽略。

    同一取单号内按 order_id 去重，避免分页重叠让同一接口记录进入详情重试候选。
    """
    grouped: dict[str, list[dict[str, Any]]] = {}
    seen_in_pick: dict[str, set[tuple[Any, ...]]] = {}
    for row in rows:
        pick = str(row.get("pick_no") or "").strip()
        if not pick:
            continue
        key = _row_identity(row)
        bucket = grouped.setdefault(pick, [])
        seen = seen_in_pick.setdefault(pick, set())
        if key in seen:
            continue
        seen.add(key)
        bucket.append(row)
    return grouped

def _filter_rows_by_date(rows: list[dict[str, Any]], selected_date: _dt.date) -> list[dict[str, Any]]:
    """先按目标日期筛选订单，避免在遍历 W 编号时再逐单判断日期。"""
    return [row for row in rows if row.get("date") == selected_date]

def _order_numbers_for_date(rows_by_pick: dict[str, list[dict[str, Any]]],
                            order_count: int | None = None) -> list[int]:
    """返回需要处理的 W 编号列表（从大到小）。

    ``order_count`` 为空时返回目标日期全部存在的 W 编号；
    有值时只返回不超过 ``order_count`` 且当天实际存在的编号。
    """
    numbers = sorted(
        (int(code[1:]) for code in rows_by_pick
         if code.startswith("W") and code[1:].isdigit()),
        reverse=True,
    )
    if order_count:
        numbers = [n for n in numbers if n <= order_count]
    return numbers

def split_refund_orders(rows_by_pick: dict[str, list[dict[str, Any]]],
                        numbers: list[int]) -> tuple[list[int], list[int], list[int]]:
    """把待处理编号按退款状态分成三类：(正常, 退款成功, 申请退款中)。

    每行来自已按目标日期过滤的列表接口记录，携带 ``state`` 字段：
    state=8 已退款（同意后退款成功）、state=7 用户申请退款（待审批）。
    一个取单号若含多条记录，任一记录命中退款状态即归入对应退款类，
    且从正常列表剔除（后续不再拉详情/写表）。返回的编号均为从大到小。
    """
    refunded: list[int] = []
    applied: list[int] = []
    normal: list[int] = []
    for number in numbers:
        code = f"W{number}"
        rows = rows_by_pick.get(code, [])
        states = {_state_code(r.get("state")) for r in rows}
        if ORDER_STATE_REFUND_DONE in states:
            refunded.append(number)
        elif ORDER_STATE_REFUND_APPLIED in states:
            applied.append(number)
        else:
            normal.append(number)
    # 保持原有从大到小顺序。
    return (normal, refunded, applied)

def _order_detail_by_id(api_get: Callable[[str], dict[str, Any]],
                         code: str, order_id: str, store_id: str) -> OrderInfo | None:
    """直接从详情接口读取订单，偶发失败自动重试一次。"""
    for attempt in range(2):
        try:
            payload = api_get(f"/channel/order/{order_id}?storeId={store_id}")
            data = payload.get("data")
        except Exception:
            data = None
        if isinstance(data, dict):
            order = _order_from_api_data(code, data)
            if order is not None:
                order.metadata["order_id"] = order_id
                return order
        if attempt == 0:
            time.sleep(1)
    return None

def _prefetch_order_details(api_get: Callable[[str], dict[str, Any]],
                            rows_by_pick: dict[str, list[dict[str, Any]]],
                            numbers: list[int], max_workers: int = 5) -> dict[int, OrderInfo]:
    """并发预取订单详情（纯接口模式），返回 {W编号: 订单}，失败项不包含在内。

    成功项在串行写表时直接复用；失败项仍走原有串行重试/决策流程。
    """
    tasks = []
    for number in numbers:
        candidates = rows_by_pick.get(f"W{number}", [])
        if candidates:
            tasks.append((number, candidates[0]))

    def fetch(task: tuple[int, dict[str, Any]]) -> tuple[int, OrderInfo | None]:
        number, entry = task
        order = _order_detail_by_id(
            api_get, f"W{number}", entry["order_id"], entry["store_id"])
        return number, order

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        results = executor.map(fetch, tasks)
    return {number: order for number, order in results if order is not None}
