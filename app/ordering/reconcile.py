"""闪时送站内订单列表查询与对账（只读，不发 POST）。"""

from __future__ import annotations

import datetime as _dt
import json
import re
import time
import unicodedata
from collections import Counter
from urllib.parse import urlencode
from typing import Any, Callable

from app.order.common import _emit
from app.ordering.common import _trace
from app.ordering.constants import (
    _INACTIVE_ORDER_STATUS,
    _LIST_PAGE_SIZE,
    _ORDER_LIST_PATH,
    _RECONCILE_POLL_INTERVAL_S,
    _SERVER_PREFILTER_MARGIN_DAYS,
    _SSS_SERVER_PREFILTER,
    _WINDOW_CHECK_TTL_S,
    _WINDOW_PAGE_SIZE,
)
from app.ordering.fingerprint import (
    _fingerprint_compatibility,
    _normalise_delivery_time,
    _order_record_fingerprint,
    _record_core,
    _task_fingerprint,
)
from app.ordering.models import OrderFingerprint, _Reconciliation
from app.ordering.records import _pick, _pick_scalar
def _is_empty_shell(container: dict[str, Any]) -> bool:
    """判断「空壳」：没有 ``records``/``list`` 但 ``total`` 明确为 0 的响应。

    服务端对**空结果**返回的就是这个形状（2026-09-13 生产实测，窗口内确实没有
    订单时）：

        {"success": true, "message": "操作成功！", "code": 200,
         "result": {"total": 0, "size": 10, "current": 1, ..., "pages": 0}}

    它和「服务端没接受查询参数」的响应形状相同，因此只能按「total 明确为 0」
    这一个**收窄**的条件认定为空页；缺 total / total 非 0 / total 读不出来时
    调用方仍然 fail-closed。真正的兜底是 ``_verify_list_window``（只读自检）。
    """
    total = container.get("total")
    if total is None or isinstance(total, bool):
        return False
    try:
        return float(total) == 0.0
    except (TypeError, ValueError):
        return False


def _list_records(payload: dict[str, Any]) -> tuple[list[dict[str, Any]], int | None]:
    """兼容闪时送列表响应的 ``result``/``data`` 两层分页结构。"""
    for container in (payload.get("result"), payload.get("data"), payload):
        if isinstance(container, list):
            return ([item for item in container if isinstance(item, dict)], None)
        if not isinstance(container, dict):
            continue
        records = container.get("records")
        if records is None:
            records = container.get("list")
        if isinstance(records, list):
            try:
                total = int(container.get("total")) if container.get("total") is not None else None
            except (TypeError, ValueError):
                total = None
            return ([item for item in records if isinstance(item, dict)], total)
        # 没有 records/list 的响应有两种：**真的空结果**（total 明确为 0，服务端
        # 的固定形状）与**结构异常/参数不被接受**。前者按空页处理——否则每次
        # 「时间窗内没有订单」都会被判成查询失败，进而退回一次全量历史扫描
        # （2026-09-26 生产实测 69 秒/次，且提交前必然触发）。后者保持 fail-closed，
        # 报错必须点名结构，避免与“真的零订单”混淆。
        if _is_empty_shell(container):
            _trace("list 空壳（total=0 且无 records），按空页处理："
                   + json.dumps(payload, ensure_ascii=False)[:200])
            return ([], 0)
        if isinstance(container, dict) and container:
            raise LookupError(
                "订单列表响应缺少 records/list（服务端可能不支持本次查询参数"
                "或结构已变化），为避免重复下单已停止")
    raise LookupError("订单列表响应缺少 records/list")


