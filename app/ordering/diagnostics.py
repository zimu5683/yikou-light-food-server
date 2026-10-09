"""提交诊断仅保存关联标识、摘要和分类，不保存报文、响应正文或凭据。"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
from pathlib import Path
from threading import Lock
from typing import Any

from app.core.config import user_data_dir


_WRITE_LOCK = Lock()
_HEX_RE = re.compile(r"^[0-9a-f]{12,64}$")
_LOCAL_ID_RE = re.compile(r"^[a-zA-Z0-9-]{1,64}$")


def payload_digest(payload: dict[str, Any]) -> str:
    material = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(material).hexdigest()[:16]


def diagnostic_dir() -> Path:
    """提交诊断日志目录（``sss-diagnostics/``，只读查看/导出入口用）。"""
    return user_data_dir() / "sss-diagnostics"


def diagnostic_log_path() -> Path:
    return diagnostic_dir() / f"{dt.date.today().isoformat()}.jsonl"


def submission_diagnostic(task: dict[str, Any], response: Any, *, started_at: float,
                          elapsed_s: float, state: str, payload_hash: str,
                          transport: dict[str, Any] | None = None) -> dict[str, Any]:
    identifier = str(task.get("identifier") or "")
    row = re.search(r"第\s*(\d+)\s*行", identifier)
    result: dict[str, Any] = {
        "started_at": round(started_at, 3), "elapsed_s": round(elapsed_s, 3),
        "state": state, "payload_digest": payload_hash,
    }
    if row:
        result["row"] = int(row.group(1))
    sheet = task.get("sheet")
    if sheet in ("午餐", "晚餐"):
        result["sheet"] = sheet
    for key in ("batch_id", "client_request_id"):
        value = str(task.get(key) or "")
        if _LOCAL_ID_RE.fullmatch(value):
            result[key] = value
    metadata = getattr(response, "diagnostics", None) or transport or {}
    status = metadata.get("http_status")
    if isinstance(status, int) and not isinstance(status, bool) and 100 <= status <= 599:
        result["http_status"] = status
    for key in ("wire_digest", "auth_context", "cookie_context"):
        value = str(metadata.get(key) or "")
        if _HEX_RE.fullmatch(value):
            result[key] = value
    terminal = metadata.get("terminal")
    if terminal in ("web", "present", "absent"):
        result["terminal"] = terminal
    if isinstance(response, dict):
        result["response_kind"] = "object"
        success = response.get("success")
        if success is None or isinstance(success, bool) or type(success) is int:
            result["response_success"] = success
        else:
            result["response_success"] = "unknown"
        code = response.get("code", response.get("errorCode", response.get("error_code")))
        if re.fullmatch(r"[0-9]{1,6}", str(code)):
            result["response_code"] = int(code)
        message = str(response.get("message") or response.get("msg") or "")
        exception = re.search(r"\b(?:java|javax|org|com)\.[\w.]*Exception\b", message)
        if exception:
            result["server_exception"] = exception.group(0)
    elif response is not None:
        result["response_kind"] = "array" if isinstance(response, list) else "unknown"
    else:
        result["response_kind"] = "not_received"
    return result


def write_submission_diagnostic(record: dict[str, Any]) -> None:
    data = (json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    target = diagnostic_log_path()
    with _WRITE_LOCK:
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(target, flags, 0o600)
        try:
            os.write(fd, data)
        finally:
            os.close(fd)
