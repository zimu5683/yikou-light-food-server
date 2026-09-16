"""账号体系端到端测试：注册申请 → 管理员审批 → 登录 → 会话访问。

这些测试覆盖本次改造的核心诉求，全部走真实 HTTP，不 mock 内部函数：
1. 未登录访问整站（含首页）被拦到登录页；
2. 自助注册落在待审批队列，**未批准前无法登录**；
3. 管理员在 /admin 点「同意」后即可登录；
4. 带有效邀请码注册可直接通过；
5. 会话 Cookie 是 HttpOnly，公网（https 转发）时带 Secure；
6. 旧静态令牌在公网入口失效（不可绕过审批），本地直连仍可用于自救；
7. 非管理员进不了审批页；拒绝后的账号旧会话立即失效；
8. 口令散列不可逆、不出现在任何界面输出里。
"""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.parse
import urllib.request

import pytest

from app.web_auth import (STATUS_APPROVED, STATUS_PENDING, AuthStore,
                          hash_password, verify_password)
from app.web_server import SESSION_COOKIE, create_server

ADMIN = "2485890442@qq.com"
ADMIN_PW = "Ldm681202"


# ----------------------------------------------------------------------
# 夹具
# ----------------------------------------------------------------------
@pytest.fixture()
def auth(tmp_path):
    """带一个已播种管理员的账号存储（生产由 CLI 播种，测试直接给）。"""
    store = AuthStore(tmp_path / "cfg")
    store.create_admin(ADMIN, ADMIN_PW)
    return store


@pytest.fixture()
def server(tmp_path, auth):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html><title>app</title>", encoding="utf-8")
    httpd = create_server("127.0.0.1", 0, dist_dir=dist, token="legacy-secret",
                          config_path=tmp_path / "config.json", auth=auth)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield httpd
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


class Reply:
    """一次请求的结果：状态码、头、正文。"""

    def __init__(self, status: int, headers, body: bytes) -> None:
        self.status = status
        self.headers = headers
        self.body = body

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", "replace")

    def json(self):
        return json.loads(self.text or "null")

    @property
    def location(self) -> str:
        return self.headers.get("Location", "")

    def redirect_query(self) -> dict[str, str]:
        return {k: v[0] for k, v in urllib.parse.parse_qs(
            urllib.parse.urlparse(self.location).query).items()}


def _call(server, method, path, *, body=None, form=None, headers=None,
          cookie=None, follow=False):
    """发一个真实 HTTP 请求；``follow=False`` 时不跟随重定向（便于断言 302）。"""
    port = server.server_address[1]
    data = None
    head = dict(headers or {})
    if form is not None:
        data = urllib.parse.urlencode(form).encode("utf-8")
        head["Content-Type"] = "application/x-www-form-urlencoded"
    elif body is not None:
        data = json.dumps(body).encode("utf-8")
        head["Content-Type"] = "application/json"
    if cookie:
        head["Cookie"] = f"{SESSION_COOKIE}={cookie}"
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}",
                                     data=data, method=method, headers=head)

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, hdrs, newurl):
            if follow:
                return super().redirect_request(req, fp, code, msg, hdrs, newurl)
            return None

    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(request, timeout=15) as response:
            return Reply(response.status, response.headers, response.read())
    except urllib.error.HTTPError as exc:
        return Reply(exc.code, exc.headers, exc.read())


def _register(server, username, password, invite=""):
    return _call(server, "POST", "/login",
                 form={"action": "register", "username": username,
                       "password": password, "invite_code": invite})


def _login(server, username, password):
    """登录并返回 (Reply, 会话令牌)。"""
    reply = _call(server, "POST", "/login",
                  form={"action": "login", "username": username, "password": password})
    if reply.status != 302:
        return reply, ""
    raw = reply.headers.get("Set-Cookie", "")
    token = ""
    for part in raw.split(";"):
        name, sep, value = part.strip().partition("=")
        if sep and name == SESSION_COOKIE:
            token = value
    return reply, token


def _login_admin(server):
    reply, token = _login(server, ADMIN, ADMIN_PW)
    assert token, f"管理员登录失败：{reply.status} {reply.text[:200]}"
    return token


# ----------------------------------------------------------------------
# 未登录拦截
# ----------------------------------------------------------------------
@pytest.mark.parametrize("path", ["/", "/index.html", "/assets/app.js", "/admin"])
def test_every_page_requires_login(server, path):
    """整站（尤其首页）都不再匿名开放。"""
    reply = _call(server, "GET", path)
    assert reply.status == 302
    assert reply.location.startswith("/login")