def _list_page_control(payload: dict[str, Any]) -> tuple[int | None, str | None, bool | None]:
    """从列表响应提取翻页控制字段：nextPage / nextToken / hasNext。

    不同服务端形态可能返回数字下一页、不透明 token 或显式 hasNext。
    能识别时优先遵循服务端信号；识别不到则返回全 None，由调用方按“
    短页也必须继续到空页/total 满足”处理，绝不把短页当结束。
    """
    for container in (payload.get("result"), payload.get("data"), payload):
        if not isinstance(container, dict):
            continue
        next_page: int | None = None
        for key in ("nextPage", "nextPageNo", "nextPageNum"):
            value = container.get(key)
            if value in (None, ""):
                continue
            try:
                candidate = int(value)
            except (TypeError, ValueError):
                continue
            if candidate > 0:
                next_page = candidate
                break
        next_token: str | None = None
        for key in ("nextPageToken", "nextToken", "pageToken", "next_page_token"):
            value = container.get(key)
            if value not in (None, ""):
                next_token = str(value)
                break
        has_next: bool | None = None
        for key in ("hasNext", "has_next", "hasMore", "has_more"):
            value = container.get(key)
            if isinstance(value, bool):
                has_next = value
                break
            if isinstance(value, (int, float)) and value in (0, 1):
                has_next = bool(value)
                break
        if next_page is not None or next_token is not None or has_next is not None:
            return next_page, next_token, has_next
    return None, None, None


def _build_list_prefilter(tasks: list[dict[str, Any]],
                          *,
                          now: _dt.datetime | None = None,
                          margin: int | None = None) -> dict[str, Any]:
    """按目标送达日构造「服务端预筛」查询参数（epoch 毫秒时间窗）。

    `margin` 可覆盖默认的前后放宽天数（`_SERVER_PREFILTER_MARGIN_DAYS`）：
    「只读核对未决记录」的宽窗复核按 `days_margin` 传更宽的值，这样它也能走
    服务端时间窗，不必为了 ±3 天再翻一遍全量历史。

    2026-09-10 实测纠正了此前「服务端不支持时间过滤」的结论：服务端字段名是
    ``startTime``/``endTime``，**必须是 epoch 毫秒**（传字符串会被 Spring 以
    ``BindException`` 拒绝），而 ``statusList`` 才是真的无效。实测同一账号
    一次对账由 23 页/46s 降到 1 页/1.7s，且过滤后的记录跑原有对账逻辑结论
    完全一致。

    窗口在目标日基础上前后各放宽 ``_SERVER_PREFILTER_MARGIN_DAYS`` 天：订单
    创建时间可能早于或晚于预约送达日，放宽只是多取几条，真正的判定依然由
    本地按送达日/状态过滤负责。

    **刻意不带 ``status`` 参数**：2026-09-11 实测送达日 09-10 的订单全部是
    ``status=3``（已发单），而目标日 09-11 的订单是 ``status=2``。也就是说
    同一批订单在派发后会从 2 变成 3，硬编码 ``status=2`` 会把这批订单直接
    筛没、误判成「缺失」。``statusList`` 又无效、``status`` 是单值，无法
    一次查多个状态，因此只按时间窗预筛。
    """
    days: list[_dt.date] = []
    for task in tasks:
        fingerprint = task.get("fingerprint") if isinstance(task, dict) else None
        raw = str(getattr(fingerprint, "expected_delivery_time", "") or "")[:10]
        try:
            days.append(_dt.datetime.strptime(raw, "%Y-%m-%d").date())
        except ValueError:
            continue
    if not days:
        return {}

    try:
        days_margin = max(0, int(
            _SERVER_PREFILTER_MARGIN_DAYS if margin is None else margin))
    except (TypeError, ValueError):
        days_margin = max(0, int(_SERVER_PREFILTER_MARGIN_DAYS))
    margin_delta = _dt.timedelta(days=days_margin)
    timezone = (now or _dt.datetime.now()).astimezone().tzinfo or _dt.timezone.utc
    start = _dt.datetime.combine(min(days) - margin_delta, _dt.time.min, tzinfo=timezone)
    end = _dt.datetime.combine(max(days) + margin_delta, _dt.time.max, tzinfo=timezone)
    return {
        "startTime": int(start.timestamp() * 1000),
        "endTime": int(end.timestamp() * 1000),
    }


