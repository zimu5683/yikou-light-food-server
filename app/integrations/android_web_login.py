"""APK 内置 WebView 登录兜底。

管理后台 WAF 会识别 Python/OpenSSL 的 TLS 指纹，单纯补请求头仍可能返回
``http_bot_simple`` 403。Android 上改为让 Kotlin 用系统 WebView（Chromium）
在真实浏览器环境里执行同源 ``fetch('/channel/login')``，再把后端原始 JSON 返回给
Python 解析。

本模块只依赖标准库；桌面/Termux 环境返回未启用。
"""
from __future__ import annotations

import json
import os
from typing import Any

#: Kotlin 侧对象名；方法已标注 ``@JvmStatic``。
WEB_LOGIN_CLASS = "com.yikou.lightfood.AdminWebLogin"

APP_MODE_ENV = "YIKOU_APP_MODE"
APP_MODE_ANDROID = "android"


class AndroidWebLoginError(RuntimeError):
    """WebView 登录兜底失败。"""


def is_android() -> bool:
    return os.environ.get(APP_MODE_ENV, "").strip().lower() == APP_MODE_ANDROID


def _java_class() -> Any:
    try:
        from java import jclass  # type: ignore[import-not-found]  # noqa: PLC0415
    except Exception as exc:  # pragma: no cover - 仅桌面环境
        raise AndroidWebLoginError("Chaquopy 未初始化") from exc
    try:
        return jclass(WEB_LOGIN_CLASS)
    except Exception as exc:  # pragma: no cover
        raise AndroidWebLoginError(f"Android WebView 登录类缺失：{WEB_LOGIN_CLASS}") from exc


def webview_fetch(origin: str, path: str, *, method: str = "GET",
                  token: str = "", uniacid: str = "",
                  body: Any = None, timeout_ms: int = 30_000) -> tuple[int, str]:
    """在 Android WebView 的 Chromium 环境里请求管理接口。

    返回 ``(status, body_text)``；后端原始响应体由调用方解析。
    """
    if not is_android():
        raise AndroidWebLoginError("非 Android 环境不启用 WebView 请求")
    body_json = None
    if body is not None:
        body_json = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
    try:
        raw = getattr(_java_class(), "fetchJson")(
            str(origin or ""), str(path or ""), str(method or "GET"),
            str(token or ""), str(uniacid or ""), body_json, int(timeout_ms))
    except AndroidWebLoginError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise AndroidWebLoginError(
            f"WebView 接口请求调用失败：{type(exc).__name__}: {exc}") from exc

    try:
        envelope = json.loads(str(raw))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise AndroidWebLoginError("WebView 接口返回了非法 JSON") from exc
    if not isinstance(envelope, dict):
        raise AndroidWebLoginError("WebView 接口返回结构异常")
    status = int(envelope.get("status") or 0)
    body_text = str(envelope.get("body") or "")
    if status <= 0:
        raise AndroidWebLoginError(body_text[:200] or "WebView 接口请求失败")
    return status, body_text


def webview_login(origin: str, username: str, password: str, *,
                  timeout_ms: int = 30_000) -> dict[str, Any]:
    """通过 Android WebView 执行浏览器登录，返回后端 JSON payload。"""
    if not is_android():
        raise AndroidWebLoginError("非 Android 环境不启用 WebView 登录兜底")
    try:
        raw = getattr(_java_class(), "loginJson")(
            str(origin or ""), str(username or ""), str(password or ""),
            int(timeout_ms))
    except AndroidWebLoginError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise AndroidWebLoginError(f"WebView 登录调用失败：{type(exc).__name__}: {exc}") from exc

    try:
        envelope = json.loads(str(raw))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise AndroidWebLoginError("WebView 登录返回了非法 JSON") from exc
    if not isinstance(envelope, dict) or not envelope.get("ok"):
        detail = ""
        if isinstance(envelope, dict):
            detail = str(envelope.get("message") or envelope.get("body") or "")
        raise AndroidWebLoginError(f"WebView 登录未成功：{detail[:200]}")

    body_raw = envelope.get("body") or "{}"
    try:
        payload = json.loads(str(body_raw))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise AndroidWebLoginError(
            f"WebView 登录响应不是 JSON（HTTP {envelope.get('status')}）") from exc
    if not isinstance(payload, dict):
        raise AndroidWebLoginError("WebView 登录响应结构异常")
    return payload
