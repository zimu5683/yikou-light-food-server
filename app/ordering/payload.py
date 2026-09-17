"""闪时送下单报文组装、门店/地址准备与任务展平。"""

from __future__ import annotations

import datetime as _dt
import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

from app.order.runner import _emit
from app.ordering.constants import (
    DEFAULT_SHEETS,
    _DRY_RUN_PREVIEW_N,
    _FREQUENT_ADDR_PATH,
    _STORE_LIST_PATH,
)
from app.ordering.fingerprint import _payload_fingerprint
from app.ordering.models import OrderFingerprint
from app.ordering.records import _match_record, _pick, _result_records
from app.ordering.workbook import compute_delivery_time


def build_order_payload(order: dict[str, Any], is_dinner: bool, store_id: int,
                        address: dict[str, Any], goods_name: str,
                        now: _dt.datetime | None = None,
                        expected_time: str | None = None,
                        *, client_request_id: str = "",
                        idempotency_field: str = "") -> dict[str, Any]:
    """组装 create-order-from-client 的请求体（字段为 2026-09-05 抓包确认）。

    ``expected_time`` 传入时直接复用（批量下单午餐/晚餐各算一次即可，
    避免每单调 ``datetime.now()`` 导致午夜跨天批次日期不一致）。
    抓包确认的接口目前没有客户端幂等字段；只有调用方明确指定
    ``idempotency_field`` 时才附加稳定 ``client_request_id``，避免向未知
    schema 塞入字段导致服务端拒绝。
    """
    payload = {
        "expectedDeliveryTime": expected_time or compute_delivery_time(is_dinner, now),
        "goodsDetail": [{"goodsName": goods_name, "goodsNum": 1}],
        "orderType": 2,  # 预约单
        "receiveName": str(order.get("name") or ""),
        "receivePhone": str(order.get("phone") or ""),
        "storeId": store_id,
        "receiveAddress": {
            "lnt": address["lnt"],
            "lat": address["lat"],
            "areaCode": address["areaCode"],
            "addressDetail": address["addressDetail"],
            "doorNum": str(order.get("door") or ""),
        },
    }
    if client_request_id and idempotency_field:
        payload[idempotency_field] = client_request_id
    return payload

def _address_from_record(record: dict[str, Any]) -> dict[str, Any]:
    """从常用地址记录提取下单需要的坐标与地址字段。

    站点前端的映射（chunk 反解）：记录的 ``markLnglat`` 子对象携带
    ``longitude/latitude/adcode/address``，顶层字段仅作兜底。
    """
    mark = record.get("markLnglat") if isinstance(record.get("markLnglat"), dict) else {}

    def pick(src: dict[str, Any], keys: tuple[str, ...]) -> Any:
        for key in keys:
            value = src.get(key)
            if value not in (None, ""):
                return value
        return None

    lnt = pick(mark, ("longitude", "lng", "lnt", "lon")) or _pick(record, ("longitude", "lnt", "lng"))
    lat = pick(mark, ("latitude", "lat")) or _pick(record, ("latitude", "lat"))
    area = pick(mark, ("adcode", "areaCode")) or _pick(record, ("areaCode", "code"))
    detail = pick(mark, ("address", "addressDetail")) or _pick(record, ("position", "address", "addressDetail"))
    if lnt in (None, "") or lat in (None, ""):
        raise LookupError("常用地址记录缺少经纬度字段："
                          + json.dumps(record, ensure_ascii=False)[:400])
    return {
        "lnt": float(lnt), "lat": float(lat),
        "areaCode": str(area or ""), "addressDetail": str(detail or ""),
    }

def _fixed_address_from_config(config: Any) -> dict[str, Any]:
    """从配置读取固定地址，供“跳过常用地址匹配”的固定地址模式使用。"""
    lnt = getattr(config, "sss_fixed_lnt", None)
    lat = getattr(config, "sss_fixed_lat", None)
    area_code = str(getattr(config, "sss_fixed_area_code", "") or "")
    detail = str(getattr(config, "sss_fixed_address_detail", "") or "")
    if lnt in (None, "") or lat in (None, ""):
        raise LookupError("固定地址配置缺少经纬度字段："
                          + json.dumps({"lnt": lnt, "lat": lat}, ensure_ascii=False))
    return {
        "lnt": float(lnt), "lat": float(lat),
        "areaCode": area_code, "addressDetail": detail,
    }