def _expand_days(days: set[str], margin: int) -> set[str]:
    """把目标送达日按 ±``margin`` 天展开；``margin<=0`` 时逐字返回原集合。

    只给“人工只读核对”的宽窗复核用：站内订单的预约送达日可能被平台改到相邻
    日期，严格按目标日过滤会把“落单了但日期变了”误判成“站内没有”。默认
    ``margin=0`` 必须与改动前逐字等价（有专门的等价性测试锁定）。
    """
    margin = max(0, int(margin or 0))
    if not margin:
        return set(days)
    expanded: set[str] = set()
    for raw in days:
        try:
            base = _dt.datetime.strptime(str(raw), "%Y-%m-%d").date()
        except ValueError:
            # 读不出日期（空串/异形写法）时原样保留：宽窗不该因此放宽判定。
            expanded.add(raw)
            continue
        for offset in range(-margin, margin + 1):
            expanded.add((base + _dt.timedelta(days=offset)).isoformat())
    return expanded


def _list_pending_orders(fetch_json: Callable[[str], dict[str, Any]],
                         tasks: list[dict[str, Any]],
                         *,
                         prefilter: dict[str, Any] | None = None,
                         days_margin: int = 0) -> list[dict[str, Any]]:
    """分页拉取订单列表并在本地按预约日期/状态过滤，不发任何写请求。

    ``prefilter`` 非空时只作为服务端粗筛条件（缩小翻页范围）；无论服务端返回
    什么，送达日与活跃状态的判定一律由本地过滤负责，判定标准不因预筛改变。
    ``days_margin`` 只放宽本地“目标送达日”集合（默认 0 = 不放宽）。
    """
    wanted_days = _expand_days(
        {_task_fingerprint(task).expected_delivery_time[:10] for task in tasks},
        days_margin)
    records: list[dict[str, Any]] = []
    page_no = 1
    page_token = ""
    # 预筛窗口内通常只有 100-140 条，用更大的页一页取完（无过滤路径保持原页大小）。
    page_size = _WINDOW_PAGE_SIZE if prefilter else _LIST_PAGE_SIZE
    max_pages = 100
    sweep_started = time.perf_counter()
    page_count = 0
    raw_seen = 0
    seen_page_signatures: set[str] = set()
    seen_tokens: set[str] = set()
    prefilter_active = bool(prefilter)
    while page_count < max_pages:
        params = {
            "pageSize": page_size,
            "sortType": 1,
            "sort": 1,
            **(prefilter or {}),
        }
        if page_token:
            params["pageToken"] = page_token
        else:
            params["pageNo"] = page_no
        query = urlencode(params)
        page_started = time.perf_counter()
        payload = fetch_json(f"{_ORDER_LIST_PATH}?{query}")
        page_seconds = time.perf_counter() - page_started
        page_count += 1
        if payload.get("success") is False:
            raise LookupError(str(payload.get("message") or "订单列表查询失败"))
        try:
            page_records, total = _list_records(payload)
        except LookupError:
            # 服务端对某些查询参数返回 success:true 的「空壳」（无 records）。
            # 埋点里连同完整查询串与原始报文一起记录，便于定位是哪个参数触发。
            _trace(f"list page {page_no} 结构异常，查询串={query} "
                   f"原始报文={json.dumps(payload, ensure_ascii=False)[:400]}")
            raise
        next_page, next_token, has_next = _list_page_control(payload)
        raw_seen += len(page_records)
        page_signature = json.dumps(page_records, ensure_ascii=False, sort_keys=True)
        _trace(f"list page {page_no}{' token' if page_token else ''}: "
               f"{page_seconds:.2f}s 返回 {len(page_records)} 条 total={total} "
               f"nextPage={next_page} hasNext={has_next} rawSeen={raw_seen}")
        # 分页重叠/服务端不前进时，重复页不能继续累加，否则对账会把同一条
        # 记录当多单；也不能静默跳过，必须 fail-closed。
        if page_signature in seen_page_signatures:
            raise LookupError(
                f"订单列表分页返回重复页（pageNo={page_no}），"
                "已停止对账以避免漏单或重复计算")
        seen_page_signatures.add(page_signature)
        if not page_records:
            if total is not None and raw_seen < total:
                raise LookupError(
                    f"订单列表分页提前结束：只读到 {raw_seen}/{total} 条，"
                    "拒绝以不完整列表做对账")
            break
        for record in page_records:
            if _record_active_for_days(record, wanted_days):
                records.append(record)
        # 显式结束信号与 total 矛盾时 fail-closed，不能以“服务端说没了”为由
        # 丢掉可能仍存在的订单。
        if has_next is False:
            if total is not None and raw_seen < total:
                raise LookupError(
                    f"订单列表 hasNext=false 但只读到 {raw_seen}/{total} 条，"
                    "拒绝以不完整列表做对账")
            break
        if next_token is not None:
            if next_token in seen_tokens:
                raise LookupError("订单列表分页 token 未前进，拒绝继续对账")
            seen_tokens.add(next_token)
            page_token = next_token
            page_no += 1
            continue
        if next_page is not None:
            if next_page <= page_no:
                raise LookupError("订单列表 nextPage 未前进，拒绝继续对账")
            page_no = next_page
            page_token = ""
            continue
        if has_next is True:
            page_no += 1
            continue
        if total is not None and raw_seen >= total:
            break
        # 没有 total/next/hasNext 时，短页也继续到下一页；真正的结束信号是
        # 空页、total 已满足、hasNext=false、nextPage/nextToken 指示或达到上限。
        page_no += 1
    else:
        raise LookupError("订单列表分页超过 100 页，拒绝继续下单")
    _trace(f"list sweep 结束：{page_count} 页 {time.perf_counter() - sweep_started:.1f}s，"
           f"命中目标日 {len(records)} 条，预筛={'开' if prefilter_active else '关'}")
    return records


