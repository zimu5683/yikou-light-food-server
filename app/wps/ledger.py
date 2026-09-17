"""WPS 云同步的本地账本：记录每人/每天/每表上次同步的餐次。"""

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

    def synced_meals(self, date_key: str, file_id: str,
                     name: str, phone: str) -> int | None:
        """查账本里某人某天在某表上「上次已同步的餐次」；没有记录返回 ``None``。"""
        people = self._batch(date_key, file_id)["people"]
        entry = people.get(f"{name}\u0000{phone}")
        if entry is None:
            return None
        try:
            return int(entry.get("meals", 0))
        except (TypeError, ValueError):
            return None

    def record(self, date_key: str, file_id: str,
               entries: Mapping[str, int]) -> None:
        """把本次写入后的每人餐次记进账本（键为 ``姓名\u0000电话``），并刷新批次时间。"""
        batch = self._batch(date_key, file_id)
        batch["synced_at"] = _dt.datetime.now().isoformat(timespec="seconds")
        people = batch["people"]
        for key, meals in entries.items():
            people[key] = {"meals": int(meals),
                           "at": _dt.datetime.now().isoformat(timespec="seconds")}

    def batch_summary(self, date_key: str, file_id: str) -> dict[str, Any] | None:
        """某天某表的批次摘要 ``{synced_at, people}``；没有批次返回 ``None``。"""
        batch = self.data.get("batches", {}).get(date_key, {}).get(file_id)
        if not batch:
            return None
        return {"synced_at": batch.get("synced_at", ""),
                "people": len(batch.get("people", {}))}