def _resolve_store_id(store_payload: dict[str, Any], store_name: str) -> int:
    """从门店列表载荷里解析出门店 id（定字段优先，全文回退）。"""
    store_record = _match_record(
        _result_records(store_payload), store_name, "门店",
        fields=("name", "storeName", "shopName", "title"),
    )
    store_id = _pick(store_record, ("id", "storeId", "storeNo"))
    if store_id in (None, ""):
        raise LookupError("门店记录缺少 id："
                          + json.dumps(store_record, ensure_ascii=False)[:300])
    return int(store_id)

def _resolve_address(addr_payload: dict[str, Any], common_address: str) -> dict[str, Any]:
    """从常用地址列表载荷里解析出下单用坐标与地址字段。"""
    address_record = _match_record(
        _result_records(addr_payload), common_address, "常用地址",
        fields=("contactName", "name", "position", "address", "addressDetail"),
    )
    return _address_from_record(address_record)

def _cached_store_id(config: Any, store_name: str) -> int | None:
    """门店名与缓存一致时直接复用 id，跳过一次门店列表查询。"""
    cached_name = str(getattr(config, "sss_store_name_cached", "") or "")
    cached_id = getattr(config, "sss_store_id", None)
    if cached_id not in (None, "") and cached_name == store_name:
        try:
            return int(cached_id)
        except (TypeError, ValueError):
            return None
    return None

def _save_store_id(config: Any, store_name: str, store_id: int,
                   store_cache_callback: Callable[[str, int], None] | None = None) -> None:
    """记住门店 id 供下次复用；写盘失败不阻断主流程。"""
    try:
        config.sss_store_id = int(store_id)
        config.sss_store_name_cached = store_name
        if hasattr(config, "save"):
            config.save()
    except Exception:
        pass
    if store_cache_callback is not None:
        try:
            store_cache_callback(store_name, int(store_id))
        except Exception:
            # 缓存只是性能优化；桥接层回写失败不应影响当前已确认的门店。
            pass

def _prepare_store_and_address(fetch_json: Callable[[str], dict[str, Any]],
                               config: Any, store_name: str,
                               common_address: str, use_fixed_address: bool,
                               callback: Callable[[str], Any] | None,
                               parallel: bool = True,
                               store_cache_callback: Callable[[str, int], None] | None = None,
                               ) -> tuple[int, dict[str, Any]]:
    """准备下单所需的门店 id 与地址。

    ``fetch_json`` 是 ``client.get_json`` 的 GET 封装。门店 id 命中缓存则跳过
    门店查询；门店与地址查询默认并发发起，``parallel=False`` 时串行，
    便于受限网络下回退排查。
    """
    store_id = _cached_store_id(config, store_name)
    need_store = store_id is None
    need_addr = not use_fixed_address

    store_payload: dict[str, Any] | None = None
    addr_payload: dict[str, Any] | None = None
    if need_store or need_addr:
        jobs: list[tuple[str, str]] = []
        if need_store:
            jobs.append(("store", _STORE_LIST_PATH))
        if need_addr:
            jobs.append(("addr", _FREQUENT_ADDR_PATH))
        if parallel and len(jobs) > 1:
            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = {kind: executor.submit(fetch_json, path)
                           for kind, path in jobs}
                if "store" in futures:
                    store_payload = futures["store"].result()
                if "addr" in futures:
                    addr_payload = futures["addr"].result()
        else:
            for kind, path in jobs:
                if kind == "store":
                    store_payload = fetch_json(path)
                else:
                    addr_payload = fetch_json(path)

    if need_store:
        assert store_payload is not None
        store_id = _resolve_store_id(store_payload, store_name)
        assert store_id is not None
        _save_store_id(config, store_name, store_id, store_cache_callback)
        _emit(callback, f"门店「{store_name}」就绪（id={store_id}）")
    else:
        assert store_id is not None
        _emit(callback, f"门店「{store_name}」命中缓存（id={store_id}），跳过查询")

    if use_fixed_address:
        address = _fixed_address_from_config(config)
        _emit(callback,
              f"使用固定地址：{address['addressDetail']} @ {address['lnt']},{address['lat']}")
    else:
        assert addr_payload is not None
        address = _resolve_address(addr_payload, common_address)
        _emit(callback,
              f"门店「{store_name}」与常用地址「{common_address}」就绪"
              f"（{address['addressDetail']} @ {address['lnt']},{address['lat']}）")
    return store_id, address