def _record_active_for_days(record: dict[str, Any], wanted_days: set[str]) -> bool:
    """本地过滤：预约送达日在目标日期内且状态为活跃（未取消/未退款）。"""
    raw_expected = _pick(record, ("expectedDeliveryTime", "expected_delivery_time",
                                  "appointmentTime", "appointment_time"))
    day = _normalise_delivery_time(raw_expected)[:10]
    if day not in wanted_days:
        return False
    status = _pick(record, ("status", "orderStatus", "state"))
    try:
        code = int(str(status).strip())
    except (TypeError, ValueError):
        # 状态不可读时按“不排除”处理，避免字段缺失导致整批误判缺失。
        return True
    # 取消/退款/忽略类状态不计入在途；其余一律视为活跃。
    return code not in _INACTIVE_ORDER_STATUS


def _record_created_timestamp(record: dict[str, Any]) -> float | None:
    """解析站内订单创建时间，用于排除本批次之前由其他设备创建的相似订单。"""
    value = _pick(record, (
        "created_at", "createdAt", "createTime", "createdTime", "gmtCreate",
        "orderTime", "submitTime", "created"))
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return number / 1000.0 if number > 10_000_000_000 else number
    text = unicodedata.normalize("NFKC", str(value)).strip()
    if re.fullmatch(r"\d{10,13}", text):
        number = int(text)
        return number / 1000.0 if number > 10_000_000_000 else float(number)
    try:
        parsed = _dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_dt.timezone(_dt.timedelta(hours=8)))
    return parsed.timestamp()


#: 时间窗自检结论的进程内缓存：{"verdict": str, "at": 单调秒}。
_WINDOW_CHECK_CACHE: dict[str, Any] = {"verdict": "", "at": 0.0}


def _list_probe(fetch_json: Callable[[str], dict[str, Any]],
                params: dict[str, Any]) -> list[dict[str, Any]]:
    """时间窗自检用的一次性只读查询：固定 pageSize=1，返回首页记录。"""
    query = urlencode({"pageNo": 1, "pageSize": 1, "sortType": 1, "sort": 1, **params})
    payload = fetch_json(f"{_ORDER_LIST_PATH}?{query}")
    if payload.get("success") is False:
        raise LookupError(str(payload.get("message") or "订单列表查询失败"))
    records, _total = _list_records(payload)
    return records


