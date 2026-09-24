"""网页版 HTTP 服务器：把 :class:`app.api.bridge.Bridge` 暴露为 JSON API。

用途：把运行任务的机器（例如 Termux 手机）当服务器，其他设备用浏览器打开网址
即可操作本项目全部任务（订单处理 / 云文档同步 / 闪时送下单）。

设计要点：

* **直接复用** :class:`app.api.bridge.Bridge` 的全部公开方法，不复制业务逻辑。
* **不需要 WebSocket/SSE**：桥接层的事件通道本来就是「Python 只追加 + 前端用
  ``last_sequence`` 轮询 ``drain_events`` 并 ACK」的设计（见 ``bridge.py`` 顶部注释），
  天然适配 HTTP 请求/响应。
* **不使用全局调用锁**：交互式流程（验证码 / 决策 / 地址补录）要求 worker 线程阻塞
  等待的同时，另一个请求还能调用 ``resolve_*``；加全局锁会直接死锁。因此沿用
  Bridge 自身的锁与 worker 生命周期模型。
* **强制账号鉴权**：本应用持有管理后台密码、WPS 云文档授权，并能真实下单，因此
  除登录页外的所有路径都要求有效会话；``/api/*`` 未授权返回 401。

文件对话框：网页版没有原生对话框，改用**服务器端文件浏览器**（``/api/fs/list``）——
客户端选中的必须是运行任务那台机器上的路径，而不是客户端自己的文件系统。
"""
from __future__ import annotations

import json
import os
import secrets
import socket
import sys
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlparse

from app import __version__
from app.api.bridge import Bridge
from app.core.config import user_data_dir
from app.web.auth import (SESSION_TTL_SECONDS, STATUS_PENDING, AccessVerifier,
                       AuthError, AuthStore, access_verifier_from_env,
                       default_auth_dir)
from app.web.fs_browser import EXCEL_SUFFIXES as _EXCEL_SUFFIXES, browse_root, list_dir
from app.web.static_files import StaticFileError, read_static
import app.web.pages

#: 默认端口。选一个不常用的高位端口，避免和 Termux 里其它服务撞车。
DEFAULT_PORT = 8756

#: 不通过 HTTP 暴露的方法：attach 由服务端启动时内部调用，走 HTTP 没有意义。
_NON_HTTP = frozenset({"attach"})

#: 兼容旧导入：文件浏览器过滤后缀。
EXCEL_SUFFIXES = _EXCEL_SUFFIXES

#: 会话 Cookie 名。HttpOnly 由服务端设置，前端不需要（也不能）读取它。
SESSION_COOKIE = "yikou_session"

#: 免登录路径：探活、登录页、提交登录/注册、登出。
_PUBLIC_PATHS = frozenset({"/healthz", "/login", "/logout"})

#: 角色为管理员才可访问的路径。
_ADMIN_PATHS = frozenset({"/admin"})

#: 网关转发过来的请求会带这些头。用于区分「本地直连」与「公网入口」。
_PROXY_HEADERS = ("cf-connecting-ip", "cf-ray", "x-forwarded-for", "x-forwarded-proto")

#: 非管理员可以调用的桥接方法（白名单 —— 未列出的**一律 403**）。
#:
#: 采用白名单而不是黑名单：以后 `Bridge` 新增任何方法，默认都是「仅管理员」，
#: 不会因为忘记登记而意外对普通用户开放。
#:
#: 放行判据：发起/停止任务、看进度日志、回应交互（验证码/决策/待确认地址），
#: 以及 WPS 的**预览与上传**（按使用要求开放，注意上传会真的写云文档）。
#: 被拦下的都是「能改服务器状态或泄漏数据」的：保存配置、文件浏览、清除密码、
#: 检查更新/安装、窗口控制、授权 WPS 等。
#:
#: 特别说明：``wps_recovery_resolve``（管理员恢复/退场旧 pending 任务）**故意不在
#: 本白名单内** —— 未登记即默认仅管理员。Bridge 内部还会再校验一次
#: ``is_admin``，直接调用（脚本/旧客户端绕过 HTTP）同样返回 forbidden。
_NON_ADMIN_METHODS = frozenset({
    # 任务控制
    "start_order", "start_sss", "stop_task", "worker_alive",
    # 握手与进度
    "bridge_ready", "drain_events", "status", "log",
    # 交互回应（任务在等这些输入才能继续）
    "resolve_decision", "resolve_captcha", "resolve_address_input",
    # 云文档同步：预览 + 上传
    "wps_status", "wps_preview", "wps_upload", "wps_check_copies",
    # 闪时送每日订单查询（只读）
    "sss_day_orders",
    # 操作断线查询（订单/闪时送/云上传/授权/更新的 active/最近结果）
    "operation_status",
    # 只读恢复：WPS 恢复对账 + pending 交互列表（普通用户只会看到自己的交互）
    "wps_recovery_status", "pending_interactions",
})


