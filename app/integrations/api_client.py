"""纯接口模式的 HTTP API 客户端（不启动浏览器）。

两个平台的登录、列表、下单等接口都通过普通 HTTP 请求完成，避免弹出浏览器。
管理后台有 WAF，需要带浏览器特征的请求头；闪时送平台登录需要图形验证码，
验证码图片由调用方展示给用户后把用户输入的 code 传回。
"""
from __future__ import annotations

import json
import random
from typing import Any
from urllib.parse import urlsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3.util import Retry


class ApiError(RuntimeError):
    """纯接口模式下请求失败或响应异常时抛出。"""


AUTH_ERROR_CODES = {401, 10000}
AUTH_MESSAGE_KEYWORDS = (
    "token失效", "token 失效", "token invalid", "invalid token",
    "登录态失效", "登录已失效", "登录过期", "登录已过期",
    "请重新登陆", "请重新登录", "unauthorized", "login expired",
)


def is_auth_expired_payload(payload: Any) -> bool:
    """Return True when a Sss JSON payload means "login expired".

    The platform currently uses HTTP 200 + ``code=10000`` with
    ``message="token失效，请重新登陆"``, so HTTP status alone is not enough.
    """
    if not isinstance(payload, dict):
        return False
    try:
        code = int(payload.get("code"))
    except (TypeError, ValueError):
        code = None
    if code in AUTH_ERROR_CODES:
        return True
    message = str(payload.get("message") or payload.get("msg") or "")
    lowered = message.lower()
    return any(keyword in message or keyword in lowered
               for keyword in AUTH_MESSAGE_KEYWORDS)


def auth_error_message(payload: Any = None, fallback: str = "") -> str:
    """把「登录态失效」的响应体整理成一句给用户看的中文提示（含 code 与原文）。"""
    if isinstance(payload, dict):
        message = str(payload.get("message") or payload.get("msg") or "").strip()
        code = payload.get("code")
        if message:
            return f"闪时送登录态已失效（code={code}，{message}），请重新登录"
        if code is not None:
            return f"闪时送登录态已失效（code={code}），请重新登录"
    return fallback or "闪时送登录态已失效，请重新登录"


class SssTransportError(ApiError):
    """闪时送请求未取得可确认响应。

    对下单 POST 而言，这不代表服务端一定没有落单；调用方必须先查询
    订单列表对账，不能把它当作可直接重试的普通失败。
    """


def origin_from_url(url: str) -> str:
    """从完整 URL 中提取协议+域名，例如 https://m.icall.me/admin/#/login → https://m.icall.me。"""
    parts = urlsplit(url or "")
    if not parts.scheme or not parts.netloc:
        raise ValueError(f"无法从网址提取源：{url}")
    return f"{parts.scheme}://{parts.netloc}"


def _browser_headers(origin: str, *, admin: bool = True) -> dict[str, str]:
    """构造能通过管理后台 WAF 的浏览器特征请求头。

    2026-09 实测：阿里云 ESA 的 ``http_bot_simple`` 会在缺少浏览器指纹时直接
    返回 403。这里补齐 UA / Sec-Fetch / sec-ch-ua 等头，并在登录前先访问
    ``/admin/`` 拿 ``acw_tc`` cookie，尽量贴近真实浏览器首访行为。
    """
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Linux; Android 15; Pixel 9 Build/AP3A.240905.015) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0.0.0 "
            "Mobile Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate, br, zstd",
        "Origin": origin,
        "X-Requested-With": "XMLHttpRequest",
        "Connection": "keep-alive",
        "sec-ch-ua": '"Chromium";v="152", "Not?A_Brand";v="24", "Google Chrome";v="152"',
        "sec-ch-ua-mobile": "?1",
        "sec-ch-ua-platform": '"Android"',
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
        "Priority": "u=1, i",
    }
    headers["Referer"] = (origin + "/admin/") if admin else (origin + "/takeout")
    return headers


def _find_key(obj: Any, key: str) -> Any:
    """在嵌套 JSON 中递归查找第一个非空键值。"""
    if isinstance(obj, dict):
        if key in obj and obj[key] not in (None, ""):
            return obj[key]
        for value in obj.values():
            found = _find_key(value, key)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = _find_key(item, key)
            if found is not None:
                return found
    return None