def _verify_list_window(fetch_json: Callable[[str], dict[str, Any]],
                        callback: Callable[[str], Any] | None = None) -> str:
    """只读自检：确认服务端真的接受 startTime/endTime 时间窗过滤。

    为什么需要它：整个对账判定都建立在「带时间窗的查询返回 0 条 = 窗口内确实
    没有订单」之上。服务端对**空结果**与**不接受的查询参数**返回的是同一种
    success:true 空壳（见 `_is_empty_shell`）。一旦服务端改了时间窗语义，对账就会
    把「读不到」误判成「站内没有」，提交前据此放行会重复下单。

    做法（最多两次 pageSize=1 的只读请求，结论按进程缓存 `_WINDOW_CHECK_TTL_S` 秒）：

    1. 探针 A：不带任何过滤查 1 条（实测列表按创建时间倒序，即最新的一单；
       自检不依赖排序——无论拿到哪一条，它自己的创建时间必然落在下面那个窗口里）；
    2. 探针 B：用这单的**创建时间 ±1 天**做时间窗再查 1 条——该窗口按构造必然
       包含探针 A 那单；
    3. 探针 B 有记录 → ``ok``（时间窗被接受）；探针 A 也没有记录 → ``ok``
       （账号本来就没有订单，空窗合理）；探针 B 空而探针 A 有记录 →
       ``unsupported``；创建时间读不出或请求异常 → ``inconclusive``。

    只返回结论、不抛异常：``unsupported`` 由调用方 fail-closed，
    ``inconclusive`` 绝不新增停机条件（读不到 ≠ 站内没有）。
    """
    cached = str(_WINDOW_CHECK_CACHE.get("verdict") or "")
    cached_at = float(_WINDOW_CHECK_CACHE.get("at") or 0.0)
    if cached and (time.monotonic() - cached_at) < _WINDOW_CHECK_TTL_S:
        return cached

    verdict = "inconclusive"
    reason = "查询异常或读不出创建时间"
    try:
        newest = _list_probe(fetch_json, {})
        if not newest:
            verdict = "ok"
            reason = "账号暂无订单，空窗口合理"
        else:
            created = _record_created_timestamp(newest[0])
            if created is None:
                reason = "最新订单读不出创建时间"
            else:
                timezone = _dt.datetime.now().astimezone().tzinfo or _dt.timezone.utc
                moment = _dt.datetime.fromtimestamp(created, tz=timezone)
                window = {
                    "startTime": int((moment - _dt.timedelta(days=1)).timestamp() * 1000),
                    "endTime": int((moment + _dt.timedelta(days=1)).timestamp() * 1000),
                }
                if _list_probe(fetch_json, window):
                    verdict = "ok"
                    reason = "带时间窗的查询能查到账号最新订单"
                else:
                    verdict = "unsupported"
                    reason = "账号有订单，但带时间窗的查询返回 0 条"
    except Exception as exc:  # noqa: BLE001 - 自检读不到只能如实说读不到
        _trace(f"时间窗自检查询失败（按 inconclusive 处理）：{exc}")

    _WINDOW_CHECK_CACHE["verdict"] = verdict
    _WINDOW_CHECK_CACHE["at"] = time.monotonic()
    if verdict == "ok":
        _emit(callback, f"时间窗自检：服务端接受 startTime/endTime（{reason}）")
    elif verdict == "unsupported":
        _emit(callback, f"时间窗自检：{reason}——服务端可能不再接受 startTime/endTime")
    else:
        _emit(callback, f"时间窗自检未能完成（{reason}），按既有逻辑继续")
    return verdict


# 仅使用数据模型上唯一、稳定的订单号/主键做分页去重；像 pickUpNumber
# 这类可能被合法多单复用的字段不参与，避免把“一人多单”误合并。
_STATION_ID_KEYS = (
    "id", "orderId", "order_id",
    "orderSn", "orderNO", "orderNo", "order_no", "orderNumber",
)


def _station_record_identity(record: dict[str, Any]) -> tuple[str, str] | None:
    """站内记录的稳定身份，用于去掉分页重叠造成的同一条记录。

    同一人的两条合法订单通常有不同的 ``id``/``orderSn``；如果记录连一个稳定
    标识都没有，则返回 ``None`` 不做去重，宁可像以前一样按多条计数并触发
    重复保护，也不能把合法多单误合并。
    """
    for key in _STATION_ID_KEYS:
        value = _pick_scalar(record, (key,))
        if value in (None, ""):
            continue
        # 0/“0” 常被接口当作缺失哨兵值；这种值不参与去重，避免把不同订单
        # 合并成同一个身份导致漏确认。
        if str(value).strip() == "0":
            continue
        return key, str(value)
    return None


