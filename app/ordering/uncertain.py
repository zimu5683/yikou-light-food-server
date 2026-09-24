"""闪时送非幂等 POST 的本地“已发送未知”记录与跨运行阻断。

平台当前没有客户端幂等字段：POST 一旦发出后超时/断线，无法证明服务端是否
落单。本模块只记录“哪些任务、什么时间、什么指纹、什么错误”为 unresolved，
并在只读对账确认后标记 resolved；后续运行只要发现同一批次键仍有 unresolved
记录，就会拒绝再次自动提交，避免重启绕过。
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import re
import tempfile
import time
import unicodedata
import uuid
from contextlib import contextmanager
from pathlib import Path
from threading import Lock
from typing import Any, Callable, Iterator

try:  # POSIX 跨进程建议锁；Android/Termux/Linux 都提供 fcntl。
    import fcntl
except ImportError:  # pragma: no cover - 不支持的非 POSIX 平台
    fcntl = None  # type: ignore[assignment]

from app.order.common import _emit
from app.integrations.sss_url import SssUrlConfigError, canonical_sss_origin
from app.ordering.constants import (
    DEFAULT_SSS_URL,
    _BATCH_CLOCK_SKEW_S,
    _PREFILTER_ZERO_RETRY_DELAY_S,
    _RECONCILE_POLL_ATTEMPTS,
)
from app.ordering.models import OrderFingerprint, _Reconciliation
from app.ordering.reconcile import _safe_reconcile


class UncertainJournalError(RuntimeError):
    """本地不确定记录读写失败；调用方必须停止 POST。"""


_JOURNAL_LOCKS: dict[str, Lock] = {}
_JOURNAL_LOCKS_GUARD = Lock()
# 等待另一个进程释放日记锁的上限；超时则明确拒绝写入，绝不冒并发覆盖风险。
try:
    _JOURNAL_LOCK_TIMEOUT_S = max(
        0.0, float(os.environ.get("YIKOU_SSS_JOURNAL_LOCK_TIMEOUT", "10") or 10))
except (TypeError, ValueError):
    _JOURNAL_LOCK_TIMEOUT_S = 10.0


def _journal_lock(path: Path) -> Lock:
    """按规范化路径串行化同进程读-改-写，避免线程并发丢记录。"""
    resolved = str(path.resolve())
    with _JOURNAL_LOCKS_GUARD:
        lock = _JOURNAL_LOCKS.get(resolved)
        if lock is None:
            lock = Lock()
            _JOURNAL_LOCKS[resolved] = lock
        return lock


class _AdvisoryLock:
    """基于 ``fcntl.flock`` 的跨进程建议锁，可显式 acquire/release。

    锁文件保持存在；不 unlink，避免两个进程各自持不同 inode 的锁。进程崩溃
    时内核自动释放建议锁，因此无需清理陈旧锁；如果平台没有可用锁原语，
    直接抛 ``UncertainJournalError``，由调用方阻止提交。
    """

    def __init__(self, lock_path: Path, timeout: float | None = None) -> None:
        self.lock_path = Path(lock_path)
        self.timeout = _JOURNAL_LOCK_TIMEOUT_S if timeout is None else max(0.0, float(timeout))
        self._fd: int | None = None
        self._acquired = False

    def acquire(self) -> None:
        if self._acquired:
            return
        if fcntl is None:  # pragma: no cover - 只跑 Android/Termux/Linux
            raise UncertainJournalError(
                "当前平台不支持跨进程文件锁，拒绝写入不确定记录以避免并发覆盖")
        try:
            self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise UncertainJournalError(
                f"无法创建不确定记录锁目录 {self.lock_path.parent}：{exc}") from exc
        try:
            self._fd = os.open(str(self.lock_path), os.O_CREAT | os.O_RDWR, 0o600)
        except OSError as exc:
            raise UncertainJournalError(
                f"无法创建不确定记录锁 {self.lock_path}：{exc}") from exc
        deadline = time.monotonic() + self.timeout
        try:
            while True:
                try:
                    if fcntl is None:  # pragma: no cover - 只跑 Android/Termux/Linux
                        raise UncertainJournalError(
                            f"当前平台没有可用的跨进程锁原语：{self.lock_path}")
                    fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    self._acquired = True
                    return
                except OSError as exc:
                    if time.monotonic() >= deadline:
                        raise UncertainJournalError(
                            f"等待另一个进程释放不确定记录锁超时：{self.lock_path}（{exc}）；"
                            "为避免并发覆盖，已拒绝本次写入") from exc
                    time.sleep(0.05)
        except BaseException:
            self.release()
            raise

    def release(self) -> None:
        if self._fd is None:
            return
        if self._acquired:
            try:
                if fcntl is not None:
                    fcntl.flock(self._fd, fcntl.LOCK_UN)
            except OSError:
                pass
        try:
            os.close(self._fd)
        finally:
            self._fd = None
            self._acquired = False

    def __enter__(self) -> "_AdvisoryLock":
        self.acquire()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.release()


@contextmanager
def _journal_file_lock(path: Path) -> Iterator[None]:
    """单次 journal 读-改-写使用的跨进程建议锁。"""
    with _AdvisoryLock(path.with_name(f"{path.name}.lock")):
        yield


def batch_submission_lock(journal_path: str | os.PathLike[str], key: str,
                          timeout: float | None = None) -> _AdvisoryLock:
    """业务批次级跨进程锁：覆盖“读 unresolved → 只读对账 → POST → 结果落盘”。

    runner 必须在任何 POST 之前 acquire，并在本轮对账/记录已经可靠落盘后
    release。第二个进程会等待；超时则明确拒绝，不允许两个进程同时 POST。
    锁身份刻意使用业务批次键（日期 + 规范化账号）摘要，不包含 platform/
    workbook/source；它是对“origin+规范化账号”权威状态身份的保守粗锁，
    即使平台身份/配置解析出现差异，也不会让同一账号/日期并发 POST。锁文件
    放在系统临时目录下，``journal_path`` 参数保留以兼容调用方。
    """
    _ = journal_path  # 锁粒度只取决于业务批次键，避免不同路径绕过互斥
    digest = hashlib.sha256(str(key).encode("utf-8")).hexdigest()[:32]
    # 锁根使用机器级稳定目录，不随 TMPDIR/YIKOU_DATA_DIR/权威根覆盖漂移；
    # 同一批次在不同 TMPDIR/不同配置下仍命中同一把锁。不同账号/平台由
    # 业务批次键与各自 authority 文件隔离。
    return _AdvisoryLock(_machine_lock_root() / f"{digest}.lock", timeout)


def default_uncertain_path(config: Any = None) -> Path:
    """返回不确定记录路径：显式配置 → 环境变量 → 每用户数据目录。"""
    explicit = getattr(config, "sss_uncertain_path", None) if config is not None else None
    if not explicit:
        explicit = os.environ.get("YIKOU_SSS_UNCERTAIN_PATH", "")
    if explicit:
        return Path(explicit).expanduser()
    try:
        from app.core.config import user_data_dir
        return user_data_dir() / "sss_uncertain.json"
    except Exception:  # pragma: no cover - 仅在配置层不可用时兜底
        return Path(tempfile.gettempdir()) / "yikou-sss-uncertain.json"


_ACCOUNT_WHITESPACE_RE = re.compile(r"\s+")


def normalise_account(value: Any) -> str:
    """规范化账号写法，只合并业务上明确等价的差异。

    规则刻意保持保守：
    - NFKC 统一全角数字/字符；
    - 删除所有空白（含全角空格），例如 ``187 5818 7837`` → ``18758187837``；
    - 不 lowercase、不做 int 转换、不丢前导零、不删其它标点，避免把不同账号合并。
    """
    if value is None:
        return ""
    return _ACCOUNT_WHITESPACE_RE.sub("", unicodedata.normalize("NFKC", str(value)))


def batch_key(delivery_date: Any, source: Any, account: Any) -> str:
    """批次阻断键：`送达日|规范化账号`。

    SN-C8：``source`` 只是可变配置（excel/wps），不能作为绕过阻断的条件。
    账号只做保守规范化（去空白/NFKC），不合并不同号码，也不丢前导零。
    """
    return "|".join((str(delivery_date or ""), normalise_account(account)))


def legacy_batch_key(delivery_date: Any, source: Any, account: Any) -> str:
    """旧版三段键，仅供兼容自检/旧 journal 迁移使用。"""
    return "|".join((str(delivery_date or ""), str(source or ""), str(account or "")))


def _record_matches_batch(record: dict[str, Any], key: str) -> bool:
    """判断 journal 记录是否属于当前批次，兼容 source/账号写法的旧数据。

    新键是 `date|normalized_account`；旧 journal 的 ``batch_key`` 是
    `date|source|account`。历史 ``account`` 可能带空格/全角，这里统一走
    ``normalise_account`` 比较；只合并空白/NFKC 等明确等价差异。
    """
    raw_batch = str(record.get("batch_key") or "")
    key_text = str(key or "")
    if raw_batch and raw_batch == key_text:
        return True
    parts = key_text.split("|")
    if len(parts) == 2:
        date = parts[0]
        account = normalise_account(parts[1])
    elif len(parts) == 3:  # 调用方仍传旧键
        date = parts[0]
        account = normalise_account(parts[2])
    else:
        return False
    if not date or not account:
        return False
    stored_date = str(record.get("delivery_date") or "")
    stored_account = normalise_account(record.get("account"))
    if stored_date and stored_account and stored_date == date and stored_account == account:
        return True
    old_parts = raw_batch.split("|")
    return (len(old_parts) == 3 and old_parts[0] == date
            and normalise_account(old_parts[2]) == account)


def _is_active_status(record: dict[str, Any]) -> bool:
    return str(record.get("status") or "unresolved") not in {"resolved", "discarded"}


def _deployment_root() -> Path:
    """旧版用户数据根（用于兼容已知旧文件），不作为新权威状态根。"""
    try:
        from app.core.config import user_data_dir
        return user_data_dir()
    except Exception:  # pragma: no cover - 配置层不可用时兜底
        return Path(tempfile.gettempdir()) / "yikou-light-food"


def _authoritative_root() -> Path:
    """机器级权威状态根：不随 YIKOU_DATA_DIR / workbook 路径变化。

    这是本机同一 OS 用户范围内的部署共享根；不同 OS 用户/不同主机不共享。
    可用 ``YIKOU_SSS_AUTHORITATIVE_ROOT`` 覆盖（测试/特殊部署）。
    """
    override = os.environ.get("YIKOU_SSS_AUTHORITATIVE_ROOT", "").strip()
    if override:
        return Path(override).expanduser()
    if os.environ.get("YIKOU_APP_MODE", "").strip().lower() == "android":
        return _deployment_root() / "sss-authoritative"
    # 只跑 Android / Termux / Linux：Windows(LOCALAPPDATA) 与 macOS(Application Support)
    # 分支已随桌面端支持删除。
    base = Path(os.environ.get("XDG_STATE_HOME")
                or (Path.home() / ".local" / "state"))
    return base / "yikou-light-food" / "sss-authoritative"


def _normalise_origin(value: Any) -> str:
    """与 SssApiClient 共用同一 origin 规范化，禁止两套规则漂移。

    只复用 ``app.integrations.api_client.origin_from_url``；无法解析时
    fail-closed，避免不同调用方各自发明“等价”写法。
    """
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        from app.integrations.api_client import origin_from_url
        return origin_from_url(text)
    except ValueError as exc:
        raise UncertainJournalError(
            f"平台 origin 无法规范化：{value!r}（{exc}）；已拒绝读取/迁移") from exc


def platform_origin(config: Any = None, *, url: str | None = None) -> str:
    """实际 API 请求目标 origin；与 SssApiClient.__init__ 使用的规则一致。

    R8-S1：先按 ``app.integrations.sss_url`` 严格校验；非规范/不支持的网址
    （尾点、非 ASCII 域名、缺协议、非法端口等）直接 fail-closed 抛
    ``UncertainJournalError``，绝不按另一种写法另建一个 authority scope ——
    那正是“换写法绕过未决记录”的入口。

    校验通过后仍复用与 ``SssApiClient`` 共用的 ``_normalise_origin``（即
    ``origin_from_url``），保持“只有一套 origin 规范化”的既有约束；对所有受
    支持的写法两者结果完全一致（专项测试逐条锁定）。

    ``url`` 可显式传入**已冻结**的配置字符串（任务启动时读取一次），这样
    执行期 ``config.sss_url`` 被改写也不会改变本次运行的作用域。
    """
    if url is None:
        raw = str(getattr(config, "sss_url", "")
                  if config is not None else "").strip()
    else:
        raw = str(url).strip()
    if not raw:
        raw = DEFAULT_SSS_URL
    try:
        canonical_sss_origin(raw)
    except SssUrlConfigError as exc:
        raise UncertainJournalError(
            f"{exc}；已拒绝读写未决状态（不会发送任何闪时送请求）") from exc
    return _normalise_origin(raw)


def authority_scope_key(config: Any = None, *, origin: str | None = None,
                        account: Any = None) -> str:
    """权威安全状态身份：实际平台 origin + 规范化账号。

    刻意不包含 sss_excel_path / sss_order_source / sss_uncertain_path，
    这样 workbook、名单来源、普通 journal 路径变化都不会改变同一平台/账号的
    未确认状态所在文件。

    R8-S1：origin 只接受规范且受支持的网址写法（大小写/默认端口/path 差异会被
    归一，尾点与非 ASCII 域名直接配置错误）；非规范写法不会再产生第二个 scope，
    不存在「换写法换 journal」的路径。旧写法留下的记录由
    :func:`cross_scope_unresolved_records` 在提交前保守阻断。

    ``origin`` / ``account`` 可显式冻结身份：任务在校验完网址后应把同一个
    origin 传给本函数、journal 元数据与跨作用域核对，避免“校验与执行之间配置
    被改写”时作用域漂移。传入的 origin 由 ``platform_origin`` 生产（已严格
    校验并走共用的 origin 规范化），这里只做一次可解析性自检，**不重新归一**，
    以免“冻结身份”与实际请求 origin 出现两套值。
    """
    if origin is None:
        origin_text = platform_origin(config)
    else:
        origin_text = str(origin).strip()
        if not origin_text:
            raise UncertainJournalError(
                "缺少已冻结的平台 origin，无法计算权威状态身份；已阻断提交")
        canonical_sss_origin(origin_text)
    account_value = account if account is not None else (
        getattr(config, "sss_account", "") if config is not None else "")
    return f"{origin_text}|{normalise_account(account_value)}"


def _machine_lock_root() -> Path:
    """机器级批次锁根：不随 TMPDIR / YIKOU_DATA_DIR / YIKOU_SSS_AUTH_ROOT 漂移。

    可用 ``YIKOU_SSS_LOCK_ROOT`` 精确覆盖（测试/特殊部署）；生产默认落到
    本 OS 用户的状态目录。
    """
    override = os.environ.get("YIKOU_SSS_LOCK_ROOT", "").strip()
    if override:
        return Path(override).expanduser()
    if os.environ.get("YIKOU_APP_MODE", "").strip().lower() == "android":
        return _deployment_root() / "sss-locks"
    # 同 _authoritative_root：只保留 Linux/Termux/Android 的状态目录。
    base = Path(os.environ.get("XDG_STATE_HOME")
                or (Path.home() / ".local" / "state"))
    return base / "yikou-light-food" / "sss-locks"


def authoritative_uncertain_path(config: Any = None, *, origin: str | None = None,
                                 account: Any = None) -> Path:
    """权威共享未决状态路径：平台 origin + 规范化账号摘要。

    ``origin`` / ``account`` 可显式传入已冻结身份（见
    :func:`authority_scope_key`），避免执行期配置漂移改变作用域。
    """
    override = getattr(config, "sss_authoritative_uncertain_path", None) \
        if config is not None else None
    if not override:
        override = os.environ.get("YIKOU_SSS_AUTHORITATIVE_PATH", "")
    if override:
        return Path(override).expanduser()
    identity = authority_scope_key(config, origin=origin, account=account)
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
    return _authoritative_root() / f"{digest}.json"


def _authority_path_is_explicit(config: Any = None) -> bool:
    """当前权威 journal 是否来自显式单文件覆盖（config 或环境变量）。"""
    override = getattr(config, "sss_authoritative_uncertain_path", None) \
        if config is not None else None
    if override:
        return True
    return bool(os.environ.get("YIKOU_SSS_AUTHORITATIVE_PATH", "").strip())


def authority_scan_root(config: Any = None) -> Path | None:
    """跨作用域只读扫描根；显式单文件覆盖时返回 ``None``。

    显式覆盖把所有 scope 固定到同一个文件，URL 写法无法拆分它；同时覆盖目录里
    可能放着与权威未决状态无关的 JSON（旧 journal、诊断文件），扫描会误判，
    因此显式覆盖时不做跨作用域扫描（记录仍由 ``merge_journals`` 按已知旧位置
    保守迁移/阻断）。

    R8-S3：显式路径**切换/取消**（默认↔显式、显式 A→B）不由本函数处理，而是由
    :func:`authority_location_gate` 用部署级位置登记在 POST 前统一阻断。
    """
    if _authority_path_is_explicit(config):
        return None
    return _authoritative_root()


def _record_raw_platform(record: dict[str, Any]) -> str:
    """记录里原始平台字段（不做任何归一化，用于判断“写法是否仍受支持”）。"""
    for key in ("platform", "platform_origin", "origin"):
        value = record.get(key)
        if value not in (None, ""):
            return str(value).strip()
    return ""


def cross_scope_unresolved_records(
        authoritative_journal: str | os.PathLike[str], *,
        config: Any = None,
        account: Any = None,
        origin: Any = None) -> list[dict[str, Any]]:
    """只读扫描本机权威根，返回**无法安全归属**给其他平台身份的活跃未决记录。

    R8-S1 背景：authority 文件按「origin|账号」摘要命名。修复前若用另一种 URL
    写法（尾点/IDN 等）跑过一次并留下 unresolved，改用规范写法启动时当前 scope
    看不到那条记录，批次闸门就被绕过。本函数只读扫描同根所有 journal，按
    「规范化账号 + 记录的原始平台写法」判定，**不做 DNS/TLS/Origin 文本等价
    推断，也不合并任何平台身份**：

    - 记录账号 != 当前账号 → 跳过（不同身份，不能把所有账号一律阻断）；
    - 记录账号 == 当前账号且平台 origin 是**规范写法** → 跳过：规范 origin 直接
      决定 journal 路径（同 origin + 同账号只会有一个权威文件；不同 origin 是
      不同安全域，按既有产品约定不误伤）；
    - 记录账号 == 当前账号但平台字段缺失 → 冲突（无法安全判定归属）；
    - 记录账号 == 当前账号但平台写法已不受支持（尾点、非 ASCII 域名、非法
      端口等，``canonical_sss_origin`` 判定失败）→ 冲突：这正是 R8-S1 已复现
      的旧写法作用域，必须保守阻断并要求人工只读核对；
    - 记录缺少账号 → 冲突（无法判定是否属于当前账号）。

    只有 ``resolved``/``discarded`` 之外的记录才算活跃未决；当前 journal 自己
    不算（它由既有 pending/对账逻辑处理）。任何同根 journal 不可读/损坏都抛
    ``UncertainJournalError``，绝不当空文件放行。本函数**只读**：不迁移、不改写、
    不删除、不把记录标成 verified。

    显式 ``sss_authoritative_uncertain_path`` / ``YIKOU_SSS_AUTHORITATIVE_PATH``
    覆盖时返回 ``[]``（原因见 :func:`authority_scan_root`）。
    """
    root = authority_scan_root(config)
    if root is None or not root.is_dir():
        return []
    current_account = normalise_account(
        account if account is not None else
        (getattr(config, "sss_account", "") if config is not None else ""))
    if not current_account:
        raise UncertainJournalError(
            "缺少规范化账号，无法做跨作用域未决记录只读核对；已阻断提交")
    current_origin = ""
    if origin not in (None, ""):
        try:
            current_origin = canonical_sss_origin(origin)
        except SssUrlConfigError as exc:
            raise UncertainJournalError(
                f"{exc}；无法做跨作用域未决记录只读核对，已阻断提交") from exc
    try:
        current = Path(authoritative_journal).resolve()
    except OSError:
        current = Path(authoritative_journal)

    conflicts: list[dict[str, Any]] = []
    for candidate in sorted(root.glob("*.json")):
        try:
            resolved = candidate.resolve()
        except OSError:
            resolved = candidate
        if resolved == current:
            continue
        try:
            payload = load_journal(candidate)
        except UncertainJournalError as exc:
            raise UncertainJournalError(
                f"跨作用域只读核对失败：权威目录中的 {candidate} 不可读/损坏"
                f"（{exc}）；为避免漏掉未决记录，已阻断本次提交。"
                "请人工核对该文件后再运行（不要删除，也不要并行重跑）") from exc
        for record in payload.get("records", []):
            if not isinstance(record, dict) or not _is_active_status(record):
                continue
            record_account = normalise_account(record.get("account"))
            if record_account and record_account != current_account:
                continue
            raw_platform = _record_raw_platform(record)
            normalized_platform = ""
            reason = ""
            if not record_account:
                reason = "记录缺少账号信息，无法安全判定是否属于当前账号"
            elif not raw_platform:
                reason = "记录账号与当前一致，但缺少平台 origin，无法安全判定归属"
            else:
                try:
                    normalized_platform = canonical_sss_origin(raw_platform)
                except SssUrlConfigError:
                    reason = (f"记录使用的网址写法已不再支持（{raw_platform}），"
                              "无法安全判定是否与当前平台同源")
                else:
                    if not current_origin:
                        reason = "缺少当前 origin，无法安全判定归属"
                    else:
                        # 规范写法：origin 直接决定 journal 路径，同 origin + 同
                        # 账号不会出现第二份权威文件；不同 origin 属不同安全域。
                        # 两种都不做“可能同源”的推断，也不据此合并/阻断。
                        continue
            conflicts.append({
                "journal": str(candidate),
                "journal_id": str(record.get("journal_id")
                                  or record.get("identifier") or ""),
                "identifier": str(record.get("identifier") or ""),
                "batch_key": str(record.get("batch_key") or ""),
                "delivery_date": str(record.get("delivery_date") or ""),
                "account": str(record.get("account") or ""),
                "platform": raw_platform,
                "normalized_platform": normalized_platform,
                "current_origin": current_origin,
                "status": str(record.get("status") or "unresolved"),
                "created_at": str(record.get("created_at") or ""),
                "reason": reason,
            })
    return conflicts


def describe_cross_scope_conflicts(conflicts: list[dict[str, Any]],
                                   *, limit: int = 5) -> str:
    """把跨作用域冲突整理成给用户看的短句（含 journal 名与记录 id）。"""
    parts: list[str] = []
    for item in conflicts[:max(1, int(limit))]:
        name = Path(str(item.get("journal") or "")).name or "?"
        record_id = str(item.get("journal_id") or item.get("identifier") or "?")
        platform = str(item.get("platform") or "未知/缺失")
        parts.append(f"{name}#{record_id}（记录平台={platform}）")
    text = "、".join(parts)
    if len(conflicts) > len(parts):
        text += f" 等共 {len(conflicts)} 条"
    return text


# ---------------------------------------------------------------------------
# R8-S3：权威状态「位置」登记与切换闸门
#
# 背景：显式权威路径（config.sss_authoritative_uncertain_path /
# YIKOU_SSS_AUTHORITATIVE_PATH）被启用、切换或取消时，旧位置的活跃 unresolved
# 会变得不可见（authority_scan_root 在显式覆盖时返回 None），从而放行第二次
# 下单 POST。这里用一个**部署级、不随被切换路径移动**的登记锚点：
#   * 位置集合 = {当前 journal} ∪ {默认权威根} ∪ {登记文件里的历史位置}；
#   * 其他位置里有**当前账号**的活跃未决记录 → 阻断（不看 origin，账号隔离保留）；
#   * 位置不可读/损坏 → fail-closed；位置不存在 → 无记录，登记项显式清理；
#   * 全部干净后先把当前位置登记落盘（POST 之前），再继续既有流程。
# ---------------------------------------------------------------------------

_AUTHORITY_LOCATIONS_ENV = "YIKOU_SSS_AUTHORITY_LOCATIONS"
_AUTHORITY_LOCATIONS_FILENAME = "sss-authority-locations.json"
_AUTHORITY_LOCATIONS_VERSION = 1


def authority_locations_path(config: Any = None) -> Path:
    """权威位置登记文件：部署级稳定锚点（不随 authority 路径切换移动）。

    R8-S3：登记文件放在部署数据目录（``_deployment_root()``），因此切换或取消
    ``sss_authoritative_uncertain_path`` / ``YIKOU_SSS_AUTHORITATIVE_PATH``
    都不会让它一起消失；可用 ``YIKOU_SSS_AUTHORITY_LOCATIONS`` 覆盖
    （测试/特殊部署，或运维显式声明需要核对的旧位置）。
    """
    override = (getattr(config, "sss_authority_locations_path", None)
                if config is not None else None)
    if not override:
        override = os.environ.get(_AUTHORITY_LOCATIONS_ENV, "").strip()
    if override:
        return Path(override).expanduser()
    return _deployment_root() / _AUTHORITY_LOCATIONS_FILENAME


def _resolve_location(path: str | os.PathLike[str]) -> str:
    try:
        return str(Path(path).resolve())
    except OSError:  # pragma: no cover - 极少数平台路径解析失败
        return str(Path(path))


def _validate_authority_locations(payload: Any, target: Path
                                  ) -> dict[str, dict[str, Any]]:
    """校验登记文件结构；任何异常都 fail-closed（绝不当作首次运行）。"""
    if not isinstance(payload, dict):
        raise UncertainJournalError(
            f"权威位置登记结构异常（根节点必须为对象）：{target}；已拒绝读取，"
            "请人工核对后再运行")
    version = payload.get("version", 1)
    if isinstance(version, bool) or not isinstance(version, int) \
            or version != _AUTHORITY_LOCATIONS_VERSION:
        raise UncertainJournalError(
            f"权威位置登记版本不受支持：{target}（version={version!r}）；"
            "已拒绝读取，请人工确认后再运行")
    items = payload.get("locations", [])
    if not isinstance(items, list):
        raise UncertainJournalError(
            f"权威位置登记缺少 locations 列表：{target}；已拒绝读取")
    known: dict[str, dict[str, Any]] = {}
    for index, item in enumerate(items):
        where = f"{target} 第 {index + 1} 个位置"
        if not isinstance(item, dict):
            raise UncertainJournalError(f"{where}不是对象；已拒绝读取，不能当首次运行放行")
        raw_path = str(item.get("path") or "").strip()
        if not raw_path:
            raise UncertainJournalError(f"{where}缺少 path；已拒绝读取")
        entry = dict(item)
        entry["path"] = _resolve_location(raw_path)
        known[entry["path"]] = entry
    return known


def load_authority_locations(config: Any = None) -> dict[str, dict[str, Any]]:
    """读取权威位置登记；文件不存在=首次运行；损坏/版本不支持=fail-closed。"""
    target = authority_locations_path(config)
    if not target.exists():
        return {}
    if not target.is_file():
        raise UncertainJournalError(
            f"权威位置登记路径不是普通文件：{target}；不能当作首次运行，"
            "已阻断本次提交。请人工核对该路径后再运行")
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UncertainJournalError(
            f"权威位置登记文件损坏或不可读：{target}（{exc}）；不能当作首次运行，"
            "已阻断本次提交。请人工核对该文件后再运行"
            "（不要删除权威记录文件，也不要并行重跑）") from exc
    return _validate_authority_locations(payload, target)


def _location_file_records(location: Path, *, skip: set[str]
                           ) -> list[tuple[Path, dict[str, Any]]]:
    """只读收集一个权威位置（文件或目录）里的全部记录；损坏/不可读 fail-closed。"""
    if location.is_file():
        payload = load_journal(location)
        return [(location, record) for record in payload.get("records", [])
                if isinstance(record, dict)]
    if not location.is_dir():
        if location.exists():
            raise UncertainJournalError(
                f"权威位置既不是文件也不是目录，无法核对：{location}；"
                "已阻断本次提交，请人工核对该路径")
        return []
    found: list[tuple[Path, dict[str, Any]]] = []
    for candidate in sorted(location.glob("*.json")):
        if _resolve_location(candidate) in skip:
            continue
        payload = load_journal(candidate)
        found.extend((candidate, record) for record in payload.get("records", [])
                     if isinstance(record, dict))
    return found


def _location_is_current(location: Path, *, current_key: str,
                         explicit: bool) -> bool:
    """该位置是否就是当前权威位置（同一文件，或未覆盖时的默认根自身）。"""
    if _resolve_location(location) == current_key:
        return True
    if not explicit and location.is_dir():
        try:
            Path(current_key).relative_to(_resolve_location(location))
            return True
        except ValueError:
            return False
    return False


def authority_location_gate(
        authoritative_journal: str | os.PathLike[str], *,
        config: Any = None,
        account: Any = None,
        origin: Any = None) -> dict[str, Any]:
    """权威位置切换闸门：核对其他已知位置，并在 POST 前登记当前位置。

    返回 ``{"conflicts", "pruned", "checked", "registry", "registered"}``：

    * ``conflicts`` 非空 → 调用方必须阻断（不改写任何 journal 记录）；
    * 无冲突 → 在**同一次登记锁**内清理已不存在的旧位置并原子写入当前位置；
      写入失败抛 ``UncertainJournalError``（绝不当作首次运行放行）。

    归属判定按**规范化账号**：账号相同或缺账号 → 冲突（**不看 origin**，同一规范
    origin 留在另一个文件同样算“位置切换”）；账号不同 → 跳过（账号隔离）。
    """
    registry = authority_locations_path(config)
    current = Path(authoritative_journal)
    current_key = _resolve_location(current)
    current_account = normalise_account(
        account if account is not None else
        (getattr(config, "sss_account", "") if config is not None else ""))
    if not current_account:
        raise UncertainJournalError(
            "缺少规范化账号，无法核对权威位置切换；已阻断提交")
    explicit = _authority_path_is_explicit(config)

    lock = _AdvisoryLock(registry.with_name(f"{registry.name}.lock"))
    lock.acquire()
    try:
        known = load_authority_locations(config)
        conflicts: list[dict[str, Any]] = []
        pruned: list[str] = []
        checked: list[str] = []
        # 已登记的文件位置单独作为扫描单元；目录扫描时跳过这些文件与当前文件，
        # 避免同一个记录被“目录 + 文件”重复计数。
        checked_files = {current_key}
        for key in known:
            try:
                if Path(key).is_file():
                    checked_files.add(key)
            except OSError:  # pragma: no cover - 路径异常按不可比对处理
                continue
        skip_paths = {_resolve_location(registry)} | checked_files
        seen_conflicts: set[tuple[str, str]] = set()

        def _collect(location_key: str) -> None:
            checked.append(location_key)
            for journal, record in _location_file_records(Path(location_key),
                                                          skip=skip_paths):
                if not _is_active_status(record):
                    continue
                record_account = normalise_account(record.get("account"))
                if record_account and record_account != current_account:
                    continue
                journal_id = str(record.get("journal_id")
                                 or record.get("identifier") or "")
                dedupe_key = (_resolve_location(journal), journal_id)
                if dedupe_key in seen_conflicts:
                    continue
                seen_conflicts.add(dedupe_key)
                conflicts.append({
                    "location": location_key,
                    "journal": str(journal),
                    "journal_id": journal_id,
                    "identifier": str(record.get("identifier") or ""),
                    "batch_key": str(record.get("batch_key") or ""),
                    "delivery_date": str(record.get("delivery_date") or ""),
                    "account": str(record.get("account") or ""),
                    "platform": _record_raw_platform(record),
                    "status": str(record.get("status") or "unresolved"),
                    "created_at": str(record.get("created_at") or ""),
                    "reason": ("记录缺少账号信息，无法安全判定是否属于当前账号"
                               if not record_account else
                               "同一账号的活跃未决记录位于另一个权威位置"),
                })

        for key in sorted(known):
            location = Path(key)
            if _location_is_current(location, current_key=current_key,
                                    explicit=explicit):
                continue
            if not location.exists():
                pruned.append(key)
                continue
            _collect(key)
        # 默认权威根永远算“已知位置”：不需要登记就能发现 默认↔显式 双向切换。
        default_root = _authoritative_root()
        default_key = _resolve_location(default_root)
        if (default_key not in known
                and default_key != current_key
                and not _location_is_current(default_root, current_key=current_key,
                                             explicit=explicit)
                and default_root.exists()):
            _collect(default_key)

        if conflicts:
            return {"conflicts": conflicts, "pruned": pruned, "checked": checked,
                    "registry": str(registry), "registered": False}

        timestamp = _dt.datetime.now().isoformat(timespec="seconds")
        for key in pruned:
            known.pop(key, None)
        previous = known.get(current_key) or {}
        known[current_key] = {
            "path": current_key,
            "kind": "explicit_file" if explicit else "default_root",
            "first_seen_at": str(previous.get("first_seen_at") or timestamp),
            "last_used_at": timestamp,
            "last_account": current_account,
            "last_origin": str(origin or ""),
        }
        _atomic_write(registry, {
            "version": _AUTHORITY_LOCATIONS_VERSION,
            "updated_at": timestamp,
            "locations": [known[key] for key in sorted(known)],
        })
        return {"conflicts": [], "pruned": pruned, "checked": checked,
                "registry": str(registry), "registered": True}
    finally:
        lock.release()


def describe_authority_location_conflicts(conflicts: list[dict[str, Any]],
                                          *, limit: int = 5) -> str:
    """把位置切换冲突整理成给用户看的短句（位置 + 记录 id）。"""
    parts: list[str] = []
    for item in conflicts[:max(1, int(limit))]:
        location = str(item.get("location") or "?")
        record_id = str(item.get("journal_id") or item.get("identifier") or "?")
        parts.append(f"{location}#{record_id}")
    text = "、".join(parts)
    if len(conflicts) > len(parts):
        text += f" 等共 {len(conflicts)} 条"
    return text


def legacy_uncertain_paths(config: Any = None,
                           authoritative: str | os.PathLike[str] | None = None
                           ) -> list[Path]:
    """只收集已知应用状态位置的旧 journal，不扫描整个用户目录。"""
    candidates: list[Path] = []
    explicit = getattr(config, "sss_uncertain_path", None) if config is not None else None
    if explicit:
        candidates.append(Path(explicit).expanduser())
    env_path = os.environ.get("YIKOU_SSS_UNCERTAIN_PATH", "").strip()
    if env_path:
        candidates.append(Path(env_path).expanduser())

    old_root = _deployment_root()
    old_default = old_root / "sss_uncertain.json"
    if old_default.exists():
        candidates.append(old_default)
    prior_global = old_root / "sss_uncertain_authoritative.json"
    if prior_global.exists():
        candidates.append(prior_global)
    # 旧规则（URL/账号/workbook 摘要）生成的哈希权威文件目录；只扫这个已知目录。
    prior_hash_dir = old_root / "sss_authoritative"
    if prior_hash_dir.is_dir():
        candidates.extend(sorted(path for path in prior_hash_dir.glob("*.json")
                                 if path.is_file()))

    auth_resolved = Path(authoritative).resolve() if authoritative else None
    result: list[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            resolved = candidate
        if auth_resolved is not None and resolved == auth_resolved:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        result.append(candidate)
    return result


def mirror_uncertain_paths(config: Any = None,
                           authoritative: str | os.PathLike[str] | None = None
                           ) -> list[Path]:
    """允许写回的兼容镜像目标：只包含显式配置为当前兼容路径的文件。

    旧哈希目录/旧默认文件/旧全局文件只作为只读迁移来源，绝不在这里返回，
    避免把当前账号权威状态覆盖到其他账号/平台的旧 journal。
    """
    candidates: list[Path] = []
    explicit = getattr(config, "sss_uncertain_path", None) if config is not None else None
    if explicit:
        candidates.append(Path(explicit).expanduser())
    env_path = os.environ.get("YIKOU_SSS_UNCERTAIN_PATH", "").strip()
    if env_path:
        candidates.append(Path(env_path).expanduser())
    auth_resolved = Path(authoritative).resolve() if authoritative else None
    result: list[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            resolved = candidate
        if auth_resolved is not None and resolved == auth_resolved:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        result.append(candidate)
    return result


def _record_identity_key(record: dict[str, Any]) -> tuple[str, str]:
    journal_id = str(record.get("journal_id") or record.get("identifier") or "")
    batch_key = str(record.get("batch_key") or "").strip()
    if not batch_key:
        batch_key = "|".join((str(record.get("delivery_date") or ""),
                              normalise_account(record.get("account"))))
    return journal_id, batch_key


def _record_platform(record: dict[str, Any]) -> str:
    for key in ("platform", "platform_origin", "origin"):
        value = record.get(key)
        if value not in (None, ""):
            return _normalise_origin(value)
    return ""


def merge_journals(authoritative: str | os.PathLike[str],
                   sources: list[str | os.PathLike[str]],
                   *, scope: dict[str, Any] | None = None) -> dict[str, Any]:
    """把旧/镜像 journal 中属于当前平台+账号的未决记录合并进权威文件。

    平台归属可判定才合并；账号不同/平台不同明确跳过；账号或平台无法判定时
    把记录保留进权威文件并加 ``scope_unknown`` 标记，然后 fail-closed，提示
    人工核对。不同账号/平台的记录不会被错误归并。
    """
    auth = Path(authoritative)
    source_paths = [Path(source) for source in sources]
    scope_origin = _normalise_origin((scope or {}).get("platform", ""))
    scope_account = normalise_account((scope or {}).get("account", ""))
    merged = 0
    quarantined: list[dict[str, Any]] = []
    with _journal_lock(auth):
        with _journal_file_lock(auth):
            try:
                payload = load_journal(auth)
            except UncertainJournalError as exc:
                raise UncertainJournalError(
                    f"权威共享 journal 不可读/损坏：{auth}（{exc}）。"
                    "已阻止提交；请人工核对后再运行，勿直接删除或并行重跑") from exc
            records = [record for record in payload.get("records", [])
                       if isinstance(record, dict)]
            if scope is not None:
                existing_unknown = [
                    record for record in records
                    if _is_active_status(record) and record.get("scope_unknown")
                ]
                if existing_unknown:
                    ids = "、".join(str(record.get("journal_id")
                                      or record.get("identifier") or "?")
                                  for record in existing_unknown)
                    raise UncertainJournalError(
                        f"权威 journal {auth} 中仍有平台归属未确认的旧记录：{ids}；"
                        "已阻止提交。请人工核对并整理/清除 scope_unknown 记录后再运行；"
                        "不要直接删除旧文件，也不要并行重跑") from None
            active_ids = {
                str(record.get("journal_id") or record.get("identifier") or "")
                for record in records if _is_active_status(record)
            }
            for source in source_paths:
                if source.resolve() == auth.resolve() or not source.exists():
                    continue
                try:
                    source_payload = load_journal(source)
                except UncertainJournalError as exc:
                    raise UncertainJournalError(
                        f"旧 journal 无法安全迁移：{source}（{exc}）。已阻止提交；"
                        f"请先人工核对旧文件中的 unresolved，再整理到 {auth}；"
                        "不要直接删除旧文件，也不要并行重跑") from exc
                for record in source_payload.get("records", []):
                    if not isinstance(record, dict) or not _is_active_status(record):
                        continue
                    journal_id = str(record.get("journal_id")
                                     or record.get("identifier") or "")
                    if journal_id and journal_id in active_ids:
                        continue
                    record_account = normalise_account(record.get("account"))
                    record_platform = _record_platform(record)
                    unknown_reason = ""
                    if scope is not None:
                        if record_platform and scope_origin \
                                and record_platform != scope_origin:
                            continue
                        if not record_account:
                            unknown_reason = "旧记录缺少账号信息，无法安全判定归属"
                        elif scope_account and record_account != scope_account:
                            continue
                        elif scope_origin and not record_platform:
                            unknown_reason = (
                                f"旧记录账号 {record_account} 与当前一致，"
                                "但缺少平台 origin，无法安全判定归属")
                    if unknown_reason:
                        record = dict(record)
                        record["scope_unknown"] = True
                        record["scope_unknown_reason"] = unknown_reason
                        records.append(record)
                        if journal_id:
                            active_ids.add(journal_id)
                        quarantined.append(record)
                        merged += 1
                        continue
                    records.append(record)
                    if journal_id:
                        active_ids.add(journal_id)
                    merged += 1
            if merged:
                payload["version"] = 1
                payload["records"] = records
                _atomic_write(auth, payload)
        if quarantined:
            ids = "、".join(str(record.get("journal_id")
                              or record.get("identifier") or "?")
                          for record in quarantined)
            raise UncertainJournalError(
                f"旧 journal 中有 {len(quarantined)} 条记录无法安全判定平台/账号"
                f"（{ids}）；已保守保留在权威 journal {auth} 并阻止本次提交。"
                "请人工核对这些记录属于哪个平台/账号，确认后整理或清理对应记录；"
                "不要直接删除旧文件，也不要并行重跑")
    return {"authoritative": str(auth), "merged": merged,
            "sources": [str(path) for path in source_paths]}


def mirror_journal(authoritative: str | os.PathLike[str],
                   mirror: str | os.PathLike[str]) -> int:
    """把权威 journal 合并进显式兼容镜像，保留目标里其他账号/平台记录。

    目标文件若损坏则 fail-closed，不覆盖；只有显式配置的镜像目标才会调用
    本函数，旧哈希目录中的其他账号文件保持字节不变。
    """
    auth = Path(authoritative)
    target = Path(mirror)
    if auth.resolve() == target.resolve():
        return 0
    with _journal_lock(target):
        with _journal_file_lock(target):
            authority_payload = load_journal(auth)
            if not target.exists():
                _atomic_write(target, authority_payload)
                return len(authority_payload.get("records", []))
            target_payload = load_journal(target)
            merged: list[dict[str, Any]] = []
            index: dict[tuple[str, str], int] = {}
            for record in target_payload.get("records", []):
                key = _record_identity_key(record)
                index[key] = len(merged)
                merged.append(record)
            for record in authority_payload.get("records", []):
                key = _record_identity_key(record)
                if key in index:
                    merged[index[key]] = record
                else:
                    index[key] = len(merged)
                    merged.append(record)
            target_payload["version"] = 1
            target_payload["records"] = merged
            _atomic_write(target, target_payload)
            return len(merged)


def _journal_id(entry: dict[str, Any]) -> str:
    explicit = str(entry.get("client_request_id") or "").strip()
    if explicit:
        return explicit
    identifier = str(entry.get("identifier") or "").strip()
    if identifier:
        return identifier
    return uuid.uuid4().hex


def _fsync_parent_dir(path: Path) -> None:
    """fsync 目标文件父目录，确保 ``os.replace`` 目录项在断电后仍可见。

    支持范围：Android / Termux / Linux 都能把目录当文件打开并 fsync，因此这里
    始终做目录 fsync；失败时由调用方按写入失败处理。
    """
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    fd = os.open(str(path.parent), flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    """原子写 journal 文件 + 文件 fsync + replace + 父目录 fsync。

    任一步失败都抛出 ``UncertainJournalError``，调用方必须停止后续 POST。
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise UncertainJournalError(
            f"无法创建不确定记录目录 {path.parent}（{exc}）；已阻止提交，"
            "请检查目录权限/磁盘空间后重试") from exc
    try:
        handle, temp_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    except PermissionError as exc:
        raise UncertainJournalError(
            f"目录不可写，无法创建不确定记录临时文件：{path.parent}（{exc}）；"
            "已阻止提交，请检查目录权限或关闭占用后重试") from exc
    except OSError as exc:
        raise UncertainJournalError(
            f"无法创建不确定记录临时文件：{path.parent}（{exc}）；"
            "已阻止提交，请检查磁盘空间/目录权限后重试") from exc
    temp_path = Path(temp_name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, path)
        # 文件内容已 fsync，再 fsync 父目录确保目录项也持久化。
        try:
            _fsync_parent_dir(path)
        except OSError as exc:
            raise UncertainJournalError(
                f"不确定记录父目录 fsync 失败：{path.parent}（{exc}）；"
                "无法确认持久化，已阻止提交，请检查文件系统/权限后重试") from exc
    except UncertainJournalError:
        temp_path.unlink(missing_ok=True)
        raise
    except OSError as exc:
        temp_path.unlink(missing_ok=True)
        raise UncertainJournalError(
            f"无法原子写入不确定记录 {path}（{exc}）；已阻止提交，"
            "请检查目录权限/磁盘空间后重试") from exc
    finally:
        temp_path.unlink(missing_ok=True)


