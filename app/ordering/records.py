"""闪时送接口返回记录的取值、匹配与小工具。"""

from __future__ import annotations

import json
from typing import Any


def _substitute_tokens(step: dict[str, Any], **tokens: str) -> dict[str, Any]:
    """Deep-copy a step and replace ``{token}`` placeholders in candidate strings."""
    copied = json.loads(json.dumps(step))

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in list(node.items()):
                if isinstance(value, str):
                    for token, replacement in tokens.items():
                        value = value.replace("{" + token + "}", replacement)
                    node[key] = value
                else:
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(copied)
    return copied

def _pick(record: dict[str, Any], keys: tuple[str, ...]) -> Any:
    """按优先级从记录里取第一个非空字段。"""
    for key in keys:
        value = record.get(key)
        if value not in (None, ""):
            return value
    return None

def _unwrap_scalar(value: Any) -> Any:
    """把列表/元组字段摊平成首个标量。

    闪时送订单列表把电话返回成数组（如 ``["18545726939"]``），直接
    ``str()`` 会得到 ``"['18545726939']"``，与下单报文的单值永远不相等。
    """
    if isinstance(value, (list, tuple)):
        for item in value:
            if isinstance(item, dict):
                continue
            if item not in (None, ""):
                return item
        return None
    return value

def _pick_scalar(record: dict[str, Any], keys: tuple[str, ...]) -> Any:
    """同 ``_pick``，但把数组字段取首元素，适配列表接口的数组化字段。"""
    for key in keys:
        value = _unwrap_scalar(record.get(key))
        if value not in (None, ""):
            return value
    return None

def _result_records(payload: dict[str, Any]) -> list:
    """从 {result: {records: [...]}} 或 {result: [...]} 里取记录列表。"""
    result = payload.get("result")
    if isinstance(result, list):
        return result
    if isinstance(result, dict):
        records = result.get("records") or result.get("list")
        if isinstance(records, list):
            return records
    return []

def _match_record(records: Any, keyword: str, what: str,
                  fields: tuple[str, ...] = ()) -> dict[str, Any]:
    """按关键词匹配记录；先定字段精确匹配，找不到再回退全文。

    ``fields`` 为优先匹配的字段名（如门店的 name、地址的 contactName）。
    定字段匹配逐字段做子串判断，避免对每条记录做 ``json.dumps`` 全文
    序列化；回退全文只用于兼容未知字段结构。
    """
    if not isinstance(records, list) or not records:
        raise LookupError(f"{what}列表为空或格式异常")
    if keyword:
        for record in records:
            if not isinstance(record, dict):
                continue
            for key in fields:
                value = record.get(key)
                if value not in (None, "") and keyword in str(value):
                    return record
        for record in records:
            if keyword in json.dumps(record, ensure_ascii=False):
                return record
    if len(records) == 1:
        single = records[0]
        if isinstance(single, dict):
            return single
        raise LookupError(f"{what}列表格式异常")
    raise LookupError(
        f"{what}列表里没有匹配「{keyword}」的记录，共 {len(records)} 条："
        + json.dumps(records, ensure_ascii=False)[:600])