def test_api_requires_login(server):
    reply = _call(server, "POST", "/api/bridge_ready", body=[])
    assert reply.status == 401
    assert reply.json()["code"] == "login_required"


def test_healthz_stays_public(server):
    reply = _call(server, "GET", "/healthz")
    assert reply.status == 200 and reply.json() == {"ok": True}


def test_login_page_is_reachable_and_hides_app(server):
    reply = _call(server, "GET", "/login")
    assert reply.status == 200
    assert "登录" in reply.text
    # 登录页不得泄露应用界面内容
    assert "<title>app</title>" not in reply.text


def test_login_redirect_remembers_target(server):
    reply = _call(server, "GET", "/some/deep/page")
    assert reply.status == 302
    assert "next=%2Fsome%2Fdeep%2Fpage" in reply.location


# ----------------------------------------------------------------------
# 注册 → 审批 → 登录
# ----------------------------------------------------------------------
def test_pending_user_cannot_login_until_approved(server, auth):
    """核心诉求：申请未获批准前，密码正确也进不去。"""
    _register(server, "visitor", "visitorpw")
    assert auth.get("visitor").status == STATUS_PENDING

    reply, token = _login(server, "visitor", "visitorpw")
    assert not token, "待审批账号不应拿到会话"
    assert "等待管理员审批" in reply.redirect_query().get("error", "")


def test_admin_approves_then_user_can_log_in(server, auth):
    _register(server, "visitor", "visitorpw")
    admin_token = _login_admin(server)

    reply = _call(server, "POST", "/admin",
                  form={"action": "approve", "username": "visitor"}, cookie=admin_token)
    assert reply.status == 200
    assert auth.get("visitor").status == STATUS_APPROVED
    assert "已批准 visitor" in reply.text

    _, token = _login(server, "visitor", "visitorpw")
    assert token, "批准后应能登录"
    # 批准后的会话能读写应用接口
    api = _call(server, "POST", "/api/bridge_ready", body=[], cookie=token)
    assert api.status == 200 and api.json()["version"]


def test_rejected_user_is_told_and_kept_out(server, auth):
    _register(server, "spammer", "spammerpw")
    admin_token = _login_admin(server)
    _call(server, "POST", "/admin",
          form={"action": "reject", "username": "spammer"}, cookie=admin_token)

    reply, token = _login(server, "spammer", "spammerpw")
    assert not token
    assert "未获批准" in reply.redirect_query().get("error", "")


def test_invite_code_skips_manual_approval(server, auth):
    admin_token = _login_admin(server)
    created = _call(server, "POST", "/admin",
                    form={"action": "new_invite", "max_uses": "1", "note": "给同事"},
                    cookie=admin_token)
    assert created.status == 200
    code = auth.list_invites()[0]["code"]

    _register(server, "friend", "friendpw", invite=code)
    assert auth.get("friend").status == STATUS_APPROVED, "邀请码注册应直接通过"
    _, token = _login(server, "friend", "friendpw")
    assert token


def test_invite_code_is_single_use_by_default(server, auth):
    admin_token = _login_admin(server)
    _call(server, "POST", "/admin", form={"action": "new_invite", "max_uses": "1"},
          cookie=admin_token)
    code = auth.list_invites()[0]["code"]
    _register(server, "firstuser", "firstpw", invite=code)
    reply = _register(server, "second", "secondpw", invite=code)
    assert "使用次数已用完" in reply.redirect_query().get("error", "")
    assert auth.get("second") is None


def test_revoked_invite_is_rejected(server, auth):
    admin_token = _login_admin(server)
    _call(server, "POST", "/admin", form={"action": "new_invite", "max_uses": "5"},
          cookie=admin_token)
    code = auth.list_invites()[0]["code"]
    _call(server, "POST", "/admin", form={"action": "revoke_invite", "code": code},
          cookie=admin_token)
    reply = _register(server, "someone", "someonepw", invite=code)
    assert "无效或已被停用" in reply.redirect_query().get("error", "")


def test_bad_invite_code_does_not_silently_pend(server, auth):
    """填了错的邀请码要明确报错，不能让用户以为申请已提交。"""
    reply = _register(server, "someone", "someonepw", invite="not-a-real-code")
    assert "无效或已被停用" in reply.redirect_query().get("error", "")
    assert auth.get("someone") is None


def test_duplicate_username_is_rejected(server, auth):
    _register(server, "duplicate", "duppw1")
    reply = _register(server, "duplicate", "duppw2")
    assert "已存在" in reply.redirect_query().get("error", "")