_SUPPORTED_JOURNAL_VERSIONS = frozenset({1})


def _validate_journal_payload(payload: Any, target: Path) -> dict[str, Any]:
    """校验 journal 顶层与每条记录；任何异常都 fail-closed。

    版本：缺省视为 1（旧文件兼容）；只支持 {1}，未知版本拒绝。
    记录：必须是 dict，且有 journal_id/identifier、batch_key 或
    delivery_date+account、fingerprint dict；status 如存在必须是字符串。
    合法空 records 文件仍可用。
    """
    if not isinstance(payload, dict):
        raise UncertainJournalError(f"本地不确定记录结构异常（根节点必须为对象）：{target}")
    version = payload.get("version", 1)
    if isinstance(version, bool) or not isinstance(version, int) \
            or version not in _SUPPORTED_JOURNAL_VERSIONS:
        raise UncertainJournalError(
            f"本地不确定记录版本不受支持：{target}（version={version!r}）；"
            "已拒绝读取，请人工确认后再运行")
    records = payload.get("records")
    if not isinstance(records, list):
        raise UncertainJournalError(
            f"本地不确定记录缺少 records 列表：{target}；已拒绝读取")
    for index, record in enumerate(records):
        where = f"{target} 第 {index + 1} 条记录"
        if not isinstance(record, dict):
            raise UncertainJournalError(f"{where}不是对象；已拒绝读取，不能当空 journal 放行")
        journal_id = record.get("journal_id") or record.get("identifier")
        if not isinstance(journal_id, str) or not journal_id.strip():
            raise UncertainJournalError(f"{where}缺少 journal_id/identifier；已拒绝读取")
        batch_key = record.get("batch_key")
        has_batch = isinstance(batch_key, str) and bool(batch_key.strip())
        has_date_account = bool(str(record.get("delivery_date") or "").strip()) and \
            bool(str(record.get("account") or "").strip())
        if not has_batch and not has_date_account:
            raise UncertainJournalError(
                f"{where}缺少 batch_key 或 delivery_date+account；已拒绝读取")
        if not isinstance(record.get("fingerprint"), dict):
            raise UncertainJournalError(f"{where}缺少可用的 fingerprint 对象；已拒绝读取")
        status = record.get("status")
        if status is not None and not isinstance(status, str):
            raise UncertainJournalError(f"{where}status 不是字符串；已拒绝读取")
    payload["version"] = version
    return payload


