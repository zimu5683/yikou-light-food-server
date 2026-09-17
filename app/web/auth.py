"""网页版账号体系：注册申请 → 管理员审批 → 登录 → 会话。

为什么需要这一层
----------------
``app.web.server`` 早期的设计假设「运行任务的机器和操作网页的人是同一个人」，
因此整套操作权限只用**一个固定令牌**保护。一旦把域名挂到公网
（Cloudflare Tunnel），这个假设就不成立了：任何人拿到那个网址就等于拿到手机的
全部操作权限（真实下单、读管理后台密码、读写 WPS 云文档）。

本模块提供一套自包含的账号体系，正好对应上面的落差：

* **注册申请**：访客自己填账号密码提交申请，落入 ``pending`` 队列；
* **管理员审批**：``approved`` 之前一律无法登录，管理员在审批页点同意/拒绝；
* **邀请码**：管理员可发码，凭码注册直接通过，省一轮人工审批；
* **会话**：审批通过后登录才发会话令牌，会话是访问 ``/api/*`` 的唯一凭据。

设计要点
--------
* **不引入新依赖**：口令用标准库 ``hashlib.pbkdf2_hmac``；Access JWT 用项目已有
  的 ``cryptography`` 验签（不依赖 PyJWT）。
* **单进程多线程**：``ThreadingHTTPServer`` 是单进程，因此用 ``RLock`` 保护内存
  副本 + 原子落盘即可，不需要文件锁。
* **失败一律保守**：任何校验函数出错都返回「不通过」，绝不因异常而放行。
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import re
import secrets
import stat
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from app.core.config import user_data_dir

# ----------------------------------------------------------------------
# 常量
# ----------------------------------------------------------------------
USERS_FILE = "users.json"

#: PBKDF2 迭代次数。手机上单次约 0.1s，登录可接受，暴力破解代价高。
PBKDF2_ITERATIONS = 200_000
PBKDF2_ALGORITHM = "sha256"

#: 会话有效期（秒）。30 天，避免访客频繁重新登录。
SESSION_TTL_SECONDS = 30 * 24 * 3600

#: 单个口令的最短长度。
MIN_PASSWORD_LENGTH = 6

#: 用户名规则：邮箱或普通用户名都允许，禁止空白与斜杠等会污染日志的字符。
USERNAME_RE = re.compile(r"^[A-Za-z0-9_.@+-]{3,64}$")

STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"

ROLE_ADMIN = "admin"
ROLE_USER = "user"

STATUS_LABELS = {
    STATUS_PENDING: "待审批",
    STATUS_APPROVED: "已批准",
    STATUS_REJECTED: "已拒绝",
}


class AuthError(Exception):
    """鉴权/注册流程中可以直接展示给用户的错误。"""


# ----------------------------------------------------------------------
# 口令散列
# ----------------------------------------------------------------------
def hash_password(password: str, *, iterations: int = PBKDF2_ITERATIONS,
                  salt: bytes | None = None) -> str:
    """返回 ``pbkdf2_sha256$迭代次数$盐$散列`` 形式的自描述字符串。"""
    if not password:
        raise AuthError("密码不能为空")
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(PBKDF2_ALGORITHM, password.encode("utf-8"), salt, iterations)
    return "pbkdf2_{}${}${}${}".format(
        PBKDF2_ALGORITHM, iterations,
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(digest).decode("ascii"),
    )


def verify_password(password: str, encoded: str) -> bool:
    """校验口令。任何格式错误都返回 False，绝不抛异常给调用方。"""
    if not password or not encoded:
        return False
    try:
        scheme, iterations, salt_b64, digest_b64 = encoded.split("$", 3)
        if not scheme.startswith("pbkdf2_"):
            return False
        algorithm = scheme[len("pbkdf2_"):]
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(digest_b64)
        actual = hashlib.pbkdf2_hmac(algorithm, password.encode("utf-8"), salt, int(iterations))
    except (ValueError, TypeError, binascii.Error):
        return False
    return hmac.compare_digest(actual, expected)


# ----------------------------------------------------------------------
# 数据模型
# ----------------------------------------------------------------------
@dataclass
class User:
    username: str
    password: str          # pbkdf2 自描述串
    status: str = STATUS_PENDING
    role: str = ROLE_USER
    created_at: float = field(default_factory=time.time)
    decided_at: float = 0.0
    decided_by: str = ""
    note: str = ""
    invite_code: str = ""

    @property
    def is_admin(self) -> bool:
        return self.role == ROLE_ADMIN and self.status == STATUS_APPROVED

    @property
    def can_login(self) -> bool:
        return self.status == STATUS_APPROVED

    def public(self) -> dict[str, Any]:
        """给界面用的视图：**绝不含口令散列**。"""
        return {
            "username": self.username,
            "status": self.status,
            "status_label": STATUS_LABELS.get(self.status, self.status),
            "role": self.role,
            "created_at": self.created_at,
            "decided_at": self.decided_at,
            "decided_by": self.decided_by,
            "note": self.note,
        }


@dataclass
class Session:
    token: str
    username: str
    created_at: float = field(default_factory=time.time)
    expires_at: float = 0.0
    remote: str = ""
    #: 判断过期用的时钟。默认真实时间；测试可注入，否则「注入 clock 的存储」
    #: 会与这里用真实时间的判断互相矛盾（过期测试因此永远失败）。
    clock: Any = time.time

    @property
    def expired(self) -> bool:
        return self.clock() >= self.expires_at


# ----------------------------------------------------------------------
# 存储
# ----------------------------------------------------------------------
class AuthStore:
    """账号与会话的持久化存储（单进程、线程安全）。

    ``directory`` 缺省为用户配置目录；测试可注入临时目录以隔离状态。
    """

    def __init__(self, directory: Path | str, *, session_ttl: int = SESSION_TTL_SECONDS,
                 clock: Any = time.time) -> None:
        self.dir = Path(directory)
        self.path = self.dir / USERS_FILE
        self.session_ttl = session_ttl
        self._clock = clock
        self._lock = threading.RLock()
        self._users: dict[str, User] = {}
        self._sessions: dict[str, Session] = {}
        self._invites: dict[str, dict[str, Any]] = {}
        self.load()

    # -- 落盘 ----------------------------------------------------------
    def load(self) -> None:
        """读取 users.json；文件缺失或损坏时以空状态启动（并备份坏文件）。"""
        with self._lock:
            self._users, self._sessions, self._invites = {}, {}, {}
            try:
                raw = self.path.read_text(encoding="utf-8")
            except OSError:
                return
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                # 不静默丢数据：留一份 .corrupt 备份便于人工恢复。
                try:
                    self.path.with_suffix(".json.corrupt").write_text(raw, encoding="utf-8")
                except OSError:
                    pass
                return
            if not isinstance(payload, dict):
                return
            for item in payload.get("users") or []:
                if not isinstance(item, dict):
                    continue
                try:
                    user = User(**item)
                except TypeError:
                    continue
                self._users[user.username] = user
            for item in payload.get("invites") or []:
                if not isinstance(item, dict):
                    continue
                code = str(item.get("code") or "")
                if code:
                    self._invites[code] = dict(item)
            # 会话不落盘：重启后全部失效，等价于强制重新登录（更安全）。

    def save(self) -> None:
        """原子写入：先写临时文件再 ``os.replace``，避免掉电产生半截 JSON。"""
        with self._lock:
            payload = {
                "version": 1,
                "users": [asdict(u) for u in self._users.values()],
                "invites": list(self._invites.values()),
            }
            self.dir.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, self.path)
            try:
                # Android 的应用私有目录本来就隔离，chmod 失败不影响安全性。
                os.chmod(self.path, stat.S_IRUSR | stat.S_IWUSR)
            except OSError:
                pass

    # -- 查询 ----------------------------------------------------------
    def get(self, username: str) -> User | None:
        with self._lock:
            return self._users.get((username or "").strip())

    def count(self) -> int:
        with self._lock:
            return len(self._users)

    def has_admin(self) -> bool:
        with self._lock:
            return any(u.is_admin for u in self._users.values())

    def list_users(self, status: str = "") -> list[User]:
        with self._lock:
            users = list(self._users.values())
        if status:
            users = [u for u in users if u.status == status]
        # 待审批的排前面，其次按申请时间倒序，方便管理员处理。
        order = {STATUS_PENDING: 0, STATUS_REJECTED: 1, STATUS_APPROVED: 2}
        users.sort(key=lambda u: (order.get(u.status, 9), -u.created_at))
        return users

    def pending_count(self) -> int:
        with self._lock:
            return sum(1 for u in self._users.values() if u.status == STATUS_PENDING)

    # -- 注册与审批 ----------------------------------------------------
    def register(self, username: str, password: str, *, invite_code: str = "",
                 remote: str = "") -> tuple[User, bool]:
        """提交注册申请。

        返回 ``(用户, 是否已直接通过)``：带有效邀请码时直接 ``approved``，
        否则进入 ``pending`` 等待管理员审批。
        """
        username = (username or "").strip()
        if not USERNAME_RE.match(username):
            raise AuthError("账号需为 3-64 位的字母、数字或 . _ @ + - 字符")
        if len(password or "") < MIN_PASSWORD_LENGTH:
            raise AuthError(f"密码至少 {MIN_PASSWORD_LENGTH} 位")

        with self._lock:
            if username in self._users:
                raise AuthError("该账号已存在，请直接登录或换一个账号")
            auto = False
            code = (invite_code or "").strip()
            if code:
                invite = self._invites.get(code)
                if invite is None or invite.get("revoked"):
                    raise AuthError("邀请码无效或已被停用")
                if int(invite.get("uses", 0)) >= int(invite.get("max_uses", 1)):
                    raise AuthError("该邀请码使用次数已用完")
                auto = True

            user = User(
                username=username,
                password=hash_password(password),
                status=STATUS_APPROVED if auto else STATUS_PENDING,
                role=ROLE_USER,
                invite_code=code,
                decided_at=self._clock() if auto else 0.0,
                decided_by="invite" if auto else "",
                note=f"来自 {remote}" if remote else "",
            )
            self._users[username] = user
            if auto:
                invite["uses"] = int(invite.get("uses", 0)) + 1
            self.save()
            return user, auto

    def create_admin(self, username: str, password: str, *, force: bool = False) -> User:
        """创建（或重置）管理员账号，用于首次播种。"""
        username = (username or "").strip()
        if not USERNAME_RE.match(username):
            raise AuthError("管理员账号名不合法")
        if len(password or "") < MIN_PASSWORD_LENGTH:
            raise AuthError(f"密码至少 {MIN_PASSWORD_LENGTH} 位")
        with self._lock:
            existing = self._users.get(username)
            if existing is not None and not force:
                raise AuthError("该账号已存在（用 --force-admin 可重置密码）")
            if existing is not None:
                existing.password = hash_password(password)
                existing.status = STATUS_APPROVED
                existing.role = ROLE_ADMIN
                existing.decided_at = self._clock()
                existing.decided_by = "cli"
                user = existing
            else:
                user = User(username=username, password=hash_password(password),
                            status=STATUS_APPROVED, role=ROLE_ADMIN,
                            decided_at=self._clock(), decided_by="cli")
                self._users[username] = user
            self.save()
            return user

    def approve(self, username: str, *, by: str = "", role: str = ROLE_USER) -> User:
        return self._decide(username, STATUS_APPROVED, by=by, role=role)

    def reject(self, username: str, *, by: str = "", note: str = "") -> User:
        return self._decide(username, STATUS_REJECTED, by=by, note=note)

    def _decide(self, username: str, status: str, *, by: str = "", role: str = "",
                note: str = "") -> User:
        with self._lock:
            user = self._users.get((username or "").strip())
            if user is None:
                raise AuthError("账号不存在")
            user.status = status
            if role:
                user.role = role
            user.decided_at = self._clock()
            user.decided_by = by
            if note:
                user.note = note
            if status != STATUS_APPROVED:
                # 被拒/被停用的账号立即踢下线，不等会话自然过期。
                self._drop_sessions_locked(username)
            self.save()
            return user

    def set_password(self, username: str, password: str, *, by: str = "") -> User:
        if len(password or "") < MIN_PASSWORD_LENGTH:
            raise AuthError(f"密码至少 {MIN_PASSWORD_LENGTH} 位")
        with self._lock:
            user = self._users.get((username or "").strip())
            if user is None:
                raise AuthError("账号不存在")
            user.password = hash_password(password)
            self._drop_sessions_locked(user.username)
            self.save()
            return user

    def delete(self, username: str) -> None:
        with self._lock:
            username = (username or "").strip()
            if self._users.pop(username, None) is None:
                raise AuthError("账号不存在")
            self._drop_sessions_locked(username)
            self.save()

    # -- 邀请码 --------------------------------------------------------
    def create_invite(self, *, created_by: str = "", max_uses: int = 1,
                      note: str = "") -> dict[str, Any]:
        with self._lock:
            code = secrets.token_urlsafe(9)
            invite = {
                "code": code,
                "created_at": self._clock(),
                "created_by": created_by,
                "max_uses": max(1, int(max_uses)),
                "uses": 0,
                "note": note,
                "revoked": False,
            }
            self._invites[code] = invite
            self.save()
            return dict(invite)

    def list_invites(self) -> list[dict[str, Any]]:
        with self._lock:
            items = [dict(i) for i in self._invites.values()]
        items.sort(key=lambda i: -float(i.get("created_at") or 0))
        return items

    def revoke_invite(self, code: str) -> None:
        with self._lock:
            invite = self._invites.get((code or "").strip())
            if invite is None:
                raise AuthError("邀请码不存在")
            invite["revoked"] = True
            self.save()

    # -- 登录 / 会话 ---------------------------------------------------
    def authenticate(self, username: str, password: str, *, remote: str = "") -> User:
        """登录。账号不存在与口令错误返回同一句话，避免枚举账号。"""
        with self._lock:
            user = self._users.get((username or "").strip())
            if user is None or not verify_password(password, user.password):
                raise AuthError("账号或密码不正确")
            if user.status == STATUS_PENDING:
                raise AuthError("申请已提交，正在等待管理员审批")
            if user.status == STATUS_REJECTED:
                raise AuthError("该账号的访问申请未获批准")
            return user

    def open_session(self, username: str, *, remote: str = "") -> Session:
        with self._lock:
            user = self._users.get((username or "").strip())
            if user is None or not user.can_login:
                raise AuthError("账号不可用")
            self._purge_locked()
            session = Session(
                token=secrets.token_urlsafe(32),
                username=user.username,
                created_at=self._clock(),
                expires_at=self._clock() + self.session_ttl,
                remote=remote,
                clock=self._clock,
            )
            self._sessions[session.token] = session
            return session

    def resolve_session(self, token: str) -> User | None:
        """会话令牌 → 已批准用户；无效或过期返回 None。"""
        if not token:
            return None
        with self._lock:
            session = self._sessions.get(token)
            if session is None or session.expired:
                self._sessions.pop(token, None)
                return None
            user = self._users.get(session.username)
            # 账号被删/被停用后，旧会话必须立刻失效。
            if user is None or not user.can_login:
                self._sessions.pop(token, None)
                return None
            return user

    def close_session(self, token: str) -> None:
        with self._lock:
            self._sessions.pop(token or "", None)

    def session_count(self) -> int:
        with self._lock:
            self._purge_locked()
            return len(self._sessions)

    def _drop_sessions_locked(self, username: str) -> None:
        for token in [t for t, s in self._sessions.items() if s.username == username]:
            self._sessions.pop(token, None)

    def _purge_locked(self) -> None:
        now = self._clock()
        for token in [t for t, s in self._sessions.items() if now >= s.expires_at]:
            self._sessions.pop(token, None)


# ----------------------------------------------------------------------
# Cloudflare Access（管理页的额外一层，可选）
# ----------------------------------------------------------------------
class AccessVerifier:
    """校验 Cloudflare Access 签发的 JWT（``Cf-Access-Jwt-Assertion``）。

    为什么不能只信任 ``Cf-Access-Authenticated-User-Email`` 请求头：那个头是明文
    的，任何绕过 Cloudflare 直连源站的人都能伪造。只有验签 JWT 才是真正的证明。

    未配置 ``team_domain``/``aud`` 时 :meth:`verify` 返回 ``None``（= 未启用），
    由调用方决定是否放行，从而不影响「先上线、后加 Access」的节奏。
    """

    KEYS_TTL = 3600

    def __init__(self, team_domain: str = "", aud: str = "", *, timeout: int = 8) -> None:
        self.team_domain = (team_domain or "").strip().rstrip("/")
        self.aud = (aud or "").strip()
        self.timeout = timeout
        self._keys: list[dict[str, Any]] = []
        self._keys_ts = 0.0
        self._lock = threading.RLock()

    @property
    def enabled(self) -> bool:
        return bool(self.team_domain and self.aud)

    def _certs_url(self) -> str:
        return f"https://{self.team_domain}/cdn-cgi/access/certs"

    def _fetch_keys(self, *, force: bool = False) -> list[dict[str, Any]]:
        """取 Access 公钥（``/cdn-cgi/access/certs``），带 1 小时缓存。"""
        with self._lock:
            fresh = (time.time() - self._keys_ts) < self.KEYS_TTL
            if self._keys and fresh and not force:
                return self._keys
            try:
                import urllib.request

                with urllib.request.urlopen(self._certs_url(), timeout=self.timeout) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))
                self._keys = [k for k in (payload.get("keys") or []) if isinstance(k, dict)]
                self._keys_ts = time.time()
            except Exception:  # noqa: BLE001 - 取公钥失败时保留旧缓存，绝不因此放行
                pass
            return self._keys

    def verify(self, assertion: str) -> str | None:
        """验签通过返回登录邮箱，否则返回 None。"""
        if not self.enabled or not assertion:
            return None
        try:
            return self._verify_jwt(assertion)
        except Exception:  # noqa: BLE001 - 任何异常都视为不通过
            return None

    def _verify_jwt(self, token: str) -> str | None:
        """手工验签 RS256 JWT。

        刻意不依赖 PyJWT（本项目没装它），只用项目已有依赖 cryptography 完成
        「解 header / 取公钥 / 验签 / 校验 exp·iat·iss·aud」这几步。
        """
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding

        parts = (token or "").split(".")
        if len(parts) != 3:
            return None
        header_b64, payload_b64, signature_b64 = parts
        header = json.loads(_b64url_decode(header_b64))
        if str(header.get("alg", "")).upper() not in {"RS256", "RS384", "RS512"}:
            # 只接受 RSA 系列：``none``/``HS256`` 是经典的算法混淆攻击面。
            return None
        kid = header.get("kid")
        signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
        signature = _b64url_decode(signature_b64)

        hash_map = {"RS256": hashes.SHA256(), "RS384": hashes.SHA384(), "RS512": hashes.SHA512()}
        algorithm = hash_map[str(header["alg"]).upper()]

        for force in (False, True):
            keys = self._fetch_keys(force=force)
            if not keys:
                continue
            match = next((k for k in keys if not kid or k.get("kid") == kid), None)
            if match is None:
                continue
            public_key = _rsa_public_key_from_jwk(match)
            if public_key is None:
                continue
            try:
                public_key.verify(signature, signing_input, padding.PKCS1v15(), algorithm)
            except InvalidSignature:
                continue  # 换一把钥匙再试（密钥轮换期间新旧并存）
            payload = json.loads(_b64url_decode(payload_b64))
            if not self._claims_ok(payload):
                return None
            email = payload.get("email") or payload.get("sub")
            return str(email) if email else None
        return None

    def _claims_ok(self, payload: dict[str, Any]) -> bool:
        """校验时间与受众/签发方声明。"""
        now = time.time()
        try:
            exp = float(payload.get("exp"))
            iat = float(payload.get("iat"))
        except (TypeError, ValueError):
            return False
        if now >= exp or now + 60 < iat:
            return False
        aud = payload.get("aud")
        audiences = aud if isinstance(aud, list) else [aud]
        if self.aud not in [str(a) for a in audiences if a is not None]:
            return False
        return str(payload.get("iss", "")).rstrip("/") == f"https://{self.team_domain}"


def _b64url_decode(value: str) -> bytes:
    """base64url 解码并补齐 padding。"""
    padded = value + "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii"))


def _rsa_public_key_from_jwk(jwk: dict[str, Any]) -> Any:
    """把 Cloudflare Access 公钥转成可验签的对象。

    优先用 JWK 的 ``n``/``e``；Cloudflare 有时只给 ``x5c``（证书链），此时退化为
    从证书里取公钥。两者都没有则返回 None（调用方按「不通过」处理）。
    """
    from cryptography import x509
    from cryptography.hazmat.primitives.asymmetric import rsa

    n, e = jwk.get("n"), jwk.get("e")
    if n and e:
        modulus = int.from_bytes(_b64url_decode(str(n)), "big")
        exponent = int.from_bytes(_b64url_decode(str(e)), "big")
        return rsa.RSAPublicNumbers(exponent, modulus).public_key()

    chain = jwk.get("x5c") or []
    if chain:
        cert = x509.load_der_x509_certificate(_b64url_decode(str(chain[0])))
        return cert.public_key()
    return None


def access_verifier_from_env() -> AccessVerifier:
    """从环境变量读取 Access 配置（``YIKOU_ACCESS_TEAM_DOMAIN`` / ``YIKOU_ACCESS_AUD``）。"""
    return AccessVerifier(
        os.environ.get("YIKOU_ACCESS_TEAM_DOMAIN", ""),
        os.environ.get("YIKOU_ACCESS_AUD", ""),
    )


def default_auth_dir() -> Path:
    """账号数据默认落在用户配置目录（手机上是 ``~/.config/yikou-light-food``）。"""
    return user_data_dir()
