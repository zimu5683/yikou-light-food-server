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
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from app.core.config import user_data_dir
from app.wps.atomicio import (FileLock, atomic_write_text, lock_path_for,
                              preserve_file, rotate_backups)
from app.wps.errors import LedgerCorruptError


def default_state_path() -> Path:
    """同步账本的默认路径：用户配置目录下的 ``wps_sync_state.json``。"""
    return user_data_dir() / "wps_sync_state.json"


def _preserve_unreadable(path: Path) -> None:
    """损坏/不可读时先把原件留档。

    下一次成功写盘会用 ``os.replace`` 原子替换掉坏文件，证据就永远没了 ——
    而账本是**唯一的幂等锚点**，坏文件里往往还能人工救回大部分内容。
    留档失败（磁盘满/无权限）绝不能把「损坏」变成「崩溃」。
    """
    try:
        preserve_file(path, tag="corrupt")
    except Exception:  # noqa: BLE001 - 留档是尽力而为，不能盖过失败关闭
        pass


def _entry_key(name: str, phone: str) -> str:
    """账本里一个人的键：``姓名\\u0000电话``（与账本文件里的历史格式一致）。"""
    return f"{name}\u0000{phone}"


def _to_int(value: Any) -> int | None:
    """尽力转 int；转不了返回 ``None``（坏数据当"没有记录"，不抛异常）。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _entry_problem(entry: Any) -> str:
    """一个人的账本条目有什么毛病；没问题返回空字符串。

    **两边共用同一条规则**：严格校验拿它决定"整本账本失败关闭"，抢救
    （:func:`salvage_payload`）拿它决定"只丢这一条"。共用是刻意的 —— 否则
    抢救过的账本可能过不了严格校验，等于白救一场。消息文本与历史版本逐字一致。
    """
    if not isinstance(entry, dict):
        return "账本人员条目结构非法"
    for numeric_key in ("local", "meals", "total"):
        if numeric_key in entry and _to_int(entry[numeric_key]) is None:
            return "账本条目数字字段非法"
    if not any(key in entry for key in ("local", "meals", "slots")):
        return "账本人员条目缺少幂等锚点"
    slots = entry.get("slots")
    if slots is not None:
        if not isinstance(slots, (list, tuple)):
            return "账本 slots 结构非法"
        if not slots or any(_to_int(value) is None for value in slots):
            return "账本 slots 值非法"
    # 槽位身份（订单号）：有就必须与 slots 等长，否则按身份配对的前提不成立 ——
    # 宁可失败关闭，也不要用错位身份算出错的增量。
    orders = entry.get("orders")
    if orders is not None:
        if (not isinstance(orders, (list, tuple))
                or not all(isinstance(value, str) for value in orders)
                or not isinstance(slots, (list, tuple))
                or len(orders) != len(slots)):
            return "账本 orders 与 slots 不一致"
    # 槽位→物理行位置：同上，等长是"槽位在云端这个人的第几行"的前提。
    positions = entry.get("positions")
    if positions is not None:
        if (not isinstance(positions, (list, tuple))
                or not all(_to_int(value) is not None for value in positions)
                or not isinstance(slots, (list, tuple))
                or len(positions) != len(slots)):
            return "账本 positions 与 slots 不一致"
    return ""


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
                if not isinstance(person_key, str):
                    raise LedgerCorruptError("账本人员条目结构非法")
                problem = _entry_problem(entry)
                if problem:
                    raise LedgerCorruptError(problem)
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

    @classmethod
    def empty(cls, path: str | os.PathLike[str] | None = None) -> "SyncLedger":
        """构造一个**不读盘**的空账本。

        只给「账本损坏/丢失后的重建」入口用（:func:`rebuild_anchors`）。正常路径
        一律走 :meth:`__init__`：账本损坏时必须失败关闭，**绝不能**把损坏静默
        当成空账本 —— 那会让整批餐再加一遍。
        """
        obj = cls.__new__(cls)
        obj.path = Path(path) if path else default_state_path()
        obj.data = {"version": 1, "batches": {}}
        obj._loaded_digest = None
        return obj

    def _load(self) -> None:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            self._loaded_digest = None
            return
        except (OSError, UnicodeDecodeError) as exc:
            _preserve_unreadable(self.path)
            raise LedgerCorruptError("账本文件不可读") from exc
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            _preserve_unreadable(self.path)
            raise LedgerCorruptError("账本 JSON 损坏") from exc
        try:
            self.data = _validate_payload(payload)
        except LedgerCorruptError:
            # 结构校验失败也是损坏：半截写入/字段被改坏的账本同样留档再失败关闭。
            _preserve_unreadable(self.path)
            raise
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
        """已持有数据锁时的原子落盘；由 :meth:`save` 与 :meth:`merge_entries` 调用。

        落盘前先尽力留档（``<账本>.bak`` 多代 + 每日快照，见
        :func:`app.wps.atomicio.rotate_backups`）：账本是唯一幂等锚点，一次误删/
        误覆盖就是整批重复加餐。留档必须在**数据锁内**做，否则并发的两份写会把
        ``.bak`` 留成半新半旧的内容。
        """
        rotate_backups(self.path)
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

    def synced_orders(self, date_key: str, file_id: str,
                      name: str, phone: str) -> list[str] | None:
        """查"某人本批**每个槽位**对应的本地订单号"；没有记录返回 ``None``。

        与 :meth:`synced_slots` 等长（未知身份是空字符串）。旧账本没有这个字段
        （升级后第一批）→ ``None``：这时只能沿用按行号的位置配对，本批会把
        订单号补记下来供下一批核对。
        """
        entry = self._person_entry(date_key, file_id, name, phone)
        if entry is None:
            return None
        orders = entry.get("orders")
        if not isinstance(orders, (list, tuple)):
            return None
        return [str(value or "") for value in orders]

    def synced_positions(self, date_key: str, file_id: str,
                         name: str, phone: str) -> list[int] | None:
        """查"某人每个槽位在云端这个人的**第几行**"；没有记录返回 ``None``。

        与 :meth:`synced_slots` 等长（``0`` = 这一槽位没有对应的云端行）。
        槽位序是**身份序**（历史写入序），而云端表的物理行序会随补单插入与地址
        排序变化 —— 补单新行插到既有行上方、或排序把同一人跨地址组重排后，
        两者不再相等，按数组位置 ``cloud_rows[slot-1]`` 配对就会把增量写到别人的
        行上（人级合计仍对，执行仍报成功）。位置按"在这个人的行里排第几"记，
        插行/删行不会让它失效。

        旧账本没有这个字段（升级后第一批）→ ``None``：只能沿用按数组位置配对，
        本批写入后会把位置补记下来供下一批核对。
        """
        entry = self._person_entry(date_key, file_id, name, phone)
        if entry is None:
            return None
        positions = entry.get("positions")
        if not isinstance(positions, (list, tuple)):
            return None
        values = [_to_int(value) for value in positions]
        if any(value is None for value in values):
            return None
        return [int(value) for value in values]

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
               entries: Mapping[str, int | Mapping[str, Any]]) -> None:
        """把本次写入后的状态记进账本（键为 ``姓名\\u0000电话``），并刷新批次时间。

        值可以是 ``{"local": 已同步量合计, "slots": [每槽位已同步量],
        "orders": [每槽位订单号], "positions": [每槽位在云端这个人的第几行],
        "total": 槽位总餐次之和}``，也可以是单个整数（= 已同步量合计，兼容旧调用）。
        ``slots`` 是幂等锚点：本地第 i 行对应第 i 个槽位，重复上传时逐个槽位算
        增量；``orders`` 是槽位身份，本地行序变化时靠它把槽位与订单对上（D02）；
        ``positions`` 是槽位的物理位置，云端行序被插入/排序改过时靠它配对
        （都与 ``slots`` 等长，未知值分别是空字符串与 0）。
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
                    # 槽位身份：只接受与 slots 等长的列表，长短不一的坏数据
                    # 宁可不记（校验器会拒绝读），也不要写进账本。
                    orders = value.get("orders")
                    if isinstance(orders, (list, tuple)) and len(orders) == len(clean):
                        payload["orders"] = [str(item or "") for item in orders]
                    # 槽位→物理位置：同样只接受与 slots 等长、可解析的列表。
                    positions = value.get("positions")
                    if (isinstance(positions, (list, tuple))
                            and len(positions) == len(clean)
                            and all(_to_int(item) is not None for item in positions)):
                        payload["positions"] = [int(_to_int(item) or 0)
                                                for item in positions]
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


