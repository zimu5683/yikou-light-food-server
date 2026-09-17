"""管理后台订单处理：HTTP 抓单、详情合并、地址归一化与写排单表。

调用方传入 :class:`AppConfig`、口令与取消事件；本模块不保存凭据，也不依赖
任何浏览器/桌面运行时。平台请求全部经由
:class:`app.integrations.api_client.AdminApiClient`。
"""
from __future__ import annotations

import datetime as _dt

import re
import shutil
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Iterable

from app.core.models import MealInfo, OrderInfo
from app.order.parsing import (
    clear_campus_sub_sheets,
    get_address_base_sheet_name,
    get_yijin_address_from_product_note,
    sort_campus_sub_sheets,
)

from app.integrations.api_client import AdminApiClient
from app.order.aliases import aliases_path, load_aliases, write_pending
from app.order.delivery import DEFAULT_ALIASES, normalize_delivery_point

REG_MEAL_COUNT = re.compile(r"x\s*(\d+)", re.I)
REG_MEAL_SPLIT = re.compile(r"（午餐）|（晚餐）")
SHEET_MEAL_SUFFIX = {"午餐": "中餐", "晚餐": "晚餐"}
WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")
HISTORICAL_SHEET_HEADERS = (
    "取单号", "姓名", "地址", "电话", *WEEKDAYS, "餐别", "经济/豪华", "总餐次",
)
PENDING_ADDRESS_HEADERS = (
    "取单号", "姓名", "地址", "电话", "餐别", "餐种", "餐次",
    "校区", "置信度", "原因", "候选点",
)

# 订单列表接口 state 取值（2026-09-11 抓包确认）：state=8 为已退款（同意后退款
# 成功），state=7 为「用户申请退款」即退款待审批（商家还没同意，对应界面上的
# 「退款详情/申请退款」）。这两种订单都不应写入排班表格。
ORDER_STATE_REFUND_DONE = 8
ORDER_STATE_REFUND_APPLIED = 7


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


def _emit(callback: Callable[[str], Any] | None, message: str) -> None:
    if callback:
        callback(message)


def parse_meal_rows(rows: Iterable[dict[str, str]], meal_type: str) -> list[MealInfo]:
    """把订单表格里的「商品名 + 数量」解析成 MealInfo 列表。"""
    result: list[MealInfo] = []
    for row in rows:
        product = str(row.get("product", ""))
        quantity = str(row.get("qty", ""))
        segments = REG_MEAL_SPLIT.split(product)
        labels = REG_MEAL_SPLIT.findall(product)
        for index, segment in enumerate(segments[:-1]):
            current = "午餐" if labels[index] == "（午餐）" else "晚餐"
            if current != meal_type or not segment.strip():
                continue
            count_match = REG_MEAL_COUNT.search(quantity)
            result.append(MealInfo(
                total_meals=6 if "六餐" in segment else 1 if "单点" in segment else None,
                grade="经济" if "经济" in segment else "豪华" if "豪华" in segment else None,
                count=int(count_match.group(1)) if count_match else 1,
                meal_type=meal_type,
            ))
    return result


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

    return fetch_safe(page_size)



def _group_orders_by_pick(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """按取单号分组（保持接口的新单在前顺序）；无取单号的忽略。"""
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        pick = str(row.get("pick_no") or "").strip()
        if pick:
            grouped.setdefault(pick, []).append(row)
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
        if any(int(r.get("state") or 0) == ORDER_STATE_REFUND_DONE for r in rows):
            refunded.append(number)
        elif any(int(r.get("state") or 0) == ORDER_STATE_REFUND_APPLIED for r in rows):
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