def _reconcile_tasks(tasks: list[dict[str, Any]],
                     fetch_json: Callable[[str], dict[str, Any]],
                     *, created_after: float | None = None,
                     created_before: float | None = None,
                     prefilter: dict[str, Any] | None = None,
                     days_margin: int = 0) -> _Reconciliation:
    """按订单身份对账，兼容列表接口的概要结构。

    匹配以「姓名 + 电话 + 预约时间」为核心；站内明确给出、却与任务不一致的
    字段（门店/商品/坐标等）判定为他人订单而忽略；站内未返回的字段视为
    “未提供”而不参与比较。地址是归属的关键：若记录疑似本批订单却缺少地址，
    则无法安全判定，直接 fail-closed，绝不猜测。

    ``created_after``/``created_before`` 用于收尾对账时只接受本批次时间窗口
    内创建的订单；站内记录缺少创建时间时无法证明归属，按“不排除”处理。

    ``prefilter`` 只是服务端粗筛（参见 ``_build_list_prefilter``），不改变
    任何本地判定标准。
    """
    expected = Counter(_task_fingerprint(task) for task in tasks)
    waiting: dict[OrderFingerprint, list[dict[str, Any]]] = {}
    by_core: dict[tuple[str, str, str], list[OrderFingerprint]] = {}
    for task in tasks:
        fingerprint = _task_fingerprint(task)
        waiting.setdefault(fingerprint, []).append(task)
    for fingerprint in expected:
        by_core.setdefault(_record_core(fingerprint), []).append(fingerprint)
    expected_accounts = {fingerprint.account for fingerprint in expected if fingerprint.account}
    fallback_account = next(iter(expected_accounts)) if len(expected_accounts) == 1 else ""

    actual = Counter()
    seen_station_records: set[tuple[str, str]] = set()
    for record in _list_pending_orders(fetch_json, tasks, prefilter=prefilter,
                                       days_margin=days_margin):
        identity = _station_record_identity(record)
        if identity is not None:
            if identity in seen_station_records:
                continue
            seen_station_records.add(identity)
        created_at = _record_created_timestamp(record)
        if created_at is not None:
            if created_after is not None and created_at < created_after:
                continue
            if created_before is not None and created_at > created_before:
                continue
        fingerprint = _order_record_fingerprint(record, account=fallback_account)
        core = _record_core(fingerprint)
        if not all(core):
            # 连姓名/电话/预约时间都读不出，无法排除是本批订单，必须 fail-closed。
            raise LookupError("订单列表记录缺少姓名、电话或预约送达时间，无法安全对账："
                              + json.dumps(record, ensure_ascii=False)[:300])
        candidates = by_core.get(core, [])
        matched: OrderFingerprint | None = None
        undecidable = False
        for candidate in candidates:
            conflict, unknown = _fingerprint_compatibility(fingerprint, candidate)
            if unknown:
                undecidable = True
                continue
            if not conflict:
                matched = candidate
                break
        if matched is not None:
            actual[matched] += 1
        elif undecidable:
            raise LookupError("订单列表记录疑似本批订单但缺少地址等字段，无法安全对账："
                              + json.dumps(record, ensure_ascii=False)[:300])
        # 否则：与同人同时间的任务在已知字段上冲突，判定为其他订单，忽略。

    confirmed: set[str] = set()
    missing: list[dict[str, Any]] = []
    duplicate_count = 0
    for fingerprint, grouped_tasks in waiting.items():
        on_site = actual[fingerprint]
        confirmed.update(task["identifier"] for task in grouped_tasks[:min(len(grouped_tasks), on_site)])
        missing.extend(grouped_tasks[min(len(grouped_tasks), on_site):])
        duplicate_count += max(0, on_site - len(grouped_tasks))
    return _Reconciliation(confirmed, missing, duplicate_count, sum(actual.values()))