@pytest.mark.parametrize("username,password,expected", [
    ("ab", "longenough", "3-64"),
    ("!!!bad!!!", "longenough", "3-64"),
    ("goodname", "123", "至少 6 位"),
])
def test_registration_validates_input(server, username, password, expected):
    reply = _register(server, username, password)
    assert expected in reply.redirect_query().get("error", "")


def test_wrong_password_is_rejected(server):
    _register(server, "u1long", "rightpw")
    reply, token = _login(server, "u1long", "wrongpw")
    assert not token
    assert "不正确" in reply.redirect_query().get("error", "")


def test_unknown_account_does_not_leak_existence(server):
    """账号不存在与密码错误必须同一句提示，避免枚举账号。"""
    reply, _ = _login(server, "ghost", "whatever")
    assert "账号或密码不正确" in reply.redirect_query().get("error", "")


# ----------------------------------------------------------------------
# 会话与 Cookie
# ----------------------------------------------------------------------
def test_session_cookie_is_httponly_and_samesite(server):
    _register(server, "u2long", "u2pw123")
    admin_token = _login_admin(server)
    _call(server, "POST", "/admin", form={"action": "approve", "username": "u2long"},
          cookie=admin_token)
    reply, token = _login(server, "u2long", "u2pw123")
    raw = reply.headers.get("Set-Cookie", "")
    assert "HttpOnly" in raw
    assert "SameSite=Lax" in raw
    assert "Path=/" in raw


def test_cookie_is_secure_when_served_over_https(server):
    """经 Cloudflare 隧道（X-Forwarded-Proto: https）时必须带 Secure。"""
    reply = _call(server, "POST", "/login",
                  form={"action": "login", "username": ADMIN, "password": ADMIN_PW},
                  headers={"X-Forwarded-Proto": "https", "CF-Connecting-IP": "1.2.3.4"})
    assert reply.status == 302
    assert "Secure" in reply.headers.get("Set-Cookie", "")


def test_session_token_works_as_api_header(server):
    """前端沿用的 X-Yikou-Token 头必须接受会话令牌（不改前端即兼容）。"""
    token = _login_admin(server)
    reply = _call(server, "POST", "/api/bridge_ready", body=[],
                  headers={"X-Yikou-Token": token})
    assert reply.status == 200


def test_logout_invalidates_session(server):
    token = _login_admin(server)
    assert _call(server, "POST", "/api/bridge_ready", body=[], cookie=token).status == 200
    out = _call(server, "POST", "/logout", cookie=token)
    assert out.status == 302
    assert _call(server, "POST", "/api/bridge_ready", body=[], cookie=token).status == 401


def test_changing_password_kills_existing_sessions(server, auth):
    token = _login_admin(server)
    reply = _call(server, "POST", "/admin",
                  form={"action": "change_password", "password": "brandnewpw"},
                  cookie=token)
    assert reply.status == 302
    # 旧会话失效 → 必须重新登录
    assert _call(server, "POST", "/api/bridge_ready", body=[], cookie=token).status == 401
    _, fresh = _login(server, ADMIN, "brandnewpw")
    assert fresh
    # 复原，避免影响其它测试对密码的假设（每个测试各自 tmp_path，此处仅自证）
    auth.set_password(ADMIN, ADMIN_PW)


def test_rejecting_user_invalidates_their_live_session(server, auth):
    """先批准、拿到会话，再拒绝：旧会话必须立刻失效。"""
    _register(server, "u3long", "u3pw123")
    admin_token = _login_admin(server)
    _call(server, "POST", "/admin", form={"action": "approve", "username": "u3long"},
          cookie=admin_token)
    _, user_token = _login(server, "u3long", "u3pw123")
    assert _call(server, "POST", "/api/bridge_ready", body=[], cookie=user_token).status == 200

    _call(server, "POST", "/admin", form={"action": "reject", "username": "u3long"},
          cookie=admin_token)
    assert _call(server, "POST", "/api/bridge_ready", body=[],
                 cookie=user_token).status == 401


def test_open_redirect_next_is_neutralised(server):
    """``next=//evil.com`` 不能把人带去站外。"""
    reply = _call(server, "POST", "/login",
                  form={"action": "login", "username": ADMIN, "password": ADMIN_PW,
                        "next": "//evil.example.com"})
    assert reply.status == 302
    assert reply.location == "/"


# ----------------------------------------------------------------------
# 旧静态令牌：公网失效、本地可自救
# ----------------------------------------------------------------------
def test_legacy_token_is_refused_on_public_entry(server):
    """经隧道进来的请求带 CF 头；旧令牌不得成为绕过审批的后门。"""
    reply = _call(server, "POST", "/api/bridge_ready?token=legacy-secret", body=[],
                  headers={"CF-Connecting-IP": "9.9.9.9",
                           "X-Forwarded-Proto": "https"})
    assert reply.status == 401, "旧令牌在公网入口必须失效"