def _normalize_timeout(value: float | tuple[float, float]) -> tuple[float, float]:
    """把调用方的超时统一成 ``(connect, read)`` 秒。

    历史调用方曾把毫秒（8000）直接传入 ``requests``（单位秒），一次卡住
    就要等 8000 秒。这里做防御性归一化：大于 120 的单值视为毫秒。
    """
    if isinstance(value, tuple):
        connect, read = value
        return (float(connect), float(read))
    seconds = float(value)
    if seconds > 120:
        seconds /= 1000.0
    seconds = max(1.0, seconds)
    return (5.0, seconds)


class AdminApiClient:
    """管理后台纯接口客户端。"""

    def __init__(self, url: str, username: str, password: str, timeout: float = 15) -> None:
        self.origin = origin_from_url(url)
        self.username = username
        self.password = password
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(_browser_headers(self.origin, admin=True))
        self.token = ""
        self.uniacid = ""
        #: Android 上登录后所有管理接口都改走 Chromium WebView，绕开 WAF 对
        #: Python/OpenSSL TLS 指纹的识别；桌面/Termux 保持 requests。
        self._native_web_http = False

    def _warm_up_waf(self) -> None:
        """先访问一次管理后台页面，让 WAF 下发 ``acw_tc`` 等会话 cookie。"""
        try:
            self.session.get(self.origin + "/admin/", timeout=self.timeout,
                             allow_redirects=True)
        except requests.RequestException:
            # 预热失败不直接报错，后续 POST 可能仍然能成功；错误在 POST 阶段统一处理。
            pass

    def _post_login(self):
        return self.session.post(
            self.origin + "/channel/login",
            json={"username": self.username, "password": self.password, "remember": False},
            timeout=self.timeout,
        )

    def _apply_login_payload(self, payload: dict[str, Any]) -> None:
        """校验并保存后端登录响应中的 token/uniacid。"""
        if payload.get("code") not in (200, None):
            raise ApiError(payload.get("msg") or f"管理后台登录失败（{payload.get('code')}）")

        data = payload.get("data") or {}
        user_info = data.get("user_info") or {}
        self.token = str(data.get("token") or "")
        self.uniacid = str(data.get("uniacid") or user_info.get("uniacid") or "")
        if not self.token:
            raise ApiError("管理后台登录响应中没有 token")
        if not self.uniacid:
            raise ApiError("管理后台登录响应中没有 uniacid")

    def _android_web_get(self, path: str) -> dict[str, Any] | None:
        """用 Android WebView 的 Chromium 环境执行管理接口 GET。

        返回解析后的 payload；非 Android / WebView 不可用时返回 None，让调用方
        退回 requests。403/非 JSON 等明确的接口错误会转成 ApiError。
        """
        from app.integrations import android_web_login

        if not android_web_login.is_android():
            return None
        try:
            status, body = android_web_login.webview_fetch(
                self.origin, path, method="GET",
                token=self.token, uniacid=self.uniacid,
                timeout_ms=int(max(5.0, float(self.timeout)) * 1000),
            )
        except android_web_login.AndroidWebLoginError:
            return None

        try:
            payload = json.loads(body)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ApiError(
                f"接口 {path} 在 WebView 中返回非 JSON（HTTP {status}）") from exc
        if not isinstance(payload, dict):
            raise ApiError(f"接口 {path} 在 WebView 中返回异常结构")
        if status == 401 or payload.get("code") == 401:
            raise ApiError("登录会话已失效（接口 401），请重新运行程序")
        code = payload.get("code")
        if code not in (200, None):
            raise ApiError(f"接口 {path} 返回异常：{payload.get('msg') or code}")
        return payload

    def _android_webview_login(self) -> dict[str, Any] | None:
        """APK 内优先用系统 WebView 登录，绕过 WAF 对 Python TLS 指纹的识别。

        非 Android 环境或 WebView 兜底不可用时返回 ``None``，调用方继续走
        requests 的换会话重试路径。
        """
        from app.integrations import android_web_login

        if not android_web_login.is_android():
            return None
        try:
            return android_web_login.webview_login(
                self.origin, self.username, self.password,
                timeout_ms=int(max(5.0, float(self.timeout)) * 1000),
            )
        except android_web_login.AndroidWebLoginError:
            return None

    def login(self) -> None:
        """账号密码登录，保存 token 与 uniacid。

        优先普通 requests（先预热拿 WAF cookie）。如果仍被 ``http_bot_simple``
        403，APK 内改用系统 WebView 的 Chromium 环境执行同源 fetch；桌面/Termux
        则换新 Session 重试一次。
        """
        from app.integrations import android_web_login

        # Android 上优先用系统 WebView 登录：既能过 WAF，也能让 CookieManager
        # 保存后续管理接口需要的 cookie。
        if android_web_login.is_android():
            webview_payload = self._android_webview_login()
            if webview_payload is not None:
                self._apply_login_payload(webview_payload)
                self._native_web_http = True
                return

        self._warm_up_waf()
        try:
            resp = self._post_login()
        except requests.RequestException as exc:
            raise ApiError(f"管理后台登录请求失败：{exc}") from exc

        if resp.status_code == 403:
            # APK：WebView 是真实 Chromium，TLS/JS 指纹能过 WAF。
            webview_payload = self._android_webview_login()
            if webview_payload is not None:
                self._apply_login_payload(webview_payload)
                self._native_web_http = True
                return

            # 桌面/Termux：换全新 Session + 重新预热再试一次。
            self.session.close()
            self.session = requests.Session()
            self.session.headers.update(_browser_headers(self.origin, admin=True))
            self._warm_up_waf()
            try:
                resp = self._post_login()
            except requests.RequestException as exc:
                raise ApiError(f"管理后台登录请求失败：{exc}") from exc

        if resp.status_code == 403:
            raise ApiError(
                "管理后台 WAF 拒绝了登录（HTTP 403，Denied by http_bot_simple）。"
                "已尝试浏览器特征重试；请关闭 VPN/代理后重试，或稍后再试。"
            )

        try:
            payload = resp.json()
        except ValueError as exc:
            raise ApiError(f"管理后台登录返回非 JSON（HTTP {resp.status_code}）") from exc
        self._apply_login_payload(payload)
        if android_web_login.is_android():
            self._native_web_http = True

    def get_json(self, path: str) -> dict[str, Any]:
        """带鉴权 GET 并解析 JSON，401 按登录失效处理。

        Android 登录后默认走 WebView/Chromium；若 WebView 不可用则退回 requests，
        并在 requests 被 WAF 403 时再尝试一次 WebView。
        """
        if not self.token:
            raise ApiError("尚未登录管理后台")

        if self._native_web_http:
            try:
                payload = self._android_web_get(path)
                if payload is not None:
                    return payload
            except ApiError:
                # WebView 出错时不要直接失败；先退回 requests 再决定，避免单点故障。
                pass

        headers = {"Authorization": f"Bearer {self.token}", "uniacid": self.uniacid}
        try:
            resp = self.session.get(self.origin + path, headers=headers, timeout=self.timeout)
        except requests.RequestException as exc:
            raise ApiError(f"接口 {path} 请求失败：{exc}") from exc

        if resp.status_code == 403:
            try:
                payload = self._android_web_get(path)
            except ApiError:
                payload = None
            if payload is not None:
                self._native_web_http = True
                return payload

        try:
            payload = resp.json()
        except ValueError as exc:
            raise ApiError(f"接口 {path} 返回非 JSON（HTTP {resp.status_code}）") from exc

        if resp.status_code == 401 or payload.get("code") == 401:
            raise ApiError("登录会话已失效（接口 401），请重新运行程序")
        code = payload.get("code")
        if code not in (200, None):
            raise ApiError(f"接口 {path} 返回异常：{payload.get('msg') or code}")
        return payload