def _reconcile_person_matches(tasks: list[dict[str, Any]],
                              fetch_json: Callable[[str], dict[str, Any]],
                              *, created_after: float | None = None,
                              days_margin: int = 0
                              ) -> dict[str, list[dict[str, Any]]]:
    """按「姓名 + 电话」在宽窗内找站内订单（**忽略预约时间是否一致**）。

    只给“人工只读核对”的宽窗复核用。严格对账要求预约时间逐字一致，因此
    “落单了、但送达日/时间被平台改过”在严格对账里表现为“站内缺失”；如果
    据此解除阻断就会重复下单。这里放宽到姓名 + 电话，其余字段仍走同一套
    ``_fingerprint_compatibility``（地址/门店/商品等冲突仍算他人订单）。

    返回 ``{task_identifier: [{"order_id", "delivery_time", "undecidable"}]}``；
    地址等字段缺失导致无法判定时**也算命中**（保守方向：宁可拒绝解除阻断）。
    """
    by_person: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for task in tasks:
        fingerprint = _task_fingerprint(task)
        person = (fingerprint.receive_name, fingerprint.receive_phone)
        if not all(person):
            continue
        by_person.setdefault(person, []).append(task)
    if not by_person:
        return {}
    expected_accounts = {_task_fingerprint(task).account for task in tasks}
    expected_accounts.discard("")
    fallback_account = (next(iter(expected_accounts))
                        if len(expected_accounts) == 1 else "")
    matches: dict[str, list[dict[str, Any]]] = {}
    seen_station_records: set[tuple[str, str]] = set()
    # 宽窗复核同样走服务端时间窗（±days_margin 天）：本地「±days_margin 天」的判定
    # 一字不改，服务端窗口只做粗筛；否则每次只读核对都要翻一遍全量历史（实测 69 秒）。
    for record in _list_pending_orders(
            fetch_json, tasks, days_margin=days_margin,
            prefilter=_build_list_prefilter(tasks, margin=days_margin)):
        identity = _station_record_identity(record)
        if identity is not None:
            if identity in seen_station_records:
                continue
            seen_station_records.add(identity)
        created_at = _record_created_timestamp(record)
        if created_at is not None and created_after is not None \
                and created_at < created_after:
            continue
        fingerprint = _order_record_fingerprint(record, account=fallback_account)
        candidates = by_person.get(
            (fingerprint.receive_name, fingerprint.receive_phone))
        if not candidates:
            continue
        for task in candidates:
            conflict, undecidable = _fingerprint_compatibility(
                fingerprint, _task_fingerprint(task))
            if conflict:
                continue
            identifier = str(task.get("identifier") or "")
            matches.setdefault(identifier, []).append({
                "order_id": str(identity[1]) if identity else "",
                "delivery_time": str(fingerprint.expected_delivery_time or ""),
                "undecidable": bool(undecidable),
            })
            break
    return matches


def _emit_reconciliation(callback: Callable[[str], Any] | None, label: str,
                         reconciliation: _Reconciliation, total: int) -> None:
    _emit(callback, f"{label}：站内匹配 {reconciliation.matched_count}/{total} 单，"
                    f"缺失 {len(reconciliation.missing)} 单，"
                    f"重复 {reconciliation.duplicate_count} 单")