def test_legacy_token_still_works_locally(server):
    reply = _call(server, "POST", "/api/bridge_ready?token=legacy-secret", body=[])
    assert reply.status == 200


def test_legacy_token_can_open_admin_page_locally(server):
    """忘记管理员密码时的自救路径：本机用 ?token= 进审批页。"""
    reply = _call(server, "GET", "/admin?token=legacy-secret")
    assert reply.status == 200
    assert "访问审批" in reply.text


# ----------------------------------------------------------------------
# 权限与信息泄露
# ----------------------------------------------------------------------
def test_non_admin_cannot_reach_approval_page(server, auth):
    _register(server, "plain", "plainpw")
    admin_token = _login_admin(server)
    _call(server, "POST", "/admin", form={"action": "approve", "username": "plain"},
          cookie=admin_token)
    _, token = _login(server, "plain", "plainpw")

    reply = _call(server, "GET", "/admin", cookie=token)
    assert reply.status == 403
    assert "无权访问" in reply.text
    # 也不能通过 POST 直接审批
    posted = _call(server, "POST", "/admin",
                   form={"action": "approve", "username": "plain"}, cookie=token)
    assert posted.status == 403


def test_password_hashes_never_appear_in_pages(server, auth):
    """审批页展示用户列表，绝不能把口令散列带出去。"""
    _register(server, "visitor2", "visitor2pw")
    admin_token = _login_admin(server)
    body = _call(server, "GET", "/admin", cookie=admin_token).text
    stored = auth.get("visitor2").password
    assert stored not in body
    assert "pbkdf2_" not in body


def test_public_view_has_no_password_field(server, auth):
    _register(server, "visitor3", "visitor3pw")
    assert "password" not in auth.get("visitor3").public()


# ----------------------------------------------------------------------
# 口令散列本身
# ----------------------------------------------------------------------
def test_password_hashing_round_trip():
    encoded = hash_password("s3cret-pw", iterations=1000)
    assert encoded.startswith("pbkdf2_sha256$")
    assert "s3cret-pw" not in encoded
    assert verify_password("s3cret-pw", encoded)
    assert not verify_password("s3cret-pW", encoded)


def test_password_hash_is_salted():
    assert hash_password("same", iterations=1000) != hash_password("same", iterations=1000)


@pytest.mark.parametrize("broken", ["", "not-a-hash", "pbkdf2_sha256$abc$def", "md5$x$y$z"])
def test_verify_password_never_raises(broken):
    assert verify_password("whatever", broken) is False


# ----------------------------------------------------------------------
# 存储健壮性
# ----------------------------------------------------------------------
def test_store_persists_across_instances(tmp_path):
    store = AuthStore(tmp_path / "cfg")
    store.create_admin(ADMIN, ADMIN_PW)
    store.register("keepme", "keepmepw")
    reopened = AuthStore(tmp_path / "cfg")
    assert reopened.get("keepme") is not None
    assert reopened.get("keepme").status == STATUS_PENDING
    assert reopened.has_admin()


def test_corrupt_store_is_backed_up_not_crashed(tmp_path):
    directory = tmp_path / "cfg"
    directory.mkdir(parents=True)
    (directory / "users.json").write_text("{not json", encoding="utf-8")
    store = AuthStore(directory)
    assert store.count() == 0
    assert (directory / "users.json.corrupt").exists(), "坏文件必须留备份"


def test_saved_store_has_no_plaintext_password(tmp_path):
    store = AuthStore(tmp_path / "cfg")
    store.create_admin(ADMIN, ADMIN_PW)
    raw = (tmp_path / "cfg" / "users.json").read_text(encoding="utf-8")
    assert ADMIN_PW not in raw


def test_sessions_do_not_survive_restart(tmp_path):
    """会话不落盘：重启即强制重新登录。"""
    store = AuthStore(tmp_path / "cfg")
    store.create_admin(ADMIN, ADMIN_PW)
    session = store.open_session(ADMIN)
    reopened = AuthStore(tmp_path / "cfg")
    assert reopened.resolve_session(session.token) is None


def test_expired_session_is_refused(tmp_path):
    now = {"t": 1000.0}
    store = AuthStore(tmp_path / "cfg", session_ttl=10, clock=lambda: now["t"])
    store.create_admin(ADMIN, ADMIN_PW)
    session = store.open_session(ADMIN)
    assert store.resolve_session(session.token) is not None
    now["t"] += 11
    assert store.resolve_session(session.token) is None