class _RecoveryUser:
    """本地旧令牌对应的身份：本机/局域网直连时可用，等价于管理员。

    存在意义是「自救」：忘记管理员密码、或账号文件损坏时，仍能在手机本地用
    ``?token=`` 进审批页。公网入口永远不会走到这里（见 ``_legacy_token_ok``）。
    """

    username = "local-recovery"
    role = "admin"
    status = "approved"

    @property
    def is_admin(self) -> bool:
        return True


class WebServerError(RuntimeError):
    """网页版启动失败（端口占用、前端产物缺失等）。"""


# ----------------------------------------------------------------------
# 令牌
# ----------------------------------------------------------------------
def _token_path() -> Path:
    return user_data_dir() / "web_token"


def load_or_create_token(rotate: bool = False) -> str:
    """读取访问令牌，缺失或被要求轮换时新建一个并落盘。

    持久化是为了让客户端设备能收藏一个稳定的网址；权限收紧到 ``0o600``。
    """
    path = _token_path()
    if not rotate:
        try:
            existing = path.read_text(encoding="utf-8").strip()
        except OSError:
            existing = ""
        if existing:
            return existing
    token = secrets.token_urlsafe(24)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(token + "\n", encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        # Android 的部分挂载点不支持 chmod；令牌文件仍受应用私有目录保护。
        pass
    return token


# ----------------------------------------------------------------------
# 网络信息
# ----------------------------------------------------------------------
def _interface_addresses() -> list[tuple[str, str]]:
    """枚举各网卡的 IPv4 地址。

    Android 上 ``ifconfig`` 读不到 ``/proc/net/dev``（Permission denied），
    因此用 ioctl ``SIOCGIFADDR`` 直接问内核要地址。

    本项目只跑 Android / Termux / Linux，``fcntl`` 必然可用；万一不可用就返回空列表
    （启动不报错，只是不打印局域网网址）——不再保留 Windows 的 gethostbyname_ex 兜底。
    """
    try:
        import fcntl
        import struct
    except ImportError:  # pragma: no cover - 非 POSIX 平台不支持
        return []

    siocgifaddr = 0x8915
    found: list[tuple[str, str]] = []
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        for _, name in socket.if_nameindex():
            if name == "lo":
                continue
            try:
                packed = fcntl.ioctl(
                    sock.fileno(), siocgifaddr, struct.pack("256s", name.encode()[:15]))
            except OSError:
                # 网卡没有 IPv4 地址（未连接 / 蜂窝数据未分配地址）时跳过。
                continue
            found.append((name, socket.inet_ntoa(packed[20:24])))
    except OSError:
        pass
    finally:
        sock.close()
    return found


def lan_addresses() -> list[tuple[str, str]]:
    """按「客户端最可能连得上」的顺序返回候选地址。

    WiFi（wlan0）优先，其次以太网/热点，``tun*``（VPN）排最后——只有同样在该
    VPN 里的设备才连得上。**不能用默认路由探测来选地址**：本机默认路由走 VPN，
    探测出来的 172.19.x.x 是其它设备根本连不上的地址。
    """
    candidates = _interface_addresses()
    if not candidates:
        return []
    preferred = {"wlan0": 0, "eth0": 1, "ap0": 2, "rndis0": 3, "wlan1": 4}

    def rank(item: tuple[str, str]) -> tuple[int, str]:
        name = item[0]
        if name.startswith(("tun", "ppp")):
            return (9, name)
        return (preferred.get(name, 5), name)

    return sorted(candidates, key=rank)


# ----------------------------------------------------------------------
# 服务端运行时适配层
# ----------------------------------------------------------------------
class _WebWindow:
    """只实现 :class:`Bridge` 在服务端真正需要的一个能力：``destroy``。

    ``request_close`` 通过它回调 ``on_destroy`` 来停止 HTTP 服务。网页版没有
    原生窗口，也没有可最小化/最大化的东西，因此不再承载桌面窗口动作。
    """

    def __init__(self, on_destroy: Any = None) -> None:
        self.uid = "server-runtime"
        self._on_destroy = on_destroy
        self._closed = threading.Event()

    def destroy(self) -> None:
        """关闭服务：由 request_close 调用，幂等。"""
        if self._closed.is_set():
            return
        self._closed.set()
        if self._on_destroy is not None:
            self._on_destroy()


# ----------------------------------------------------------------------
# HTTP 处理
# ----------------------------------------------------------------------
class _Handler(BaseHTTPRequestHandler):
    server_version = f"yikou-light-food/{__version__}"
    protocol_version = "HTTP/1.1"

    # -- 基础设施 ------------------------------------------------------
    def log_message(self, fmt: str, *args: Any) -> None:
        """默认实现往 stderr 刷每一条请求；轮询 drain_events 会淹没终端，故静音。"""

    @property
    def _bridge(self) -> Bridge:
        return self.server.bridge  # type: ignore[attr-defined]

    @property
    def _dist(self) -> Path:
        return self.server.dist_dir  # type: ignore[attr-defined]

    def _drain_body(self) -> None:
        """回错误前把未读取的请求体读掉。

        keep-alive 下如果直接回响应而留下未读 body，剩余字节会被当成下一个请求，
        导致后续请求整体错位。
        """
        if self._body_read:
            return
        self._body_read = True
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length > 0:
            try:
                self.rfile.read(length)
            except OSError:
                pass

    def _send_bytes(self, body: bytes, status: int = 200,
                    content_type: str = "application/octet-stream",
                    extra_headers: dict[str, str] | None = None) -> None:
        """发送一个完整响应。

        客户端随时可能中途断开（浏览器取消、手机切网、隧道侧掐断回源连接）。
        这时写 socket 会抛 BrokenPipeError/ConnectionResetError；如果不吞掉，
        它会变成未捕获异常，让 Cloudflare 判定「源站无响应」并给访客返回
        **502 Bad Gateway**。真实案例：输错密码本应看到"密码不正确"的提示，
        结果因为这次断连变成了 502 报错页。

        因此这里把「写失败」当作正常情况处理：对端已经走了，没有别的补救动作，
        静静收场即可（Debug 级别留痕，便于排查但不刷屏）。
        """
        self._drain_body()
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            for key, value in (extra_headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError) as exc:
            self._note_closed(exc)
        except OSError as exc:
            # 写超时（ETIMEDOUT）等其它 socket 级失败同样不该 500。
            self._note_closed(exc)
            self.close_connection = True

    def _note_closed(self, exc: OSError) -> None:
        """客户端已断开：关掉 keep-alive，不再尝试复用这条连接。"""
        self.close_connection = True
        if getattr(self.server, "log_closed_connections", False):  # pragma: no cover
            print(f"客户端提前断开，响应未送达：{type(exc).__name__}: {exc}", file=sys.stderr)

    def _send_json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self._send_bytes(body, status, "application/json; charset=utf-8")

    def _send_error_json(self, status: int, message: str, code: str = "") -> None:
        self._send_json({"error": message, "code": code or f"http_{status}"}, status)

    # -- 账号与会话 ----------------------------------------------------
    @property
    def _auth(self) -> AuthStore:
        return self.server.auth  # type: ignore[attr-defined]

    @property
    def _access(self) -> AccessVerifier:
        return self.server.access  # type: ignore[attr-defined]

    @property
    def _is_proxied(self) -> bool:
        """请求是否经网关（Cloudflare 隧道）进来。

        这个判断决定旧令牌还能不能用：旧令牌是单一静态口令，一旦允许它在公网入口
        生效，就等于绕过了整套「申请-审批」流程。因此它只对本地/局域网直连有效。
        """
        return any(self.headers.get(h) for h in _PROXY_HEADERS)

    def _cookie_token(self) -> str:
        raw = self.headers.get("Cookie", "")
        for part in raw.split(";"):
            name, sep, value = part.strip().partition("=")
            if sep and name == SESSION_COOKIE:
                return value.strip()
        return ""

    def _supplied_token(self) -> str:
        """客户端提供的凭据：先看请求头，再看会话 Cookie，最后看查询串。"""
        supplied = (self.headers.get("X-Yikou-Token") or "").strip()
        if supplied:
            return supplied
        if cookie := self._cookie_token():
            return cookie
        query = parse_qs(urlparse(self.path).query)
        return (query.get("token") or [""])[0].strip()

    def _legacy_token_ok(self) -> bool:
        """旧的静态令牌：仅本地直连有效，且公网入口一律不认。"""
        expected = self.server.token  # type: ignore[attr-defined]
        if not expected or self._is_proxied:
            return False
        supplied = (self.headers.get("X-Yikou-Token") or "").strip()
        if not supplied:
            supplied = (parse_qs(urlparse(self.path).query).get("token") or [""])[0].strip()
        return bool(supplied) and secrets.compare_digest(supplied, expected)

    def _current_user(self) -> Any:
        """把请求凭据解析成账号；会话（账号体系）优先，其次本地旧令牌。"""
        supplied = self._supplied_token()
        if supplied:
            user = self._auth.resolve_session(supplied)
            if user is not None:
                return user
        if self._legacy_token_ok():
            return _RecoveryUser()
        return None

    def _wants_json(self, path: str) -> bool:
        """/api/* 一律给 JSON；页面请求给可读的 HTML/重定向。"""
        return path.startswith("/api/") or "application/json" in (self.headers.get("Accept") or "")

    def _safe_next(self, value: str) -> str:
        """只接受站内相对路径，避免 ``next=//evil.com`` 型开放重定向。"""
        value = (value or "").strip()
        if not value.startswith("/") or value.startswith("//"):
            return "/"
        return value

    def _session_cookie(self, token: str, *, clear: bool = False) -> str:
        parts = [f"{SESSION_COOKIE}=", "Path=/", "HttpOnly", "SameSite=Lax"]
        if clear:
            parts[0] = f"{SESSION_COOKIE}="
            parts.append("Max-Age=0")
        else:
            parts[0] = f"{SESSION_COOKIE}={token}"
            parts.append(f"Max-Age={SESSION_TTL_SECONDS}")
        if self._is_secure_request():
            parts.append("Secure")
        return "; ".join(parts)

    def _is_secure_request(self) -> bool:
        proto = (self.headers.get("X-Forwarded-Proto") or "").split(",")[0].strip().lower()
        return proto == "https"

    # -- 路由拦截 ------------------------------------------------------
    def parse_request(self) -> bool:
        """在解析请求的过程中做统一鉴权。

        为什么挂在 ``parse_request`` 而不是 ``handle_one_request``：正常流程是
        「handle_one_request → parse_request → do_* → 读 body」，鉴权只要插在
        ``parse_request`` 成功之后即可，body 还留在流里由 ``do_*`` 照常读取。
        早先的写法在 handle_one_request 里手动 readline+parse 再回填 BytesIO，
        会因为丢弃未读的 body 字节而让 POST 解析错位（400 bad_arguments）。

        放在这里的第二个好处：这是所有路由的唯一入口，新加路由不可能绕过鉴权。
        """
        if not super().parse_request():
            return False
        # 到这里头部已解析、body 仍未读取。先初始化 _body_read，
        # 因为拒绝分支会调 _drain_body()，而它原本由 do_GET/do_POST 初始化。
        self._body_read = False
        path = urlparse(self.path).path
        # 先清掉复用连接线程可能残留的上一个请求角色/身份；无论后续鉴权成功/失败，
        # 都不会让“未鉴权请求”继承上一请求的管理员身份或非本人交互。
        self._bridge.set_request_is_admin(None)
        self._bridge.set_request_identity(None)
        if path not in _PUBLIC_PATHS:
            user = self._current_user()
            if user is None:
                self._reject_unauthenticated(path)
                return False
            # Bridge 是长生命周期对象；角色写入请求线程的 ContextVar，
            # 而不是改写共享实例属性，避免并发请求互相借用管理员权限。
            self._bridge.set_request_is_admin(
                bool(getattr(user, "is_admin", False)))
            self._bridge.set_request_identity(
                str(getattr(user, "username", "") or ""))
        return True

    def _reject_unauthenticated(self, path: str) -> None:
        if self._wants_json(path):
            self._send_error_json(
                HTTPStatus.UNAUTHORIZED,
                "需要登录：请先在 /login 登录后再操作",
                "login_required",
            )
            return
        self._send_redirect("/login?next=" + quote(path, safe=""))

    def _access_ok(self) -> bool:
        """管理页的 Cloudflare Access 校验。

        未配置（``self._access.enabled`` 为假）时返回 True，即「暂时只靠账号密码”；
        配置之后就变成强制项 —— 目的是让「忘记配置」表现为不可用，而不是静默放开。
        """
        verifier = self._access
        if not verifier.enabled:
            return True
        email = verifier.verify(self.headers.get("Cf-Access-Jwt-Assertion", ""))
        if email:
            self._access_email = email
            return True
        return False

    # -- 页面 ----------------------------------------------------------
    def _send_html(self, body: str, status: int = 200) -> None:
        self._send_bytes(body.encode("utf-8"), status, "text/html; charset=utf-8",
                         {"Cache-Control": "no-store",
                          "Content-Security-Policy":
                              "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
                              "base-uri 'none'; frame-ancestors 'none'",
                          "Referrer-Policy": "no-referrer"})

    def _send_redirect(self, location: str) -> None:
        self._send_bytes(b"", HTTPStatus.FOUND, "text/plain; charset=utf-8",
                         {"Location": location, "Cache-Control": "no-store"})

    def _read_form(self) -> dict[str, str]:
        """读取 ``application/x-www-form-urlencoded`` 表单体。"""
        self._body_read = True
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        # 表单很小，1MB 上限足以防住恶意的超大请求体。
        raw = self.rfile.read(min(length, 1 << 20)) if length > 0 else b""
        try:
            parsed = parse_qs(raw.decode("utf-8"), keep_blank_values=True)
        except UnicodeDecodeError:
            return {}
        return {k: (v[0] if v else "") for k, v in parsed.items()}

    def _client_ip(self) -> str:
        forwarded = (self.headers.get("CF-Connecting-IP")
                     or self.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
        return forwarded or self.client_address[0]

    def _serve_login(self) -> None:
        """登录页。已登录的人直接送回应用，不必重复登录。"""
        if self._current_user() is not None:
            self._send_redirect("/")
            return
        query = parse_qs(urlparse(self.path).query)
        error = (query.get("error") or [""])[0]
        notice = (query.get("notice") or [""])[0]
        next_url = self._safe_next((query.get("next") or ["/"])[0])
        self._send_html(app.web.pages.login_page(
            error=error, notice=notice, next_url=next_url,
            pending_count=self._auth.pending_count(),
        ))

    def _serve_logout(self) -> None:
        self._auth.close_session(self._supplied_token())
        self._send_bytes(b"", HTTPStatus.FOUND, "text/plain; charset=utf-8",
                         {"Location": "/login?notice=" + quote("已退出登录"),
                          "Set-Cookie": self._session_cookie("", clear=True),
                          "Cache-Control": "no-store"})

    def _handle_login_post(self) -> None:
        """处理登录 / 注册表单提交。"""
        form = self._read_form()
        action = (form.get("action") or "login").strip()
        next_url = self._safe_next(form.get("next") or "/")
        username = (form.get("username") or "").strip()
        password = form.get("password") or ""

        try:
            if action == "register":
                user, auto = self._auth.register(
                    username, password, invite_code=form.get("invite_code") or "",
                    remote=self._client_ip())
                notice = ("注册成功，已凭邀请码直接通过，请登录。" if auto
                          else "访问申请已提交，请等待管理员批准后再登录。")
                self._send_redirect("/login?next=" + quote(next_url, safe="")
                                    + "&notice=" + quote(notice))
                return
            user = self._auth.authenticate(username, password, remote=self._client_ip())
        except AuthError as exc:
            self._send_redirect("/login?next=" + quote(next_url, safe="")
                                + "&error=" + quote(str(exc)))
            return

        session = self._auth.open_session(user.username, remote=self._client_ip())
        self._send_bytes(b"", HTTPStatus.FOUND, "text/plain; charset=utf-8",
                         {"Location": next_url,
                          "Set-Cookie": self._session_cookie(session.token),
                          "Cache-Control": "no-store"})

    # -- 管理员审批页 --------------------------------------------------
    def _serve_admin(self) -> None:
        user = self._current_user()
        if user is None:
            self._reject_unauthenticated("/admin")
            return
        if not getattr(user, "is_admin", False):
            self._send_denied("无权访问审批页",
                              "当前账号不是管理员，无法审批访问申请。", user)
            return
        if not self._access_ok():
            # Access 已配置但验签失败：直连源站或在 Cloudflare 之外，拒绝。
            self._send_denied("请通过 Cloudflare Access 访问",
                              "此页面要求通过 Cloudflare Access 邮箱验证。"
                              "请从管理子域正常访问。", user)
            return
        self._render_admin(user)

    def _send_denied(self, title: str, message: str, user: Any) -> None:
        """403：身份已识别但权限不够。与 401（没登录）必须区分开。"""
        self._send_html(app.web.pages.denied_page(
            title=title, message=message,
            username=getattr(user, "username", "")), HTTPStatus.FORBIDDEN)

    def _render_admin(self, user: Any, *, error: str = "", notice: str = "") -> None:
        self._send_html(app.web.pages.admin_page(
            actor=user.username,
            pending=[u.public() for u in self._auth.list_users(STATUS_PENDING)],
            users=[u.public() for u in self._auth.list_users()],
            invites=self._auth.list_invites(),
            error=error, notice=notice,
            access_email=getattr(self, "_access_email", ""),
            access_enabled=self._access.enabled,
        ))

    def _handle_admin_post(self) -> None:
        """审批页的表单动作：同意 / 拒绝 / 建邀请码 / 停用邀请码 / 改密码。"""
        user = self._current_user()
        if user is None:
            self._reject_unauthenticated("/admin")
            return
        if not getattr(user, "is_admin", False):
            # 已登录但不是管理员：这是权限问题（403），不是没登录（401/跳登录页）。
            self._send_denied("无权访问审批页",
                              "当前账号不是管理员，无法审批访问申请。", user)
            return
        if not self._access_ok():
            self._send_denied("请通过 Cloudflare Access 访问",
                              "此页面要求通过 Cloudflare Access 邮箱验证。", user)
            return

        form = self._read_form()
        action = (form.get("action") or "").strip()
        target = (form.get("username") or "").strip()
        notice = ""
        try:
            if action == "approve":
                self._auth.approve(target, by=user.username)
                notice = f"已批准 {target}"
            elif action == "reject":
                self._auth.reject(target, by=user.username)
                notice = f"已拒绝 {target}"
            elif action == "new_invite":
                try:
                    max_uses = max(1, int(form.get("max_uses") or 1))
                except ValueError:
                    max_uses = 1
                invite = self._auth.create_invite(created_by=user.username,
                                                  max_uses=max_uses,
                                                  note=form.get("note") or "")
                notice = f"邀请码已生成：{invite['code']}"
            elif action == "revoke_invite":
                self._auth.revoke_invite(form.get("code") or "")
                notice = "邀请码已停用"
            elif action == "change_password":
                self._auth.set_password(user.username, form.get("password") or "",
                                        by=user.username)
                # 改密后旧会话全部失效，需要重新登录一次。
                self._send_redirect("/login?notice=" + quote("密码已更新，请重新登录"))
                return
            else:
                error = "未知操作"
                self._render_admin(user, error=error)
                return
        except AuthError as exc:
            self._render_admin(user, error=str(exc))
            return
        self._render_admin(user, notice=notice)

    # -- 鉴权 ----------------------------------------------------------
    def _token_ok(self) -> bool:
        expected = self.server.token  # type: ignore[attr-defined]
        if not expected:
            return True
        supplied = self.headers.get("X-Yikou-Token", "")
        if not supplied:
            query = parse_qs(urlparse(self.path).query)
            supplied = (query.get("token") or [""])[0]
        # 常量时间比较；长度不同直接返回 False。
        return bool(supplied) and secrets.compare_digest(supplied, expected)

    def _method_allowed(self, name: str) -> bool:
        """非管理员只能调用白名单里的桥接方法。"""
        if self._bridge.is_admin or name in _NON_ADMIN_METHODS:
            return True
        self._send_error_json(
            HTTPStatus.FORBIDDEN,
            "该操作仅管理员可用",
            "admin_only",
        )
        return False

    def _require_fs_access(self) -> bool:
        """服务器端文件浏览器：能列出这台手机的全部目录，仅管理员可用。"""
        if not self._require_token():
            return False
        if not self._bridge.is_admin:
            self._send_error_json(
                HTTPStatus.FORBIDDEN,
                "浏览服务器文件仅管理员可用",
                "admin_only",
            )
            return False
        return True

    def _require_token(self) -> bool:
        """``/api/*`` 的凭据闸门。

        原来只认单一的静态令牌 ``self.server.token``；现在改成认「账号会话」，
        同时保留本地直连的旧令牌（见 :meth:`_legacy_token_ok`）。之所以不直接复用
        ``_token_ok``：静态令牌不能成为绕过审批链的后门。
        """
        if self._current_user() is not None:
            return True
        self._send_error_json(
            HTTPStatus.UNAUTHORIZED,
            "需要登录：请在 /login 登录后再操作",
            "login_required",
        )
        return False

    # -- 路由 ----------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 要求的命名
        self._body_read = False
        route = urlparse(self.path).path
        if route == "/healthz":
            # 免鉴权的最小连通性探测：只回答「服务活着」，不泄露任何状态。
            self._send_json({"ok": True})
            return
        if route == "/login":
            self._serve_login()
            return
        if route == "/logout":
            self._serve_logout()
            return
        if route in _ADMIN_PATHS:
            self._serve_admin()
            return
        if route == "/api/fs/list":
            if not self._require_fs_access():
                return
            self._handle_fs_list()
            return
        if route.startswith("/api/"):
            self._send_error_json(HTTPStatus.METHOD_NOT_ALLOWED, "桥接方法请用 POST 调用", "use_post")
            return
        self._serve_static(route)

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def do_POST(self) -> None:  # noqa: N802
        self._body_read = False
        route = urlparse(self.path).path
        if route == "/login":
            self._handle_login_post()
            return
        if route == "/logout":
            self._serve_logout()
            return
        if route in _ADMIN_PATHS:
            self._handle_admin_post()
            return
        if not route.startswith("/api/"):
            self._send_error_json(HTTPStatus.NOT_FOUND, "未知路径", "not_found")
            return
        if not self._require_token():
            return
        name = route[len("/api/"):].strip("/")
        if name == "fs/list":
            if not self._require_fs_access():
                return
            self._handle_fs_list()
            return
        if not self._method_allowed(name):
            return
        self._handle_bridge_call(name)

    # -- 静态文件 ------------------------------------------------------
    def _serve_static(self, route: str) -> None:
        try:
            body, ctype, cache = read_static(route, self._dist)
        except StaticFileError as exc:
            self._send_error_json(exc.status, exc.message, exc.code)
            return
        self._send_bytes(body, 200, ctype, {"Cache-Control": cache})

    # -- 文件浏览器 ----------------------------------------------------
    def _handle_fs_list(self) -> None:
        """列出服务器（也就是跑任务那台手机）上的目录，供前端选择 Excel 路径。"""
        query = parse_qs(urlparse(self.path).query)
        raw = (query.get("path") or [""])[0].strip()
        self._send_json(list_dir(browse_root(raw)))

    # -- 桥接分发 ------------------------------------------------------
    def _handle_bridge_call(self, name: str) -> None:
        bridge = self._bridge
        method = None if (not name or name.startswith("_") or name in _NON_HTTP) else getattr(bridge, name, None)
        if not callable(method):
            self._send_error_json(HTTPStatus.NOT_FOUND, f"未知方法：{name}", "unknown_method")
            return
        args, kwargs = self._read_call_args()
        if args is None or kwargs is None:
            return
        try:
            result = method(*args, **kwargs)
        except TypeError as exc:
            # 参数不匹配是调用方的问题，400 比 500 更准确。
            self._send_error_json(HTTPStatus.BAD_REQUEST, f"参数不匹配：{exc}", "bad_arguments")
            return
        except Exception as exc:  # noqa: BLE001 - 任何业务异常都要变成 JSON 而不是断连
            self._send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR,
                                 f"{type(exc).__name__}: {exc}", "call_failed")
            return
        self._send_json(result)

    def _read_call_args(self) -> tuple[list[Any] | None, dict[str, Any] | None]:
        """请求体为 JSON 数组时按位置传参，为对象时按关键字传参（对齐 Python 语义）。"""
        self._body_read = True
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        raw = self.rfile.read(length) if length > 0 else b""
        if not raw.strip():
            return [], {}
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            self._send_error_json(HTTPStatus.BAD_REQUEST, f"请求体不是合法 JSON：{exc}", "bad_json")
            return None, None
        if isinstance(payload, list):
            return payload, {}
        if isinstance(payload, dict):
            return [], payload
        # 标量请求体：当成单个位置参数，便于 set_split_ratio(0.4) 这类调用。
        return [payload], {}


# ----------------------------------------------------------------------
# 服务器
# ----------------------------------------------------------------------
class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], dist_dir: Path, bridge: Bridge, token: str,
                 *, auth: AuthStore | None = None,
                 access: AccessVerifier | None = None,
                 auth_env: dict[str, str] | None = None) -> None:
        super().__init__(address, _Handler)
        self.dist_dir = dist_dir
        self.bridge = bridge
        self.token = token
        #: 账号体系。为 None 时退化为纯令牌模式（仅测试/兼容旧行为）。
        self.auth = auth if auth is not None else AuthStore(default_auth_dir())
        #: Cloudflare Access 校验器（管理页的额外一层，未配置时为 disabled）。
        self.access = access if access is not None else access_verifier_from_env()
        #: 是否信任网关头。仅当确实经由 Cloudflare 隧道进来时才算。
        self.auth_env = auth_env if auth_env is not None else os.environ