def _safe_reconcile(tasks: list[dict[str, Any]],
                    fetch_json: Callable[[str], dict[str, Any]],
                    callback: Callable[[str], Any] | None,
                    label: str,
                    *,
                    created_after: float | None = None,
                    attempts: int = 1,
                    prefilter: dict[str, Any] | None = None,
                    zero_retry_delay: float | None = None) -> _Reconciliation | None:
    """只读对账；列表延迟时有限轮询，绝不因“暂时查不到”而重发 POST。

    ``prefilter`` 非空时用服务端时间窗粗筛，**结论一律由本地严格判定负责**。
    3.6.15 起不再有「缺失 → 无过滤全量扫描复核」：全量扫描只多取记录、不改判定，
    而提交前站内本就没有本批订单（0 匹配是常态），于是每次运行都要白扫一遍全量
    历史（2026-09-26 生产实测 69 秒）。真正需要兜底的是「服务端不再接受时间窗」：
    那会把「窗口内 0 条」从「确实没有」变成「读不到」，交给 `_verify_list_window`
    只读自检识别并 fail-closed。``YIKOU_SSS_SERVER_PREFILTER=0`` 仍可退回无过滤扫描。

    ``zero_retry_delay`` 为秒数时：预筛窗口返回「一条都没有」会先等这么久
    再重查一次，用于覆盖「刚写入、列表还读不到」的情况。只应在**提交后**的
    对账里启用——提交前站内本就没有本批订单，重查纯属浪费。
    """
    if prefilter is None and _SSS_SERVER_PREFILTER:
        prefilter = _build_list_prefilter(tasks)
    if prefilter:
        _trace(f"{label} 服务端预筛窗口 startTime={prefilter.get('startTime')} "
               f"endTime={prefilter.get('endTime')}")
    attempts = max(1, int(attempts))
    last: _Reconciliation | None = None
    last_error = ""
    zero_retry_done = False
    for attempt in range(attempts):
        try:
            reconciliation = _reconcile_tasks(tasks, fetch_json, created_after=created_after,
                                              prefilter=prefilter)
        except Exception as exc:
            last_error = str(exc)
            if attempt + 1 < attempts:
                _emit(callback, f"{label}第 {attempt + 1} 次查询失败：{exc}；"
                                f"{_RECONCILE_POLL_INTERVAL_S:g}s 后只读复查")
                time.sleep(_RECONCILE_POLL_INTERVAL_S)
                continue
            reconciliation = None
        # 预筛窗口一条都没查到、而目标批次非空：很可能是「刚写入还没被列表读到」。
        # 先做一次短暂重查，把这种情况和「窗口真的为空」区分开；这次重查不占用
        # attempts 配额，因此后面仍会正常进入时间窗自检。
        if (zero_retry_delay is not None and reconciliation is not None and prefilter
                and not zero_retry_done and reconciliation.matched_count == 0
                and len(reconciliation.missing) == len(tasks)):
            zero_retry_done = True
            _emit(callback, f"{label}：服务端预筛窗口内 0 条，"
                            f"{zero_retry_delay:g}s 后重查一次"
                            f"（可能是刚写入尚未可见）")
            time.sleep(zero_retry_delay)
            try:
                reconciliation = _reconcile_tasks(
                    tasks, fetch_json, created_after=created_after, prefilter=prefilter)
            except Exception as exc:
                last_error = str(exc)
                reconciliation = None
        if reconciliation is not None:
            last_error = ""
            _emit_reconciliation(callback, label, reconciliation, len(tasks))
            last = reconciliation
            if not reconciliation.missing:
                return reconciliation
            if attempt + 1 < attempts:
                _emit(callback, f"{label}暂缺 {len(reconciliation.missing)} 单，"
                                f"{_RECONCILE_POLL_INTERVAL_S:g}s 后只读复查（不重发 POST）")
                time.sleep(_RECONCILE_POLL_INTERVAL_S)
                continue
        # 轮询用尽仍有缺失或查询报错：不再做无过滤全量扫描（见 docstring）。
        # 只补一次只读的**时间窗自检**：只有「服务端不再接受 startTime/endTime」
        # 才会让「窗口内 0 条」失去意义，那种情况必须 fail-closed，绝不提交。
        if prefilter and reconciliation is not None and reconciliation.matched_count == 0:
            if _verify_list_window(fetch_json, callback) == "unsupported":
                _emit(callback, f"{label}：时间窗自检判定服务端未接受 startTime/endTime，"
                                "「窗口内 0 条」不能当作「站内没有」；"
                                "为避免重复下单，已停止本次对账")
                return None
        break
    if last is None:
        _emit(callback, f"{label}失败：{last_error}；为避免重复下单，后续不会自动提交")
    return last


def _merge_reconciliation(preconfirmed: set[str], current: _Reconciliation) -> _Reconciliation:
    return _Reconciliation(
        confirmed=set(preconfirmed) | set(current.confirmed),
        missing=list(current.missing),
        duplicate_count=current.duplicate_count,
        matched_count=len(preconfirmed) + current.matched_count,
    )