def load_journal(path: str | os.PathLike[str]) -> dict[str, Any]:
    """读取日记；文件不存在返回空结构，内容/记录异常一律 fail-closed。"""
    target = Path(path)
    if not target.exists():
        return {"version": 1, "records": []}
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UncertainJournalError(f"本地不确定记录损坏，拒绝继续下单：{target}（{exc}）") from exc
    return _validate_journal_payload(payload, target)


def pending_records(records: list[dict[str, Any]], key: str | None = None) -> list[dict[str, Any]]:
    """筛选 Still active 记录；``key`` 非空时用兼容匹配（忽略 source 变化）。"""
    pending: list[dict[str, Any]] = []
    for record in records:
        if not isinstance(record, dict) or not _is_active_status(record):
            continue
        if key is not None and not _record_matches_batch(record, key):
            continue
        pending.append(record)
    return pending


def append_uncertain_records(path: str | os.PathLike[str],
                             key: str,
                             entries: list[dict[str, Any]],
                             *,
                             meta: dict[str, Any] | None = None,
                             now: _dt.datetime | None = None) -> int:
    """把本轮“已发送未知”任务写入本地记录；失败必须由调用方停止 POST。"""
    target = Path(path)
    with _journal_lock(target):
        with _journal_file_lock(target):
            payload = load_journal(target)
            meta = dict(meta or {})
            timestamp = (now or _dt.datetime.now()).isoformat(timespec="seconds")
            deduped: dict[str, dict[str, Any]] = {}
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                record = {
                    "journal_id": _journal_id(entry),
                    "identifier": str(entry.get("identifier") or ""),
                    "client_request_id": str(entry.get("client_request_id") or ""),
                    "sheet": str(entry.get("sheet") or ""),
                    "batch_id": str(entry.get("batch_id") or ""),
                    "batch_key": key,
                    "account": normalise_account(meta.get("account") or entry.get("account") or ""),
                    "platform": _normalise_origin(meta.get("platform")
                                                 or entry.get("platform") or ""),
                    "delivery_date": str(meta.get("delivery_date") or ""),
                    "source": str(meta.get("source") or ""),
                    "fingerprint": dict(entry.get("fingerprint") or {}),
                    "error": str(entry.get("error") or ""),
                    # POST 前置记录使用 inflight，响应不确定后更新为 unresolved。
                    "status": str(entry.get("status") or "unresolved"),
                    "created_at": timestamp,
                    "batch_started_at": meta.get("batch_started_at"),
                }
                # 本次调用内同 journal_id 只留最后一条，避免重复 entry 落两条。
                deduped[record["journal_id"]] = record
            new_records = list(deduped.values())
            # 只替换待写 entry 同 journal_id 的旧 active 记录；已经存在的其他
            # 不确定任务（例如另一个 round/另一个 worker）必须保留，绝不能被
            # 本次追加覆盖掉，否则跨运行阻断会漏单。旧三段 batch_key 也通过
            # _record_matches_batch 兼容替换。
            new_ids = {record["journal_id"] for record in new_records}
            kept: list[dict[str, Any]] = []
            for record in payload.get("records", []):
                if not isinstance(record, dict):
                    continue
                if (_record_matches_batch(record, key)
                        and _is_active_status(record)
                        and str(record.get("journal_id") or "") in new_ids):
                    continue
                kept.append(record)
            payload["version"] = 1
            payload["records"] = kept + new_records
            _atomic_write(target, payload)
            return len(new_records)


