"""WPS 云同步的本地账本：记录每人/每天/每表上次同步的餐次。

**这不只是留痕**（2026-09-18 起）：总餐次改成"云端现值 + 本次增量"后，
账本里记的"本批已同步的本地餐次"就是幂等锚点：

    本次增量 = 本地餐次合计 − 账本里的本批本地餐次

同一批重复上传时增量为 0（一个格子都不写）；本地表里加了新的餐，只补差额。
"""

from __future__ import annotations

import copy
import datetime as _dt
import hashlib
import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from app.core.config import user_data_dir
from app.wps.atomicio import FileLock, atomic_write_text, lock_path_for
from app.wps.errors import LedgerCorruptError


def default_state_path() -> Path:
    """同步账本的默认路径：用户配置目录下的 ``wps_sync_state.json``。"""
    return user_data_dir() / "wps_sync_state.json"


def _entry_key(name: str, phone: str) -> str:
    """账本里一个人的键：``姓名\\u0000电话``（与账本文件里的历史格式一致）。"""
    return f"{name}\u0000{phone}"


def _to_int(value: Any) -> int | None:
    """尽力转 int；转不了返回 ``None``（坏数据当"没有记录"，不抛异常）。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _require_entry_number(entry: Mapping[str, Any], key: str) -> None:
    if key in entry and _to_int(entry[key]) is None:
        raise LedgerCorruptError("账本条目数字字段非法")


def _validate_payload(payload: Any) -> dict[str, Any]:
    """校验账本整体结构；任何损坏都抛 :class:`LedgerCorruptError`。

    迁移兼容只接受“旧字段名 + 新校验规则”：旧账本可能只有 ``meals``，
    可能没有 ``version``；但 **必须** 有 ``batches`` 映射，且每个人条目的
    已知数字字段可解析。这样截断 JSON、半截写入、字段类型被改坏的账本会
    失败关闭，而不是被当成空账本把同一批餐再加一遍。
    """
    if not isinstance(payload, dict):
        raise LedgerCorruptError("账本根节点不是 JSON 对象")
    batches = payload.get("batches")
    if not isinstance(batches, dict):
        raise LedgerCorruptError("账本缺少合法的 batches 映射")
    version = payload.get("version", 1)
    if not isinstance(version, int):
        raise LedgerCorruptError("账本 version 不是整数")
    for day_key, day in batches.items():
        if not isinstance(day, dict):
            raise LedgerCorruptError("账本日期层结构非法")
        for file_key, batch in day.items():
            if not isinstance(batch, dict):
                raise LedgerCorruptError("账本批次层结构非法")
            people = batch.get("people")
            if not isinstance(people, dict):
                raise LedgerCorruptError("账本 people 层结构非法")
            for person_key, entry in people.items():
                if not isinstance(person_key, str) or not isinstance(entry, dict):
                    raise LedgerCorruptError("账本人员条目结构非法")
                for numeric_key in ("local", "meals", "total"):
                    _require_entry_number(entry, numeric_key)
                slots = entry.get("slots")
                has_anchor = any(key in entry for key in ("local", "meals", "slots"))
                if not has_anchor:
                    raise LedgerCorruptError("账本人员条目缺少幂等锚点")
                if slots is not None:
                    if not isinstance(slots, (list, tuple)):
                        raise LedgerCorruptError("账本 slots 结构非法")
                    if not slots or any(_to_int(value) is None for value in slots):
                        raise LedgerCorruptError("账本 slots 值非法")
    return payload


def _entry_merge_key(entry: Mapping[str, Any]) -> tuple[str, int]:
    stamp = str(entry.get("at") or "")
    slots = entry.get("slots")
    if isinstance(slots, (list, tuple)):
        weight = sum(_to_int(item) or 0 for item in slots)
    else:
        weight = _to_int(entry.get("local")) or 0
    return stamp, weight


def _merge_people_into(base: dict[str, Any], incoming: Mapping[str, Any]) -> None:
    for person_key, entry in incoming.items():
        if not isinstance(entry, Mapping):
            continue
        other = base.get(person_key)
        if (not isinstance(other, Mapping)
                or _entry_merge_key(entry) >= _entry_merge_key(other)):
            base[person_key] = entry


def _digest_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class SyncLedger:
    """记录"每人在某目标日期上，上一次已同步的餐次数"。"""

    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        self.path = Path(path) if path else default_state_path()
        self.data: dict[str, Any] = {"version": 1, "batches": {}}
        self._loaded_digest: str | None = None
        self._load()

    def _load(self) -> None:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            self._loaded_digest = None
            return
        except (OSError, UnicodeDecodeError) as exc:
            raise LedgerCorruptError("账本文件不可读") from exc
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise LedgerCorruptError("账本 JSON 损坏") from exc
        self.data = _validate_payload(payload)
        self._loaded_digest = _digest_text(raw)

    @property
    def journal_path(self) -> Path:
        """与账本同目录的写入意图日志路径（测试可用临时账本路径注入）。"""
        return Path(str(self.path) + ".journal")

    def _merged_data(self, disk_data: Mapping[str, Any]) -> dict[str, Any]:
        """把本内存批次合并进磁盘最新快照；不同人/不同批次不互相覆盖。

        同一人条目按 ``(at 时间戳, 槽位总量)`` 取新；时间戳相同时保留总量较大的
        一方。这样两个进程各自 ``record()+save()`` 不会用旧快照覆盖对方。
        """
        merged = copy.deepcopy(dict(disk_data))
        merged.setdefault("version", 1)
        batches = merged.setdefault("batches", {})
        for day_key, day in (self.data.get("batches") or {}).items():
            if not isinstance(day, Mapping):
                continue
            target_day = batches.setdefault(day_key, {})
            for file_key, batch in day.items():
                if not isinstance(batch, Mapping):
                    continue
                target_batch = target_day.setdefault(
                    file_key, {"synced_at": "", "people": {}})
                target_batch["synced_at"] = max(
                    str(target_batch.get("synced_at") or ""),
                    str(batch.get("synced_at") or ""))
                people = target_batch.setdefault("people", {})
                _merge_people_into(people, batch.get("people") or {})
        return merged

    def _save_unlocked(self) -> Path:
        """已持有数据锁时的原子落盘；由 :meth:`save` 与 :meth:`merge_entries` 调用。"""
        text = json.dumps(self.data, ensure_ascii=False, indent=2)
        path = atomic_write_text(self.path, text)
        self._loaded_digest = _digest_text(text)
        return path

    def save(self) -> Path:
        """持跨进程数据锁，合并磁盘最新快照后原子落盘。

        journal/ledger 共用 ``<ledger>.lock``；即使两个进程都从旧快照
        ``record()+save()``，后写者也会把先写者的不同批次/人员合并而不是覆盖。
        同一人同槽位的并发更新仍建议由 operation 锁串行化。
        """
        with FileLock(lock_path_for(self.path), timeout=30.0):
            fresh = SyncLedger(self.path)
            merged = self._merged_data(fresh.data)
            fresh.data = merged
            fresh._save_unlocked()
            self.data = merged
            self._loaded_digest = fresh._loaded_digest
        return self.path

    def merge_entries(self, date_key: str, file_id: str,
                      entries: Mapping[str, int | Mapping[str, int]]) -> Path:
        """跨进程安全的账本合并：持锁、重读磁盘、追加本次条目、原子落盘。

        用于“两个进程各自给同一账本追加不同批次/人员”的场景，避免后写者
        用旧内存快照覆盖先写者；已存在的同 key 以本次 ``record`` 语义覆盖
        （与 :meth:`record` 一致，重复恢复相同 slots 不会重复加餐）。
        """
        with FileLock(lock_path_for(self.path), timeout=30.0):
            fresh = SyncLedger(self.path)
            fresh.record(date_key, file_id, entries)
            fresh._save_unlocked()
            self.data = fresh.data
            self._loaded_digest = fresh._loaded_digest
        return self.path

    # ---- 批次 ----

    def _batch(self, date_key: str, file_id: str) -> dict[str, Any]:
        batches = self.data.setdefault("batches", {})
        day = batches.setdefault(date_key, {})
        return day.setdefault(file_id, {"synced_at": "", "people": {}})

    def _person_entry(self, date_key: str, file_id: str,
                      name: str, phone: str) -> Mapping[str, Any] | None:
        """**只读**取一个人的账本条目；任何一层缺失都返回 ``None``。

        刻意不走 :meth:`_batch`：那个会 ``setdefault`` 造出空批次，而
        ``build_plan`` 会**按子表并发**查账本，预览也要求"绝不改状态"。
        """
        batches = self.data.get("batches")
        if not isinstance(batches, Mapping):
            return None
        day = batches.get(date_key)
        if not isinstance(day, Mapping):
            return None
        batch = day.get(file_id)
        if not isinstance(batch, Mapping):
            return None
        people = batch.get("people")
        if not isinstance(people, Mapping):
            return None
        entry = people.get(_entry_key(name, phone))
        return entry if isinstance(entry, Mapping) else None

    def synced_local(self, date_key: str, file_id: str,
                     name: str, phone: str) -> int | None:
        """查"某人本批上次已同步的**本地餐次**"；没有记录返回 ``None``。

        旧版账本（绝对值时代）里只有 ``meals``：那个值就是当时的本地「餐次」
        （旧代码写入的总餐次 = 本地值），因此可以直接当"已同步本地餐次"用 ——
        这保证从旧版本升级后，**同一批不会被重复加一次**。
        """
        entry = self._person_entry(date_key, file_id, name, phone)
        if entry is None:
            return None
        for key in ("local", "meals"):
            if key in entry:
                return _to_int(entry[key])
        return None

    def synced_slots(self, date_key: str, file_id: str,
                     name: str, phone: str) -> list[int] | None:
        """查"某人本批**每个槽位**已同步的本地餐次"；没有记录返回 ``None``。

        槽位 = 本地表的第几行 = 云端这个人的第几行（见
        ``planner._group_rows_per_person``）。一个人一天下两单时本地两行、
        云端两行，所以幂等也要按行记：本地第 2 行对应账本第 2 个槽位。

        旧版账本（一人一行时代）只有 ``local``/``meals``：整体当成第 1 个槽位。
        """
        entry = self._person_entry(date_key, file_id, name, phone)
        if entry is None:
            return None
        slots = entry.get("slots")
        if isinstance(slots, (list, tuple)):
            return [int(value) for value in slots]
        for key in ("local", "meals"):
            if key in entry:
                value = _to_int(entry[key])
                return None if value is None else [value]
        return None

    def synced_total(self, date_key: str, file_id: str,
                     name: str, phone: str) -> int | None:
        """查"上次写入后的云端总餐次"（仅审计/排查用；旧版账本没有这个字段）。"""
        entry = self._person_entry(date_key, file_id, name, phone)
        if entry is None or "total" not in entry:
            return None
        return _to_int(entry["total"])

    def synced_meals(self, date_key: str, file_id: str,
                     name: str, phone: str) -> int | None:
        """历史名字，等价于 :meth:`synced_local`（保留给已有调用与测试）。"""
        return self.synced_local(date_key, file_id, name, phone)

    def record(self, date_key: str, file_id: str,
               entries: Mapping[str, int | Mapping[str, int]]) -> None:
        """把本次写入后的状态记进账本（键为 ``姓名\\u0000电话``），并刷新批次时间。

        值可以是 ``{"local": 本地餐次合计, "slots": [每行餐次], "total": 各槽位总餐次之和}``，
        也可以是单个整数（= 本地餐次合计，兼容旧调用）。``slots`` 是幂等锚点：
        本地第 i 行对应第 i 个槽位，重复上传时逐个槽位算增量。
        """
        batch = self._batch(date_key, file_id)
        stamp = _dt.datetime.now().isoformat(timespec="microseconds")
        batch["synced_at"] = stamp
        people = batch["people"]
        for key, value in entries.items():
            if isinstance(value, Mapping):
                slots = value.get("slots")
                if isinstance(slots, (list, tuple)):
                    clean = [_to_int(item) or 0 for item in slots]
                    payload: dict[str, Any] = {"local": sum(clean), "slots": clean,
                                               "at": stamp}
                else:
                    payload = {"local": _to_int(value.get("local")) or 0, "at": stamp}
                total = _to_int(value.get("total"))
                if total is not None:
                    payload["total"] = total
            else:
                payload = {"local": _to_int(value) or 0, "at": stamp}
            people[key] = payload

    def batch_summary(self, date_key: str, file_id: str) -> dict[str, Any] | None:
        """某天某表的批次摘要 ``{synced_at, people}``；没有批次返回 ``None``。"""
        batch = self.data.get("batches", {}).get(date_key, {}).get(file_id)
        if not batch:
            return None
        return {"synced_at": batch.get("synced_at", ""),
                "people": len(batch.get("people", {}))}