def _stable_client_request_id(account: str, sheet: str, row: Any,
                              fingerprint: "OrderFingerprint") -> str:
    """同一账号/表/行/订单内容的稳定 UUID；人工重试时复用同一个值。

    注意：抓包确认的平台接口没有幂等字段，本 ID 默认不发送，仅用于本地
    日志/诊断与未来平台支持幂等字段时的稳定键。
    """
    material = "|".join(("yikou-sss", str(account or ""), str(sheet), str(row),
                         "|".join(fingerprint)))
    return str(uuid.uuid5(uuid.NAMESPACE_URL, material))

def _collect_tasks(orders_by_sheet: dict[str, list[dict[str, Any]]],
                   store_id: int, address: dict[str, Any],
                   goods_name: str, *,
                   account: str = "",
                   batch_id: str = "",
                   idempotency_field: str = "",
                   now: _dt.datetime | None = None) -> list[dict[str, Any]]:
    """把午餐/晚餐两表展平成待下单任务；送达时间每表只算一次。

    每条任务带完整 ``OrderFingerprint``（账号/门店/商品/地址/坐标/类型等）
    以及稳定 ``client_request_id``，避免仅凭姓名+电话+门牌+时间误判。

    ``now`` 由调用方传入时复用同一时刻：日期闸门（云端识别日期 vs 送达日期）
    与报文里的 ``expectedDeliveryTime`` 必须基于同一个瞬间计算，否则在
    16:00 / 午夜这样的边界上会出现两套日期。
    """
    now = now or _dt.datetime.now()
    tasks: list[dict[str, Any]] = []
    for sheet_name in DEFAULT_SHEETS:
        orders = orders_by_sheet.get(sheet_name, [])
        if not orders:
            continue
        is_dinner = sheet_name == "晚餐"
        expected_time = compute_delivery_time(is_dinner, now)
        for order in orders:
            identifier = f"第 {order['row']} 行 {order.get('name') or '未填写'}"
            fingerprint = _payload_fingerprint(
                build_order_payload(order, is_dinner, store_id, address, goods_name,
                                    expected_time=expected_time),
                account=account,
            )
            client_request_id = _stable_client_request_id(account, sheet_name,
                                                          order.get("row", ""), fingerprint)
            payload = build_order_payload(
                order, is_dinner, store_id, address, goods_name,
                expected_time=expected_time,
                client_request_id=client_request_id,
                idempotency_field=idempotency_field,
            )
            tasks.append({
                "sheet": sheet_name,
                "identifier": identifier,
                "payload": payload,
                "fingerprint": fingerprint,
                "account": account,
                "batch_id": batch_id,
                "client_request_id": client_request_id,
            })
    return tasks

def _run_dry_run(tasks: list[dict[str, Any]],
                 callback: Callable[[str], Any] | None) -> dict[str, Any]:
    """干跑：只组装打印报文，不真实提交；只预览前几单全文。"""
    _emit(callback, f"【干跑模式】只组装报文，不真实提交，共 {len(tasks)} 单")
    for index, task in enumerate(tasks):
        if index < _DRY_RUN_PREVIEW_N:
            _emit(callback,
                  f"【干跑】{task['identifier']} 报文："
                  f"{json.dumps(task['payload'], ensure_ascii=False)}")
    if len(tasks) > _DRY_RUN_PREVIEW_N:
        _emit(callback, f"【干跑】…其余 {len(tasks) - _DRY_RUN_PREVIEW_N} 单报文已省略")
    _emit(callback, f"闪时送干跑完成：已组装 {len(tasks)}/{len(tasks)}，未创建真实订单")
    return {
        "status": "dry_run",
        "processed": len(tasks),
        "created": 0,
        "previewed": len(tasks),
        "submitted": 0,
        "reconciled": False,
        "uncertain": False,
    }