def resolve_uncertain_records(path: str | os.PathLike[str], key: str,
                              identifiers: set[str] | list[str] | tuple[str, ...],
                              *, note: str = "", actor: str = "") -> int:
    """只读对账确认后，把对应记录标记为 resolved（保留审计痕迹）。

    ``note``/``actor`` 供**人工确认入口**填写：审计必须留下“谁、凭什么”把这条
    未决记录判成已确认，事后才能追溯。自动对账路径不传，行为与以前完全一致。
    """
    target = Path(path)
    with _journal_lock(target):
        with _journal_file_lock(target):
            payload = load_journal(target)
            wanted = {str(identifier) for identifier in identifiers}
            resolved = 0
            timestamp = _dt.datetime.now().isoformat(timespec="seconds")
            for record in payload.get("records", []):
                if not isinstance(record, dict):
                    continue
                if not _record_matches_batch(record, key):
                    continue
                if not _is_active_status(record):
                    continue
                record_id = str(record.get("journal_id") or "")
                task_id = str(record.get("identifier") or "")
                if record_id in wanted or task_id in wanted:
                    record["status"] = "resolved"
                    record["resolved_at"] = timestamp
                    record["resolved_reason"] = (str(note or "").strip()
                                                 or "站内只读对账确认")
                    if actor:
                        record["resolved_by"] = str(actor)
                    resolved += 1
            if resolved:
                _atomic_write(target, payload)
            return resolved


