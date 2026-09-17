"""订单领域通用小工具（供 runner / fetching / writer 等共用）。"""

from __future__ import annotations

from typing import Any, Callable


def _emit(callback: Callable[[str], Any] | None, message: str) -> None:
    if callback:
        callback(message)
