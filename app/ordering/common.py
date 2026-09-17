"""闪时送模块通用的小工具（日志埋点、文本清理）。"""

from __future__ import annotations

import os
import sys
import time
from typing import Any


def _clean(value: Any) -> Any:
    return value.strip() if isinstance(value, str) else value

_SSS_TRACE_ENABLED = os.environ.get("YIKOU_SSS_TRACE", "").strip().lower() not in (
    "", "0", "false", "no", "off")

def _trace(message: str) -> None:
    """把耗时埋点写到标准错误，不影响业务事件日志。"""
    if not _SSS_TRACE_ENABLED:
        return
    stamp = time.strftime("%H:%M:%S")
    print(f"[sss-trace {stamp}] {message}", file=sys.stderr, flush=True)