#: 抢救报告里最多列出几条被丢弃的条目（超大文件不能把响应撑爆）。
SALVAGE_REPORT_LIMIT = 64


def salvage_payload(payload: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    """尽力抢救：逐层丢掉坏掉的日期/表/人，返回 ``(救回来的 payload, 报告)``。

    账本结构是「日期 → 表 → 人 → 槽位」互相独立的映射，所以**一处坏不该整本赔葬**：
    哪一层烂了丢哪一层，好的一律原样留下。判据与 :func:`_validate_payload` 完全同源
    （都走 :func:`_entry_problem`），因此抢救结果一定还能被严格校验读回来。

    报告里的 ``dropped`` 逐条写明丢的是**谁**：那些人的"这一批加没加过"没有锚点了，
    接下来只能靠云端现状去推断，用户得知道自己要核对谁。
    """
    dropped: list[dict[str, Any]] = []
    if not isinstance(payload, dict):
        return {"version": 1, "batches": {}}, {
            "kept_people": 0, "kept_batches": 0, "dropped_people": 0,
            "dropped_batches": 0, "dropped": [], "dropped_total": 0,
            "unsalvageable": "账本根节点不是 JSON 对象", "version_note": ""}
    batches = payload.get("batches")
    if not isinstance(batches, dict):
        return {"version": 1, "batches": {}}, {
            "kept_people": 0, "kept_batches": 0, "dropped_people": 0,
            "dropped_batches": 0, "dropped": [], "dropped_total": 0,
            "unsalvageable": "账本缺少合法的 batches 映射", "version_note": ""}

    version = payload.get("version", 1)
    version_note = ""
    if not isinstance(version, int) or isinstance(version, bool):
        version = 1
        version_note = "账本 version 不是整数，已按 1 处理"

    kept: dict[str, Any] = {}
    for day_key, day in batches.items():
        if not isinstance(day_key, str) or not isinstance(day, dict):
            dropped.append({"scope": "day", "day": str(day_key)[:64],
                            "reason": "账本日期层结构非法"})
            continue
        kept_day: dict[str, Any] = {}
        for file_key, batch in day.items():
            if not isinstance(file_key, str) or not isinstance(batch, dict):
                dropped.append({"scope": "batch", "day": day_key,
                                "file": str(file_key)[:64],
                                "reason": "账本批次层结构非法"})
                continue
            people = batch.get("people")
            if not isinstance(people, dict):
                dropped.append({"scope": "batch", "day": day_key, "file": file_key,
                                "reason": "账本 people 层结构非法"})
                continue
            kept_people: dict[str, Any] = {}
            for person_key, entry in people.items():
                name, separator, phone = str(person_key).partition("\u0000")
                if not isinstance(person_key, str):
                    dropped.append({"scope": "person", "day": day_key,
                                    "file": file_key, "name": name[:64],
                                    "phone": phone[:32],
                                    "reason": "账本人员条目结构非法"})
                    continue
                problem = _entry_problem(entry)
                if problem:
                    dropped.append({"scope": "person", "day": day_key,
                                    "file": file_key, "name": name[:64],
                                    "phone": phone[:32], "reason": problem})
                    continue
                kept_people[person_key] = entry
            kept_batch = {key: value for key, value in batch.items()
                          if key != "people"}
            kept_batch["people"] = kept_people
            kept_day[file_key] = kept_batch
        kept[day_key] = kept_day

    # 根节点上除 batches/version 之外的字段原样保留（未来新增字段不该被抢救吃掉），
    # 但上一次抢救的审计记录要丢掉 —— 它由本次写回时重新写。
    result = {key: value for key, value in payload.items()
              if key not in ("batches", "version", "salvage")}
    result["version"] = version
    result["batches"] = kept
    dropped_people = sum(1 for item in dropped if item["scope"] == "person")
    report = {
        "kept_people": sum(len(batch.get("people") or {})
                           for day in kept.values() for batch in day.values()),
        "kept_batches": sum(len(day) for day in kept.values()),
        "dropped_people": dropped_people,
        "dropped_batches": len(dropped) - dropped_people,
        "dropped": dropped[:SALVAGE_REPORT_LIMIT],
        "dropped_total": len(dropped),
        "unsalvageable": "",
        "version_note": version_note,
    }
    return result, report


def salvage_ledger(path: str | os.PathLike[str], *, note: str = "",
                   actor: str = "local", dry_run: bool = False) -> dict[str, Any]:
    """把一个结构受损的账本**就地**抢救回来；``dry_run=True`` 时只读不写。

    只给「账本抢救」入口用，且必须是人工确认之后才写。三种救不了的情况各自返回
    稳定的 ``status``：文件不存在 ``missing``、读不出来 ``unreadable``、
    连 JSON 都解析不了 ``parse_failed``（那条路走「账本锚点重建」）。

    写之前先留档原件（``<账本>.pre-salvage-<内容摘要>``），写完由调用方回读证明。
    **不写云端、不碰意图日志。**
    """
    target = Path(path)
    empty_report: dict[str, Any] = {
        "kept_people": 0, "kept_batches": 0, "dropped_people": 0,
        "dropped_batches": 0, "dropped": [], "dropped_total": 0,
        "unsalvageable": "", "version_note": ""}

    def failure(status: str, reason: str) -> dict[str, Any]:
        return {"ok": False, "status": status, "reason": reason, "changed": False,
                "path": str(target), "backup": "", "report": dict(empty_report)}

    if dry_run:
        return _salvage_locked_read(target, failure)

    with FileLock(lock_path_for(target), timeout=30.0):
        return _salvage_locked_read(target, failure, write=True,
                                    note=note, actor=actor)


def _salvage_locked_read(target: Path, failure: Any, *, write: bool = False,
                         note: str = "", actor: str = "") -> dict[str, Any]:
    """读盘 → 抢救 → 需要时写回。``write=False`` 时绝不创建任何文件。"""
    try:
        raw = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return failure("missing", "账本文件不存在，没有可抢救的内容")
    except (OSError, UnicodeDecodeError) as exc:
        return failure("unreadable", f"账本文件不可读：{type(exc).__name__}")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return failure("parse_failed",
                       "账本 JSON 损坏（连解析都过不去），抢救不了；"
                       "请走「账本锚点重建」，原件已留档")
    salvaged, report = salvage_payload(payload)
    if report["unsalvageable"]:
        return {**failure("unsalvageable", report["unsalvageable"]),
                "report": report}
    if not write:
        return {"ok": True, "status": "salvage_ready", "reason": "", "changed": False,
                "path": str(target), "backup": "", "report": report}
    if not report["dropped_people"] and not report["dropped_batches"] \
            and not report["version_note"]:
        # 什么都没坏：一个字节都不写（抢救不该变成一次无谓的重写）。
        return {"ok": True, "status": "nothing_to_salvage",
                "reason": "账本没有任何需要抢救的内容", "changed": False,
                "path": str(target), "backup": "", "report": report}
    # 自查：抢救结果必须能通过严格校验。过不了宁可不写（那说明规则两边不同源，
    # 是程序 bug，绝不能把读不回来的文件写进去）。
    try:
        _validate_payload(salvaged)
    except LedgerCorruptError as exc:
        return {**failure("self_check_failed", f"抢救结果自查未通过：{exc}"),
                "report": report}
    backup = preserve_file(target, tag="pre-salvage")
    stamp = _dt.datetime.now().isoformat(timespec="microseconds")
    salvaged["salvage"] = {
        "at": stamp, "actor": str(actor or ""), "note": str(note or ""),
        "dropped_people": report["dropped_people"],
        "dropped_batches": report["dropped_batches"],
    }
    atomic_write_text(target, json.dumps(salvaged, ensure_ascii=False, indent=2))
    return {"ok": True, "status": "salvaged", "reason": "", "changed": True,
            "path": str(target), "backup": str(backup) if backup else "",
            "salvaged_at": stamp, "report": report}


def rebuild_anchors(path: str | os.PathLike[str],
                    batches: Sequence[tuple[str, str, Mapping[str, Any]]], *,
                    note: str = "", source: str = "", actor: str = "local"
                    ) -> dict[str, Any]:
    """以给定条目重建（或补写）账本锚点，先留档原件，整批一次落盘。

    ``batches`` 是 ``[(date_key, file_id, entries), ...]``；**一次锁、一次留档、
    一次原子落盘**，所以多张表的重建不会在中间留下半批状态。

    **只给「账本重建」入口用**（``app.wps.rebuild`` / Bridge 的
    ``wps_ledger_rebuild``）。正常上传路径一律走 :meth:`SyncLedger.record` +
    :meth:`save`；只有这里允许在账本损坏时**从空账本重来** —— 因为原件已经留档，
    而且调用方必须先拿到人工逐字确认。

    健康账本上**其它日期/其它表的锚点原样保留**，只有本次这几批被覆盖。
    """
    target = Path(path)
    with FileLock(lock_path_for(target), timeout=30.0):
        backup = preserve_file(target, tag="pre-rebuild")
        replaced_corrupt = False
        try:
            fresh = SyncLedger(target)
        except LedgerCorruptError:
            # 原件已留档；损坏账本没法合并，只能从空账本重建这几批。
            fresh = SyncLedger.empty(target)
            replaced_corrupt = True
        stamp = _dt.datetime.now().isoformat(timespec="microseconds")
        people = 0
        slots = 0
        written_batches: list[dict[str, Any]] = []
        for date_key, file_id, entries in batches:
            clean = {str(key): value for key, value in dict(entries).items()
                     if isinstance(value, Mapping)}
            if not clean:
                continue
            fresh.record(str(date_key), str(file_id), clean)
            batch = fresh._batch(str(date_key), str(file_id))
            batch["rebuilt_at"] = stamp
            batch["rebuilt_note"] = str(note or "")
            batch["rebuilt_source"] = str(source or "")
            batch["rebuilt_actor"] = str(actor or "")
            batch_people = len(clean)
            batch_slots = sum(len(value.get("slots") or []) for value in clean.values())
            people += batch_people
            slots += batch_slots
            written_batches.append({"date_key": str(date_key),
                                    "file_id": str(file_id),
                                    "people": batch_people,
                                    "slots": batch_slots})
        if written_batches:
            fresh._save_unlocked()
        return {
            "path": str(target),
            "people": people,
            "slots": slots,
            "batches": written_batches,
            "changed": bool(written_batches),
            "replaced_corrupt": replaced_corrupt,
            "backup": str(backup) if backup else "",
            "rebuilt_at": stamp,
        }