class SssApiClient:
    """闪时送纯接口客户端。

    ``timeout`` 统一用秒（调用方传毫秒时必须先除以 1000）。内部拆分为
    ``(connect, read)`` 二元组，避免一次慢响应卡死整个批量任务。
    """

    RETRYABLE_STATUS = (429, 500, 502, 503, 504)

    def __init__(self, url: str, account: str, password: str,
                 timeout: float | tuple[float, float] = 15,
                 pool_size: int = 20) -> None:
        self.origin = origin_from_url(url)
        self.account = account
        self.password = password
        self.timeout = _normalize_timeout(timeout)
        self.pool_size = max(1, int(pool_size))
        self.session = self._new_session(self.pool_size)
        self.token = ""

    def _new_session(self, pool_size: int) -> requests.Session:
        """创建会话；只让安全的 GET 请求自动重试。

        ``create-order-from-client`` 是非幂等 POST。即使连接在服务端落库后
        断开，重试也可能制造重复订单，因此 POST 必须交给上层先对账再决定。
        """
        session = requests.Session()
        session.headers.update(_browser_headers(self.origin, admin=False))
        retry = Retry(
            total=2,
            backoff_factor=0.5,
            status_forcelist=list(self.RETRYABLE_STATUS),
            allowed_methods=("GET",),
        )
        adapter = HTTPAdapter(pool_connections=pool_size,
                              pool_maxsize=pool_size,
                              max_retries=retry)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        return session

    def fork(self) -> "SssApiClient":
        """为并发 worker 创建独立 Session 的克隆（共享 token）。

        ``requests.Session`` 非线程安全，多线程下单时每个 worker 必须用
        自己的 Session；token 字符串共享即可，401 后由主线程统一重登。
        """
        clone = SssApiClient.__new__(SssApiClient)
        clone.origin = self.origin
        clone.account = self.account
        clone.password = self.password
        clone.timeout = self.timeout
        clone.pool_size = 1
        clone.session = clone._new_session(clone.pool_size)
        clone.token = self.token
        return clone

    def close(self) -> None:
        """关闭空闲连接池，供停止请求后的收尾使用。

        requests 无法保证撤回已经抵达服务端的 POST；上层仍会等待在途请求
        结束并对账，而不是把 ``close`` 误当作撤单操作。
        """
        self.session.close()

    def fetch_captcha(self) -> bytes:
        """获取登录图形验证码 PNG 图片。"""
        try:
            resp = self.session.get(
                self.origin + "/consumer/customer/verify-code",
                params={"data": str(random.random()), "phone": self.account},
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise ApiError(f"获取闪时送验证码失败：{exc}") from exc
        if not resp.content.startswith(b"\x89PNG"):
            raise ApiError("闪时送验证码接口未返回 PNG 图片")
        return resp.content

    def login(self, code: str) -> None:
        """使用账号密码和用户输入的图形验证码登录。"""
        code = str(code or "").strip()
        if len(code) < 4:
            raise ApiError("请输入完整图形验证码")
        try:
            resp = self.session.post(
                self.origin + "/consumer/customer/password/login",
                json={"mobile": self.account, "password": self.password, "code": code},
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise ApiError(f"闪时送登录请求失败：{exc}") from exc

        try:
            payload = resp.json()
        except ValueError as exc:
            raise ApiError(f"闪时送登录返回非 JSON（HTTP {resp.status_code}）") from exc

        if not payload.get("success"):
            raise ApiError(payload.get("message") or payload.get("msg") or "闪时送登录失败（验证码错误？）")

        token = _find_key(payload, "token")
        if not token:
            raise ApiError("闪时送登录响应中没有 token")
        self.token = str(token)

    def _request(self, method: str, path: str, body: Any = None) -> dict[str, Any]:
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["token"] = self.token
        try:
            resp = self.session.request(
                method,
                self.origin + path,
                headers=headers,
                json=body if body is not None else None,
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise SssTransportError(f"接口 {path} 请求失败：{exc}") from exc

        # 401 有明确的恢复路径，即便反向代理返回 HTML 也必须先让上层统一
        # 重登；否则会被误分类成“状态不确定”，既不能恢复又容易误导日志。
        if resp.status_code == 401:
            raise ApiError("闪时送登录态已失效（接口 401），请重新登录")

        # 对非幂等 POST 来说，限流/服务端错误同样无法证明服务端没有落库。
        # 不解析成普通业务失败，交给上层按订单列表对账后再决定是否补发。
        if method.upper() == "POST" and resp.status_code in self.RETRYABLE_STATUS:
            raise SssTransportError(f"接口 {path} 返回 HTTP {resp.status_code}")

        try:
            payload = resp.json()
        except ValueError as exc:
            raise SssTransportError(
                f"接口 {path} 返回非 JSON（HTTP {resp.status_code}）") from exc

        if is_auth_expired_payload(payload):
            raise ApiError(auth_error_message(payload))
        return payload

    def get_json(self, path: str) -> dict[str, Any]:
        """对闪时送发起 GET 并返回解析后的 JSON（走统一的请求头/超时/错误处理）。"""
        return self._request("GET", path)

    def post_json(self, path: str, body: Any = None) -> dict[str, Any]:
        """对闪时送发起 POST 并返回解析后的 JSON；body 为空时按无载荷请求发送。"""
        return self._request("POST", path, body)
