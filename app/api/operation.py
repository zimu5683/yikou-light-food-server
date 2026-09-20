"""统一操作互斥与断线可查状态。

设计边界：

* ``OperationCoordinator`` 内部只有一把 ``threading.RLock``，且只在
  “占位 / 更新内存状态 / 查询”这些微秒级操作中持有；**绝不包住**云端请求、
  文件读写或等待交互。因此 ``operation_status`` / ``bridge_ready`` / resolve_*
  不会被正在上传的线程阻塞。
* ``try_reserve`` 是原子占位：同一时刻最多一个互斥操作；冲突立即返回，
  不等待、不排队，避免 Web 请求线程被长任务拖死。
* ``finish`` 幂等：无论是正常完成、异常 finally 还是超时恢复，重复调用都只
  接受第一次终结状态；后续调用不再覆盖最近结果。

当前覆盖的动作（mode）：

``order`` / ``sss`` / ``wps_upload`` / ``wps_authorize`` / ``wps_logout`` /
``check_update`` / ``install_update``。
"""
from __future__ import annotations

import secrets
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any


def _as_id(value: Any) -> str:
    """接受操作对象、字符串等，统一取出 operation_id。"""
    if isinstance(value, Operation):
        return value.operation_id
    return str(value or "").strip()


@dataclass
class Operation:
    """一次互斥操作的公共状态，读者只会拿到 ``as_dict()`` 的副本。"""

    operation_id: str
    mode: str
    status: str = "running"
    phase: str = ""
    reason: str = ""
    next_action: str = ""
    summary: dict[str, Any] = field(default_factory=dict)
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None

    def as_dict(self, *, active: bool | None = None) -> dict[str, Any]:
        if active is None:
            active = self.status == "running"
        return {
            "operation_id": self.operation_id,
            "mode": self.mode,
            "status": self.status,
            "active": bool(active),
            "phase": self.phase,
            "reason": self.reason,
            "next_action": self.next_action,
            "summary": dict(self.summary),
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


@dataclass(frozen=True)
class Reservation:
    """``try_reserve`` 的结果：granted=True 时 operation 一定非空。"""

    operation: Operation | None = None
    conflict: Operation | None = None

    @property
    def granted(self) -> bool:
        return self.operation is not None and self.conflict is None


class OperationCoordinator:
    """进程内互斥协调器（每个 Bridge 实例一个，网页版共享同一个 Bridge）。"""

    def __init__(self, *, max_recent: int = 20) -> None:
        self._lock = threading.RLock()
        self._seq = 0
        self._active: Operation | None = None
        self._recent: deque[Operation] = deque(maxlen=max(1, int(max_recent)))

    # -- 占位/释放 ----------------------------------------------------
    def try_reserve(self, mode: str, *, summary: dict[str, Any] | None = None,
                    next_action: str = "", phase: str = "") -> Reservation:
        """原子占位；已有活动操作时返回冲突方，不阻塞调用者。"""
        clean_mode = str(mode or "unknown").strip() or "unknown"
        with self._lock:
            if self._active is not None:
                return Reservation(operation=None, conflict=self._active)
            self._seq += 1
            operation = Operation(
                operation_id=f"op-{self._seq:06d}-{secrets.token_hex(3)}",
                mode=clean_mode,
                summary=dict(summary or {}),
                next_action=str(next_action or ""),
                phase=str(phase or ""),
            )
            self._active = operation
            return Reservation(operation=operation, conflict=None)

    def update(self, operation: Any, *, phase: str | None = None,
               next_action: str | None = None,
               summary: dict[str, Any] | None = None,
               reason: str | None = None) -> bool:
        """更新活动操作的进度；已终结或非活动操作返回 False。"""
        operation_id = _as_id(operation)
        if not operation_id:
            return False
        with self._lock:
            active = self._active
            if active is None or active.operation_id != operation_id:
                return False
            if phase is not None:
                active.phase = str(phase or "")
            if next_action is not None:
                active.next_action = str(next_action or "")
            if reason is not None:
                active.reason = str(reason or "")
            if summary:
                merged = dict(active.summary)
                merged.update(summary)
                active.summary = merged
            return True

    def finish(self, operation: Any, *, status: str = "success",
               reason: str = "", next_action: str = "",
               summary: dict[str, Any] | None = None) -> bool:
        """终结活动操作并移入最近列表；重复调用不会覆盖第一次结果。

        ``status="running"`` 会被视为编程错误并按 ``success`` 处理，避免出现
        “最近列表里躺着一条仍在 active 的操作”。业务异常请显式传 ``error``。
        """
        operation_id = _as_id(operation)
        if not operation_id:
            return False
        clean_status = str(status or "success").strip() or "success"
        if clean_status == "running":
            clean_status = "success"
        with self._lock:
            active = self._active
            if active is None or active.operation_id != operation_id:
                return False
            active.status = clean_status
            # 终结时显式覆盖：否则最近结果会残留启动时“等待任务结束”的提示。
            active.reason = str(reason or "")
            active.next_action = str(next_action or "")
            if summary:
                merged = dict(active.summary)
                merged.update(summary)
                active.summary = merged
            active.finished_at = time.time()
            self._recent.appendleft(active)
            self._active = None
            return True

    def finish_if_active(self, operation: Any, *, status: str = "error",
                         reason: str = "operation_aborted",
                         next_action: str = "") -> bool:
        """安全网：只在该操作仍是活动操作时终结它，用于 finally 兜底。"""
        operation_id = _as_id(operation)
        if not operation_id:
            return False
        with self._lock:
            active = self._active
            if active is None or active.operation_id != operation_id:
                return False
            return self.finish(active, status=status, reason=reason,
                               next_action=next_action)

    # -- 查询 ----------------------------------------------------------
    def is_active(self, operation: Any) -> bool:
        operation_id = _as_id(operation)
        if not operation_id:
            return False
        with self._lock:
            return self._active is not None and self._active.operation_id == operation_id

    def active_operation_id(self) -> str:
        with self._lock:
            return self._active.operation_id if self._active is not None else ""

    def status(self, operation_id: str = "") -> dict[str, Any]:
        """返回统一状态；指定未知 ID 时 ``ok=False``，其余情况 ``ok=True``。"""
        wanted = str(operation_id or "").strip()
        with self._lock:
            active = self._active
            recent = list(self._recent)

            def _operations() -> list[dict[str, Any]]:
                items: list[dict[str, Any]] = []
                if active is not None:
                    items.append(active.as_dict(active=True))
                items.extend(item.as_dict(active=False) for item in recent)
                return items

            if wanted:
                if active is not None and active.operation_id == wanted:
                    payload = active.as_dict(active=True)
                else:
                    match = next((item for item in recent
                                  if item.operation_id == wanted), None)
                    if match is None:
                        return {
                            "ok": False,
                            "active": False,
                            "operation_id": wanted,
                            "mode": "",
                            "status": "not_found",
                            "phase": "",
                            "reason": "operation_not_found",
                            "next_action": "operation_status",
                            "summary": {},
                            "started_at": None,
                            "finished_at": None,
                            "operations": _operations(),
                        }
                    payload = match.as_dict(active=False)
            elif active is not None:
                payload = active.as_dict(active=True)
            elif recent:
                payload = recent[0].as_dict(active=False)
            else:
                return {
                    "ok": True,
                    "active": False,
                    "operation_id": "",
                    "mode": "",
                    "status": "idle",
                    "phase": "",
                    "reason": "",
                    "next_action": "",
                    "summary": {},
                    "started_at": None,
                    "finished_at": None,
                    "operations": [],
                }

            payload["ok"] = True
            payload["operations"] = _operations()
            return payload


__all__ = ["Operation", "OperationCoordinator", "Reservation"]
