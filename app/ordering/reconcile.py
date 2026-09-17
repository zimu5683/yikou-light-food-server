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

from app.order.runner import _emit
from app.ordering.common import _trace
from app.ordering.constants import (
    _INACTIVE_ORDER_STATUS,
    _LIST_PAGE_SIZE,
    _ORDER_LIST_PATH,
    _RECONCILE_POLL_INTERVAL_S,
    _SERVER_PREFILTER_MARGIN_DAYS,
    _SSS_SERVER_PREFILTER,
)
from app.ordering.fingerprint import (
    _fingerprint_compatibility,
    _normalise_delivery_time,
    _order_record_fingerprint,
    _record_core,
    _task_fingerprint,
)
from app.ordering.models import OrderFingerprint, _Reconciliation
from app.ordering.records import _pick
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
        # 服务端对不支持的过滤参数返回 success:true + 无 records 的空壳
        # （2026-09-10 实测：带 startTime/endTime/statusList 即如此）。调用方
        # 已改为无过滤参数 + 本地过滤；此处保留 fail-closed，但报错必须点名
        # 结构，避免与“真的零订单”混淆。
        if isinstance(container, dict) and container:
            raise LookupError(
                "订单列表响应缺少 records/list（服务端可能不支持本次查询参数"
                "或结构已变化），为避免重复下单已停止")
    raise LookupError("订单列表响应缺少 records/list")


def _build_list_prefilter(tasks: list[dict[str, Any]],
                          *,
                          now: _dt.datetime | None = None) -> dict[str, Any]:
    """按目标送达日构造「服务端预筛」查询参数（epoch 毫秒时间窗）。

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

    margin = _dt.timedelta(days=max(0, _SERVER_PREFILTER_MARGIN_DAYS))
    timezone = (now or _dt.datetime.now()).astimezone().tzinfo or _dt.timezone.utc
    start = _dt.datetime.combine(min(days) - margin, _dt.time.min, tzinfo=timezone)
    end = _dt.datetime.combine(max(days) + margin, _dt.time.max, tzinfo=timezone)
    return {
        "startTime": int(start.timestamp() * 1000),
        "endTime": int(end.timestamp() * 1000),
    }


def _list_pending_orders(fetch_json: Callable[[str], dict[str, Any]],
                         tasks: list[dict[str, Any]],
                         *,
                         prefilter: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """分页拉取订单列表并在本地按预约日期/状态过滤，不发任何写请求。

    ``prefilter`` 非空时只作为服务端粗筛条件（缩小翻页范围）；无论服务端返回
    什么，送达日与活跃状态的判定一律由本地过滤负责，判定标准不因预筛改变。
    """
    wanted_days = {_task_fingerprint(task).expected_delivery_time[:10] for task in tasks}
    records: list[dict[str, Any]] = []
    page_no = 1
    page_size = _LIST_PAGE_SIZE
    sweep_started = time.perf_counter()
    page_count = 0
    prefilter_active = bool(prefilter)
    while page_no <= 100:
        query = urlencode({
            "pageNo": page_no,
            "pageSize": page_size,
            "sortType": 1,
            "sort": 1,
            **(prefilter or {}),
        })
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
        _trace(f"list page {page_no}: {page_seconds:.2f}s 返回 {len(page_records)} 条 total={total}")
        if not page_records:
            break
        for record in page_records:
            if _record_active_for_days(record, wanted_days):
                records.append(record)
        if len(page_records) < page_size:
            break
        if total is not None and page_no * page_size >= total:
            break
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


def _reconcile_tasks(tasks: list[dict[str, Any]],
                     fetch_json: Callable[[str], dict[str, Any]],
                     *, created_after: float | None = None,
                     created_before: float | None = None,
                     prefilter: dict[str, Any] | None = None) -> _Reconciliation:
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
    for record in _list_pending_orders(fetch_json, tasks, prefilter=prefilter):
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

    ``prefilter`` 非空时先用服务端粗筛对账；只要粗筛没有得出「全部匹配」，
    就再用无过滤的全量扫描复核一次，以全量结论为准。这样即使服务端将来
    忽略或改变时间窗语义，最坏结果只是多扫一遍，绝不会因为预筛把订单
    筛没而误判「缺失」。

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
        # attempts 配额，因此后面仍会正常进入全量兜底。
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
        # 轮询用尽仍有缺失或查询报错：若这次用了服务端预筛，先用无过滤的全量
        # 扫描确认不是预筛窗口/参数把站内订单挡住，再以全量结论为准。
        if not prefilter:
            break
        why = (f"缺少 {len(reconciliation.missing)} 单" if reconciliation is not None
               else f"查询失败（{last_error}）")
        _emit(callback, f"{label}：服务端预筛{why}，正在用无过滤全量扫描复核")
        try:
            full = _reconcile_tasks(tasks, fetch_json, created_after=created_after)
        except Exception as exc:
            _emit(callback, f"{label}全量复核失败：{exc}；为避免重复下单，后续不会自动提交")
            return None
        _emit_reconciliation(callback, label, full, len(tasks))
        if reconciliation is not None and len(full.confirmed) != len(reconciliation.confirmed):
            _emit(callback, f"{label}：全量复核确认为 {len(full.confirmed)}/{len(tasks)} 单，"
                            f"以全量结果为准")
        return full
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
