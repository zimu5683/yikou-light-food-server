"""Android 原生 ``WpsRuntime`` 的 Python 适配层。

桌面 / Termux 走 :mod:`app.wps.cli` 里的 ``subprocess``；APK 内没有可以合法
``fork/exec`` 整套用户空间的路径，因此由 Kotlin 侧的 ``WpsRuntime`` 负责
proot + kdocs-cli + DNS/CA/授权浏览器。本模块是 Python 侧唯一的调用入口，
接口与 ``design/APK-PLAN.md`` 第 2.2/2.3 节保持一致。

设计约束：
* 只有 ``YIKOU_APP_MODE=android`` 时才接管，桌面与 Termux 行为完全不变；
* 不复制、不记录 token；``authorize`` 只把授权 URL 交给调用方写日志；
* Java/Kotlin 异常在这里统一转成带 ``errorCode`` 的 :class:`ExecResult` /
  :class:`AndroidRuntimeError`，上层不直接接触 Chaquopy。
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

#: ``find_cli`` 在 Android 模式下返回的占位标记，不要求真实文件存在。
RUNTIME_MARKER = "@android-runtime"

#: Kotlin 侧对象名（Java 反射视角；Kotlin ``object`` 的方法已加 ``@JvmStatic``）。
RUNTIME_CLASS = "com.yikou.lightfood.WpsRuntime"

#: APK 启动时由 Kotlin 写入的环境变量；只有显式设置了它才启用原生路径。
APP_MODE_ENV = "YIKOU_APP_MODE"
APP_MODE_ANDROID = "android"


class AndroidRuntimeError(RuntimeError):
    """原生 WpsRuntime 不可用或调用失败，消息可直接展示给用户。"""

    def __init__(self, message: str, *, error_code: str = "RUNTIME_UNAVAILABLE") -> None:
        super().__init__(message)
        self.error_code = error_code


@dataclass(frozen=True)
class ExecResult:
    """与 Kotlin ``ExecResult`` 对齐的命令执行结果。"""

    exit_code: int = -1
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    error_code: str | None = None

    @property
    def ok(self) -> bool:
        return not self.timed_out and self.error_code is None and self.exit_code == 0


@dataclass(frozen=True)
class AuthResult:
    """一次完整授权流程的结果。"""

    ok: bool
    url: str = ""
    error_code: str | None = None
    message: str = ""


def is_android() -> bool:
    """是否为 APK 内置模式。

    故意**不**用 ``sys.platform`` 或 Android 内核信息判断：Termux 也是 Android，
    本项目的 Termux 路径必须继续走 ``subprocess`` + proot。Kotlin 启动时显式设置
    ``YIKOU_APP_MODE=android``，这是唯一开关。
    """
    return os.environ.get(APP_MODE_ENV, "").strip().lower() == APP_MODE_ANDROID


def _runtime_class() -> Any:
    """返回 Chaquopy ``jclass``；在桌面测试中由 monkeypatch 替换。"""
    try:
        from java import jclass  # type: ignore[import-not-found]  # noqa: PLC0415
    except Exception as exc:  # pragma: no cover - 仅 APK 之外会走到
        raise AndroidRuntimeError(
            "Android 原生运行时不可用（Chaquopy 未初始化）",
            error_code="RUNTIME_UNAVAILABLE",
        ) from exc
    try:
        return jclass(RUNTIME_CLASS)
    except Exception as exc:  # pragma: no cover - APK 类缺失时才发生
        raise AndroidRuntimeError(
            f"Android 原生运行时类缺失：{RUNTIME_CLASS}",
            error_code="RUNTIME_UNAVAILABLE",
        ) from exc


def _call(method: str, *args: Any) -> Any:
    """调用 Kotlin 静态方法；只传字符串 / 整数，避免 Java 类型擦除歧义。"""
    runtime = _runtime_class()
    try:
        function = getattr(runtime, method)
    except AttributeError as exc:
        raise AndroidRuntimeError(
            f"Android 原生运行时缺少方法：{method}",
            error_code="RUNTIME_UNAVAILABLE",
        ) from exc
    try:
        return function(*args)
    except Exception as exc:  # noqa: BLE001 - Kotlin/Python 异常都转成统一错误
        name = type(exc).__name__
        raise AndroidRuntimeError(
            f"Android 原生运行时调用失败（{method}）：{name}: {exc}",
            error_code="RUNTIME_CRASHED",
        ) from exc


def _decode_json(raw: Any, *, default: Any = None) -> Any:
    """把 Java 字符串或 Python mapping 解码成对象；坏数据返回 ``default``。"""
    if isinstance(raw, (dict, list)):
        return raw
    if raw is None:
        return default
    try:
        return json.loads(str(raw))
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


def _as_mapping(raw: Any) -> dict[str, Any]:
    value = _decode_json(raw, default={})
    return value if isinstance(value, dict) else {}


def _as_int(value: Any, default: int = -1) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def run_cli(args: Sequence[str], params: Mapping[str, Any] | None = None, *,
            timeout_ms: int = 300_000) -> ExecResult:
    """在 Android 原生兼容层里运行一次 kdocs-cli 命令。

    ``args`` 不含 ``--file``，参数通过 ``params`` 传入临时 JSON；Kotlin 组装命令并
    在结束后删除临时文件。不会把 token 拼到命令行。
    """
    args_json = json.dumps([str(item) for item in args], ensure_ascii=False)
    params_json = None if params is None else json.dumps(
        params, ensure_ascii=False, separators=(",", ":"))
    raw = _call("runJson", args_json, params_json, int(timeout_ms))
    data = _as_mapping(raw)
    result = ExecResult(
        exit_code=_as_int(data.get("exitCode")),
        stdout=str(data.get("stdout") or ""),
        stderr=str(data.get("stderr") or ""),
        timed_out=bool(data.get("timedOut", False)),
        error_code=str(data["errorCode"]) if data.get("errorCode") else None,
    )
    if data and "exitCode" not in data and data.get("ok") is False:
        return ExecResult(exit_code=-1, stderr=result.stderr, timed_out=result.timed_out,
                          error_code=result.error_code or "RUNTIME_CRASHED")
    return result


def auth_status() -> bool:
    """读取授权状态。

    ``auth status`` 本身返回 false 是正常状态；但如果原生层连命令都没跑起来
    （PROOT_MISSING / DNS_FAILED / TLS_CA_FAILED），必须抛出带 errorCode 的错误，
    否则界面只会显示「未授权」，用户拿不到可诊断的原因。
    """
    try:
        data = _as_mapping(_call("authStatusJson"))
    except AndroidRuntimeError:
        return False
    if not data:
        return False
    authenticated = bool(data.get("authenticated", False))
    if not authenticated and data.get("errorCode"):
        raise AndroidRuntimeError(
            str(data.get("message") or "auth status 执行失败"),
            error_code=str(data["errorCode"]),
        )
    if "authenticated" in data:
        return authenticated
    # 兼容直接返回布尔 JSON 的实现。
    return bool(_decode_json(data, default=False))


def begin_authorization() -> dict[str, Any]:
    """启动原生授权流程，立即返回状态 JSON（URL 稍后由浏览器 shim 写入）。"""
    return _as_mapping(_call("beginAuthorizeJson"))


def authorization_state() -> dict[str, Any]:
    """读取当前授权流程的快照（running / url / authenticated / errorCode）。"""
    return _as_mapping(_call("authorizationStateJson"))


def cancel_authorization() -> dict[str, Any]:
    """取消正在进行的授权；同一时刻只允许一个流程。"""
    try:
        return _as_mapping(_call("cancelAuthorizeJson"))
    except AndroidRuntimeError:
        return {"ok": True}


def _safe_state() -> dict[str, Any]:
    try:
        return authorization_state()
    except AndroidRuntimeError as exc:
        return {"running": False, "finished": True, "ok": False,
                "errorCode": exc.error_code, "message": str(exc)}


def authorize(*, timeout_ms: int = 330_000, poll_interval_s: float = 1.0,
              on_url: Callable[[str], None] | None = None) -> AuthResult:
    """等待用户在浏览器里完成 WPS OAuth 授权。

    Python 负责轮询原生状态并把 URL 交给 ``on_url``（Bridge 用它写日志），
    Kotlin 的 ``AuthCoordinator`` 负责实际拉起 Custom Tabs 并用文件监听拿到 URL。
    ```
    """
    started = begin_authorization()
    if started.get("ok") is False:
        return AuthResult(
            ok=False,
            error_code=str(started.get("errorCode") or "AUTH_START_FAILED"),
            message=str(started.get("message") or "无法启动 WPS 授权流程"),
        )

    reported_url = ""
    deadline = time.monotonic() + max(0.0, timeout_ms / 1000.0)
    interval = max(0.1, float(poll_interval_s))
    while time.monotonic() < deadline:
        state = _safe_state()
        url = str(state.get("url") or "").strip()
        if url and url != reported_url:
            reported_url = url
            if on_url is not None:
                try:
                    on_url(url)
                except Exception:  # noqa: BLE001 - 日志回调失败不能中断授权
                    pass
        if state.get("authenticated") or (state.get("finished") and state.get("ok")):
            return AuthResult(ok=True, url=reported_url or url)
        if state.get("finished") and not state.get("running"):
            return AuthResult(
                ok=False,
                url=reported_url or url,
                error_code=str(state.get("errorCode") or "AUTH_FAILED"),
                message=str(state.get("message") or "授权未完成"),
            )
        time.sleep(interval)

    cancel_authorization()
    return AuthResult(
        ok=False,
        url=reported_url,
        error_code="TIMEOUT",
        message=f"授权等待超时（{int(timeout_ms / 1000)} 秒），请重试",
    )


def logout() -> ExecResult:
    """退出 WPS 授权，供前端/诊断页使用。"""
    return run_cli(["auth", "logout"], timeout_ms=60_000)


def notify_interaction(kind: str) -> bool:
    """通知原生层弹出「等待验证码/等待地址确认」前台通知。"""
    if not str(kind or "").strip():
        return False
    try:
        raw = _call("notifyInteraction", str(kind))
    except AndroidRuntimeError:
        return False
    return bool(raw)


def clear_interaction_notifications() -> bool:
    """用户回应交互后取消对应的前台通知。"""
    try:
        raw = _call("clearInteraction")
    except AndroidRuntimeError:
        return False
    return bool(raw)


def open_external(url: str) -> bool:
    """用系统浏览器打开外部链接；失败返回 False，不抛异常给 Bridge。"""
    if not str(url or "").strip():
        return False
    try:
        raw = _call("openExternal", str(url))
    except AndroidRuntimeError:
        return False
    if raw is None:
        return False
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() in {"true", "1"}


def diagnostics() -> dict[str, Any]:
    """返回原生运行时诊断快照（不含 token/密码）。"""
    try:
        data = _as_mapping(_call("diagnosticsJson"))
    except AndroidRuntimeError as exc:
        return {"ok": False, "errorCode": exc.error_code, "message": str(exc)}
    return data


def files_dir() -> Path | None:
    """返回 Kotlin 注入的应用私有目录；仅供诊断与兜底定位。"""
    raw = os.environ.get("YIKOU_FILES_DIR") or os.environ.get("HOME") or ""
    return Path(raw) if raw else None
