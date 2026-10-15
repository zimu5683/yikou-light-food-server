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
* **访问令牌是唯一凭据**：本应用持有管理后台密码、WPS 云文档授权，并能真实下单，
  因此除 ``/healthz`` 外的所有路径都要求有效令牌；``/api/*`` 未授权返回 401。
* **只服务本机与局域网**：令牌只对直连有效。请求一旦带上网关转发头
  （``_PROXY_HEADERS``），说明它来自公网入口，一律 403——本项目不再提供公网访问。

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
from urllib.parse import parse_qs, urlparse

from app import __version__
from app.api.bridge import Bridge
from app.core.config import user_data_dir
from app.web.fs_browser import EXCEL_SUFFIXES as _EXCEL_SUFFIXES, browse_root, list_dir
from app.web.static_files import StaticFileError, read_static

#: 默认端口。选一个不常用的高位端口，避免和 Termux 里其它服务撞车。
DEFAULT_PORT = 8756

#: 不通过 HTTP 暴露的方法：attach 由服务端启动时内部调用，走 HTTP 没有意义。
_NON_HTTP = frozenset({"attach"})

#: 兼容旧导入：文件浏览器过滤后缀。
EXCEL_SUFFIXES = _EXCEL_SUFFIXES

#: 免令牌路径：只有探活。
_PUBLIC_PATHS = frozenset({"/healthz"})

#: 网关转发过来的请求会带这些头。用于区分「本地直连」与「公网入口」。
_PROXY_HEADERS = ("cf-connecting-ip", "cf-ray", "x-forwarded-for", "x-forwarded-proto")


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

        客户端随时可能中途断开（浏览器取消、手机切网）。这时写 socket 会抛
        BrokenPipeError/ConnectionResetError；如果不吞掉，它会变成未捕获异常刷屏。

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

    # -- 凭据 ----------------------------------------------------------
    @property
    def _is_proxied(self) -> bool:
        """请求是否经网关进来。

        令牌是单一静态口令，一旦允许它在公网入口生效，任何人都能拿它操作这台机器
        （管理后台密码、WPS 授权、真实下单都在后面）。因此它只对本地/局域网直连有效，
        经网关的请求一概拒绝——本项目不再提供公网访问。
        """
        return any(self.headers.get(h) for h in _PROXY_HEADERS)

    def _supplied_token(self) -> str:
        """客户端提供的令牌：先看请求头，再看 ``?token=``。"""
        supplied = (self.headers.get("X-Yikou-Token") or "").strip()
        if supplied:
            return supplied
        query = parse_qs(urlparse(self.path).query)
        return (query.get("token") or [""])[0].strip()

    def _token_ok(self) -> bool:
        """校验令牌：常量时间比较，且公网入口一律不认。"""
        expected = self.server.token  # type: ignore[attr-defined]
        if not expected or self._is_proxied:
            return False
        supplied = self._supplied_token()
        return bool(supplied) and secrets.compare_digest(supplied, expected)

    def _wants_json(self, path: str) -> bool:
        """/api/* 一律给 JSON；页面请求给可读的 HTML。"""
        return path.startswith("/api/") or "application/json" in (self.headers.get("Accept") or "")

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
        if self._is_proxied and path not in _PUBLIC_PATHS:
            self._reject_remote(path)
            return False
        if path in _PUBLIC_PATHS:
            return True
        if not self._token_ok():
            self._reject_unauthenticated(path)
            return False
        return True

    def _reject_remote(self, path: str) -> None:
        """403：请求来自网关。服务只对本机/局域网直连开放。"""
        message = "本服务只在本机/局域网直连可用，不接受来自网关（反向代理）的请求。"
        if self._wants_json(path):
            self._send_error_json(HTTPStatus.FORBIDDEN, message, "remote_denied")
            return
        self._send_html(f"<!doctype html><meta charset=utf-8><p>{message}</p>",
                        HTTPStatus.FORBIDDEN)

    def _reject_unauthenticated(self, path: str) -> None:
        if self._wants_json(path):
            self._send_error_json(
                HTTPStatus.UNAUTHORIZED,
                "缺少或错误的访问令牌；请从启动日志里的带 ?token= 网址打开",
                "token_required",
            )
            return
        self._send_html(
            "<!doctype html><meta charset=utf-8><p>缺少或错误的访问令牌；"
            "请从启动日志里的带 ?token= 网址打开。</p>",
            HTTPStatus.UNAUTHORIZED)

    # -- 页面 ----------------------------------------------------------
    def _send_html(self, body: str, status: int = 200) -> None:
        self._send_bytes(body.encode("utf-8"), status, "text/html; charset=utf-8",
                         {"Cache-Control": "no-store",
                          "Content-Security-Policy":
                              "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
                              "base-uri 'none'; frame-ancestors 'none'",
                          "Referrer-Policy": "no-referrer"})

    # -- 路由 ----------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 要求的命名
        self._body_read = False
        route = urlparse(self.path).path
        if route == "/healthz":
            # 免令牌的最小连通性探测：只回答「服务活着」，不泄露任何状态。
            self._send_json({"ok": True})
            return
        if route == "/api/fs/list":
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
        if not route.startswith("/api/"):
            self._send_error_json(HTTPStatus.NOT_FOUND, "未知路径", "not_found")
            return
        name = route[len("/api/"):].strip("/")
        if name == "fs/list":
            self._handle_fs_list()
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

    def __init__(self, address: tuple[str, int], dist_dir: Path, bridge: Bridge,
                 token: str) -> None:
        super().__init__(address, _Handler)
        self.dist_dir = dist_dir
        self.bridge = bridge
        self.token = token


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
          token: str = "", rotate_token: bool = False, announce: bool = True) -> int:
    """启动网页版服务并阻塞到被关闭；返回进程退出码。"""
    access_token = token or load_or_create_token(rotate=rotate_token)
    httpd = create_server(host, port, dist_dir=dist_dir, token=access_token)

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
                  on_destroy: Any = None, config_path: Any = None) -> ThreadingHTTPServer:
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
        return _Server((host, port), dist, build_bridge(on_destroy, config_path), token)
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


def main(argv: list[str] | None = None) -> int:
    """实现 ``python run.py --web``。"""
    argv = list(sys.argv[1:] if argv is None else argv)
    host = "0.0.0.0"
    port = DEFAULT_PORT
    rotate = "--new-token" in argv
    if "--host" in argv:
        host = argv[argv.index("--host") + 1]
    if "--port" in argv:
        try:
            port = int(argv[argv.index("--port") + 1])
        except (IndexError, ValueError):
            print("--port 需要一个整数端口", file=sys.stderr)
            return 2

    try:
        return serve(host=host, port=port, rotate_token=rotate)
    except WebServerError as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover - 便于直接调试本模块
    raise SystemExit(main())