def discard_uncertain_records(path: str | os.PathLike[str], key: str,
                              identifiers: set[str] | list[str] | tuple[str, ...],
                              reason: str = "明确未发送/明确失败，有充分证据无需重试",
                              *, note: str = "", actor: str = "") -> int:
    """有充分证据证明 POST 未落单时关闭记录，避免误阻断后续运行。

    仅用于明确 401/余额不足/显式 success=false/从未派发等“未发送”证据。
    resolved 只用于站内只读对账确认；discarded 记录保留审计但不参与阻断。
    """
    target = Path(path)
    with _journal_lock(target):
        with _journal_file_lock(target):
            payload = load_journal(target)
            wanted = {str(identifier) for identifier in identifiers}
            discarded = 0
            timestamp = _dt.datetime.now().isoformat(timespec="seconds")
            for record in payload.get("records", []):
                if not isinstance(record, dict):
                    continue
                if not _record_matches_batch(record, key):
                    continue
                if not _is_active_status(record):
                    continue
                record_id = str(record.get("journal_id") or "")
                task_id = str(record.get("identifier") or "")
                if record_id in wanted or task_id in wanted:
                    record["status"] = "discarded"
                    record["discarded_at"] = timestamp
                    record["discarded_reason"] = reason
                    if note:
                        # 人工核对说明单独留痕：reason 是机器可读的分类，note 是
                        # “谁凭什么敢判它没落单”，事后追溯只看 note。
                        record["discarded_note"] = str(note)
                    if actor:
                        record["discarded_by"] = str(actor)
                    discarded += 1
            if discarded:
                _atomic_write(target, payload)
            return discarded