def default_dist_dir() -> Path:
    """前端构建产物目录。

    Android APK 通过 ``YIKOU_DIST_DIR`` 指向从 assets 解出的 ``dist/``；
    桌面与 Termux 未设置时保持仓库内的 ``frontend/dist``。
    """
    override = os.environ.get("YIKOU_DIST_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    return Path(__file__).resolve().parents[2] / "frontend" / "dist"


def build_bridge(on_destroy: Any = None, config_path: Any = None) -> Bridge:
    """构造 Bridge 并挂上网页版窗口适配层。

    ``config_path`` 仅供测试注入临时配置文件，生产环境走默认用户配置目录。
    """
    bridge = Bridge(config_path) if config_path is not None else Bridge()
    bridge.attach(_WebWindow(on_destroy))
    return bridge


def serve(host: str = "0.0.0.0", port: int = DEFAULT_PORT, *, dist_dir: Path | None = None,
          token: str = "", rotate_token: bool = False, announce: bool = True,
          auth_dir: Path | None = None) -> int:
    """启动网页版服务并阻塞到被关闭；返回进程退出码。"""
    access_token = token or load_or_create_token(rotate=rotate_token)
    httpd = create_server(host, port, dist_dir=dist_dir, token=access_token,
                          auth_dir=auth_dir)

    actual_port = httpd.server_address[1]
    if announce:
        _announce(host, actual_port, access_token)
    try:
        httpd.serve_forever(poll_interval=0.4)
    except KeyboardInterrupt:
        if announce:
            print("\n收到中断，正在停止服务…", flush=True)
    finally:
        httpd.shutdown()
        httpd.server_close()
        # 任务线程都是 daemon，进程退出时不会挂住。
    return 0


def create_server(host: str, port: int, *, dist_dir: Path | None = None, token: str = "",
                  on_destroy: Any = None, config_path: Any = None,
                  auth_dir: Path | None = None, auth: AuthStore | None = None,
                  access: AccessVerifier | None = None) -> ThreadingHTTPServer:
    """构造（但不启动）网页版服务器。

    单独拆出来便于测试与嵌用：调用方可以 ``serve_forever()`` 自己控制生命周期，
    单元测试则用 ``port=0`` 拿一个临时端口，并用 ``config_path`` 隔离用户配置。
    """
    dist = Path(dist_dir) if dist_dir else default_dist_dir()
    if not (dist / "index.html").is_file():
        raise WebServerError(
            f"未找到前端构建产物 {dist / 'index.html'}。\n"
            "请先构建前端：cd frontend && pnpm install && pnpm build"
        )
    try:
        return _Server((host, port), dist, build_bridge(on_destroy, config_path), token,
                       auth=auth if auth is not None else AuthStore(auth_dir or default_auth_dir()),
                       access=access)
    except OSError as exc:
        raise WebServerError(
            f"无法监听 {host}:{port}（{exc}）。端口可能已被占用，可用 --port 换一个。"
        ) from exc


def _announce(host: str, port: int, token: str) -> None:
    """打印客户端可直接点开的网址、令牌与省电提示。"""
    lines = [
        "",
        f"一口轻食 网页版 v{__version__} 已启动",
        f"  本机访问：http://127.0.0.1:{port}/?token={token}",
    ]
    if host not in {"127.0.0.1", "localhost"}:
        addresses = lan_addresses()
        if addresses:
            lines.append("  其它设备访问（按可用性排序，同一 WiFi 下用第一条）：")
            for name, ip in addresses:
                note = "（VPN 网卡，仅同 VPN 的设备可直连）" if name.startswith(("tun", "ppp")) else ""
                lines.append(f"    http://{ip}:{port}/?token={token}  [{name}]{note}")
        else:
            lines.append(f"  其它设备访问：http://<手机IP>:{port}/?token={token}（未探测到网卡地址）")
    lines += [
        "",
        "  令牌保存在用户配置目录的 web_token；用 --new-token 可轮换。",
        "  手机端建议先执行：termux-wake-lock   （避免息屏后 Termux 被挂起）",
        "",
        "  按 Ctrl+C 停止服务。",
        "",
    ]
    print("\n".join(lines), flush=True)


def _prompt_password(prompt: str) -> str:
    """读取口令：优先隐藏输入，终端不可用时退化为普通输入。"""
    import getpass

    try:
        return getpass.getpass(prompt)
    except (EOFError, OSError, getpass.GetPassWarning):
        return input(prompt)


def _admin_cli(argv: list[str], auth_dir: Path | None) -> int | None:
    """处理 ``--create-admin`` / ``--list-users`` / ``--reset-admin-password``。

    交互式命令，返回退出码；不是这类命令时返回 None 让调用方继续启动服务。
    """
    wants_create = "--create-admin" in argv
    wants_list = "--list-users" in argv
    wants_reset = "--reset-admin-password" in argv
    if not (wants_create or wants_list or wants_reset):
        return None

    store = AuthStore(auth_dir or default_auth_dir())
    try:
        if wants_list:
            users = store.list_users()
            if not users:
                print("还没有任何账号。用 --create-admin 建管理员。")
                return 0
            print(f"账号总数 {len(users)}（待审批 {store.pending_count()}）：")
            for user in users:
                print(f"  {user.username:<32} {user.status:<10} {user.role}")
            return 0

        username = ""
        if "--username" in argv:
            index = argv.index("--username")
            if index + 1 < len(argv):
                username = argv[index + 1]
        try:
            if not username:
                username = input("管理员账号（邮箱或用户名）：").strip()
            password = _prompt_password("管理员密码（至少 6 位）：")
            confirm = _prompt_password("再输一次确认：")
        except EOFError:
            print("需要交互式终端来输入账号密码。", file=sys.stderr)
            return 2
        if password != confirm:
            print("两次输入不一致。", file=sys.stderr)
            return 2
        user = store.create_admin(username, password, force=wants_reset)
        action = "已重置密码" if wants_reset else "已创建管理员"
        print(f"{action}：{user.username}")
        print(f"数据文件：{store.path}")
        return 0
    except AuthError as exc:
        print(str(exc), file=sys.stderr)
        return 1


def main(argv: list[str] | None = None) -> int:
    """实现 ``python run.py --web`` 及账号管理子命令。

    账号管理：``--create-admin`` / ``--reset-admin-password`` / ``--list-users``，
    均可配 ``--username`` 与 ``--auth-dir``。
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    host = "0.0.0.0"
    port = DEFAULT_PORT
    rotate = "--new-token" in argv
    auth_dir: Path | None = None
    if "--auth-dir" in argv:
        index = argv.index("--auth-dir")
        if index + 1 < len(argv):
            auth_dir = Path(argv[index + 1]).expanduser()
    if "--host" in argv:
        host = argv[argv.index("--host") + 1]
    if "--port" in argv:
        try:
            port = int(argv[argv.index("--port") + 1])
        except (IndexError, ValueError):
            print("--port 需要一个整数端口", file=sys.stderr)
            return 2

    if (code := _admin_cli(argv, auth_dir)) is not None:
        return code

    try:
        return serve(host=host, port=port, rotate_token=rotate, auth_dir=auth_dir)
    except WebServerError as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover - 便于直接调试本模块
    raise SystemExit(main())
