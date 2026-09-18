"""WPS 云同步的本地账本：记录每人/每天/每表上次同步的餐次。

**这不只是留痕**（2026-09-18 起）：总餐次改成"云端现值 + 本次增量"后，
账本里记的"本批已同步的本地餐次"就是幂等锚点：

    本次增量 = 本地餐次合计 − 账本里的本批本地餐次

同一批重复上传时增量为 0（一个格子都不写）；本地表里加了新的餐，只补差额。
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from app.core.config import user_data_dir


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


class SyncLedger:
    """记录"每人在某目标日期上，上一次已同步的餐次数"。"""

    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        self.path = Path(path) if path else default_state_path()
        self.data: dict[str, Any] = {"version": 1, "batches": {}}
        self._load()

    def _load(self) -> None:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return
        if isinstance(payload, dict) and isinstance(payload.get("batches"), dict):
            self.data = payload

    def save(self) -> Path:
        """原子写账本（先写临时文件再 ``os.replace``），返回落盘路径。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(self.data, ensure_ascii=False, indent=2)
        fd, tmp = tempfile.mkstemp(prefix=f".{self.path.name}.", suffix=".tmp",
                                   dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
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
            return [int(value) for value in slots if _to_int(value) is not None]
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
        stamp = _dt.datetime.now().isoformat(timespec="seconds")
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