_MASK_PHONE_RE = re.compile(r"^(\d{3})\d{4}(\d{4})$")


def mask_contact(value: Any) -> str:
    """手机号/账号脱敏：只保留前 3 后 4，非 11 位数字按首尾保留。

    未决记录里带着客户姓名与电话。接口层只给管理员看，也不应把整份客户名单
    原样吐进前端/日志（与 WPS 恢复入口“不含客户/路径”的既有口径一致）。
    """
    text = normalise_account(value)
    if not text:
        return ""
    match = _MASK_PHONE_RE.match(text)
    if match:
        return f"{match.group(1)}****{match.group(2)}"
    if len(text) <= 4:
        return "*" * len(text)
    return f"{text[:2]}{'*' * max(0, len(text) - 6)}{text[-4:]}"


def pending_record_views(path: str | os.PathLike[str], key: str | None = None
                         ) -> dict[str, Any]:
    """只读列出未决记录（脱敏投影），供人工核对；**绝不改文件**。

    ``key`` 非空时只返回该批次键下的活跃记录（只有它们会阻断本次运行），
    ``counts`` 里的 active/inflight/unresolved 也按该批次口径统计，
    resolved/discarded 则是整个 journal 的审计计数。读失败一律抛
    ``UncertainJournalError``：调用方绝不能把它当成“没有未决记录”。
    """
    target = Path(path)
    payload = load_journal(target)
    records = [record for record in payload.get("records", [])
               if isinstance(record, dict)]
    counts = {"active": 0, "inflight": 0, "unresolved": 0,
              "resolved": 0, "discarded": 0}
    views: list[dict[str, Any]] = []
    for record in records:
        status = str(record.get("status") or "unresolved")
        if status == "resolved":
            counts["resolved"] += 1
        elif status == "discarded":
            counts["discarded"] += 1
        if not _is_active_status(record):
            continue
        if key is not None and not _record_matches_batch(record, key):
            continue
        if status == "inflight":
            counts["inflight"] += 1
        else:
            counts["unresolved"] += 1
        counts["active"] += 1
        fingerprint = record.get("fingerprint") if isinstance(
            record.get("fingerprint"), dict) else {}
        views.append({
            "journal_id": str(record.get("journal_id") or ""),
            "identifier": str(record.get("identifier") or ""),
            "sheet": str(record.get("sheet") or ""),
            "batch_id": str(record.get("batch_id") or ""),
            "delivery_date": str(record.get("delivery_date") or ""),
            "status": status,
            "error": str(record.get("error") or ""),
            "created_at": str(record.get("created_at") or ""),
            "batch_started_at": record.get("batch_started_at"),
            "name": str(fingerprint.get("receive_name") or ""),
            "phone": mask_contact(fingerprint.get("receive_phone")),
            "delivery_time": str(fingerprint.get("expected_delivery_time") or ""),
            "door_num": str(fingerprint.get("door_num") or ""),
            "account": mask_contact(record.get("account")),
            "reason": str(record.get("scope_unknown_reason") or ""),
        })
    return {"records": views, "counts": counts, "path": str(target)}


