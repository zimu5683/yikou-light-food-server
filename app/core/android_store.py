"""Android Keystore 密码存储的 Python 适配层。

APK 内没有系统 keyring，管理后台与闪时送密码由 Kotlin ``SecureStore`` 使用
Android Keystore（AES-GCM）加密后写入应用私有 SharedPreferences。本模块提供与
``keyring`` 相同的 ``get_password / set_password / delete_password`` 形状，让
:mod:`app.core.credentials` 无需判断运行平台。

任何异常都不向上抛：Keystore 不可用、类缺失、写入失败都退化为 ``None/False``，
与 keyring 后端的行为一致（用户每次手输，而不是任务崩溃）。
"""
from __future__ import annotations

import os
from typing import Any

#: Kotlin 侧对象名（方法均标注 ``@JvmStatic``）。
STORE_CLASS = "com.yikou.lightfood.SecureStore"

#: 只有显式 App 模式才接管；Termux 仍走原 keyring / 手动输入逻辑。
APP_MODE_ENV = "YIKOU_APP_MODE"
APP_MODE_ANDROID = "android"


class AndroidStoreError(RuntimeError):
    """Android 密码存储不可用。"""


def is_android() -> bool:
    return os.environ.get(APP_MODE_ENV, "").strip().lower() == APP_MODE_ANDROID


def _store_class() -> Any:
    """返回 Chaquopy ``jclass``；桌面测试中由 monkeypatch 替换。"""
    try:
        from java import jclass  # type: ignore[import-not-found]  # noqa: PLC0415
    except Exception as exc:  # pragma: no cover - 仅桌面解释器会走到
        raise AndroidStoreError("Chaquopy 未初始化") from exc
    try:
        return jclass(STORE_CLASS)
    except Exception as exc:  # pragma: no cover
        raise AndroidStoreError(f"SecureStore 类缺失：{STORE_CLASS}") from exc


class AndroidSecureStoreBackend:
    """适配 ``keyring`` 形状的 Android 原生后端。"""

    def get_password(self, service: str, username: str) -> str | None:
        if not username:
            return None
        try:
            raw = getattr(_store_class(), "getSecret")(str(service or ""), str(username))
        except Exception:  # noqa: BLE001 - 与 keyring 失败语义一致
            return None
        value = "" if raw is None else str(raw)
        return value or None

    def set_password(self, service: str, username: str, password: str) -> bool:
        if not username:
            return False
        try:
            result = getattr(_store_class(), "setSecret")(
                str(service or ""), str(username), str(password))
        except Exception:  # noqa: BLE001
            return False
        return bool(result)

    def delete_password(self, service: str, username: str) -> bool:
        if not username:
            return False
        try:
            result = getattr(_store_class(), "deleteSecret")(
                str(service or ""), str(username))
        except Exception:  # noqa: BLE001
            return False
        return bool(result)


def backend() -> AndroidSecureStoreBackend | None:
    """返回 Android 后端；非 Android 模式返回 ``None`` 让 credentials 走 keyring。"""
    if not is_android():
        return None
    return AndroidSecureStoreBackend()