def journal_fingerprint(path: str | os.PathLike[str]) -> str:
    """当前 journal 内容的稳定指纹（只读），用作核对快照与写入门禁的 CAS 锚点。

    只对 records 做规范化 JSON 哈希：任何记录的新增/状态变化都会改变指纹，
    因此“先只读核对、后解除”之间文件被改过就一定对不上，必须重新核对。
    """
    payload = load_journal(Path(path))
    records = [record for record in payload.get("records", [])
               if isinstance(record, dict)]
    blob = json.dumps(records, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def journal_tasks(records: list[dict[str, Any]]
                  ) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """把未决记录还原成对账用的 tasks 与 ``{journal_id: record}`` 映射。

    抽出来是为了让“正式运行的只读对账”和“管理员手动只读核对”用同一套还原
    规则，避免两条路径各自解释 fingerprint 而出现结论差异。
    """
    tasks: list[dict[str, Any]] = []
    by_id: dict[str, dict[str, Any]] = {}
    for record in records:
        journal_id = str(record.get("journal_id")
                         or record.get("identifier") or "").strip()
        if not journal_id:
            journal_id = uuid.uuid4().hex
        fingerprint_data = record.get("fingerprint") if isinstance(
            record.get("fingerprint"), dict) else {}
        kwargs = {field: str(fingerprint_data.get(field) or "")
                  for field in OrderFingerprint._fields}
        tasks.append({
            "identifier": journal_id,
            "payload": {},
            "fingerprint": OrderFingerprint(**kwargs),
            "account": normalise_account(record.get("account")),
        })
        by_id[journal_id] = record
    return tasks, by_id


def journal_created_after(records: list[dict[str, Any]]) -> float | None:
    """本批最早开始时间减去时钟偏移；没有可解析时间时返回 None（不按时间过滤）。"""
    started: list[float] = []
    for record in records:
        try:
            if record.get("batch_started_at") not in (None, ""):
                started.append(float(record["batch_started_at"]))
        except (TypeError, ValueError):
            continue
    return (min(started) - _BATCH_CLOCK_SKEW_S) if started else None


def resolve_pending_records(path: str | os.PathLike[str], key: str,
                            fetch_json: Callable[[str], dict[str, Any]],
                            callback: Callable[[str], Any] | None = None,
                            *, attempts: int = _RECONCILE_POLL_ATTEMPTS,
                            ) -> tuple[list[dict[str, Any]], _Reconciliation | None, int]:
    """对当前批次键的 unresolved 记录做只读对账；返回剩余记录/对账结果/已解决数。"""
    target = Path(path)
    payload = load_journal(target)
    records = pending_records(payload.get("records", []), key)
    if not records:
        return [], None, 0

    # 还原规则与“管理员手动只读核对”共用同一份实现（journal_tasks /
    # journal_created_after），避免两条路径对同一批记录给出不同结论。
    tasks, by_id = journal_tasks(records)
    created_after = journal_created_after(records)
    try:
        reconciliation = _safe_reconcile(
            tasks, fetch_json, callback, "跨运行不确定记录只读对账",
            created_after=created_after, attempts=max(1, int(attempts)),
            zero_retry_delay=_PREFILTER_ZERO_RETRY_DELAY_S)
    except Exception as exc:
        _emit(callback, f"不确定记录只读对账失败：{exc}；本批拒绝自动提交")
        return records, None, 0
    if reconciliation is None:
        return records, None, 0

    confirmed = {journal_id for journal_id in by_id if journal_id in reconciliation.confirmed}
    remaining = [record for journal_id, record in by_id.items() if journal_id not in confirmed]
    resolved = 0
    if confirmed:
        resolved = resolve_uncertain_records(target, key, confirmed)
        if resolved != len(confirmed):
            # 清理失败说明记录状态不可信，宁可 block 也不能继续提交。
            _emit(callback, "不确定记录已确认但清理失败，本批拒绝自动提交")
            return records, reconciliation, 0
    return remaining, reconciliation, resolved


__all__ = [
    "UncertainJournalError",
    "append_uncertain_records",
    "authoritative_uncertain_path",
    "authority_location_gate",
    "authority_locations_path",
    "authority_scan_root",
    "authority_scope_key",
    "batch_key",
    "batch_submission_lock",
    "cross_scope_unresolved_records",
    "default_uncertain_path",
    "describe_authority_location_conflicts",
    "journal_created_after",
    "journal_fingerprint",
    "journal_tasks",
    "describe_cross_scope_conflicts",
    "legacy_uncertain_paths",
    "load_authority_locations",
    "mask_contact",
    "merge_journals",
    "mirror_journal",
    "mirror_uncertain_paths",
    "discard_uncertain_records",
    "legacy_batch_key",
    "normalise_account",
    "platform_origin",
    "load_journal",
    "pending_record_views",
    "pending_records",
    "resolve_pending_records",
    "resolve_uncertain_records",
]
