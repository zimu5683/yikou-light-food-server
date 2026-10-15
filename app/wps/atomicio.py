"""原子写盘与跨进程文件锁（WPS journal/ledger 共用的小基础设施）。

设计取舍：

* 写临时文件 → `fsync` 文件 → `os.replace` → 尽力 `fsync` 父目录；
* 用 ``fcntl.lockf``（进程级记录锁）：本项目只跑 Android / Termux / Linux，
  这些平台都提供 fcntl。没有可用跨进程锁原语时明确抛错，不做“假装上锁”的降级；
* 锁文件与数据文件分离，数据锁路径由 :func:`lock_path_for` 统一计算，
  journal 与 ledger 对同一账本映射到同一个锁文件。
"""
from __future__ import annotations

import errno
import hashlib
import os
import re
import shutil
import tempfile
import threading
import time
from pathlib import Path

try:  # POSIX：Android / Termux / Linux 都提供
    import fcntl
except ImportError:  # pragma: no cover - 不支持的非 POSIX 平台
    fcntl = None  # type: ignore[assignment]


class LockTimeout(TimeoutError):
    """在给定超时时间内没有取得跨进程锁。"""


class AtomicWriteError(OSError):
    """原子写盘失败（调用方必须失败关闭）。"""

#: 写盘前保留几代 ``.bak``（``.bak`` = 上一版，``.bak.1`` = 再上一版）。
BACKUP_GENERATIONS = 3
#: 每日快照 ``<文件名>.<YYYY-MM-DD>.bak`` 保留天数。
BACKUP_KEEP_DAYS = 7
#: 损坏原件 / 重建前原件的留档份数。
PRESERVE_KEEP = 5


_LOCK_STATE = threading.Lock()
_HELD_LOCKS: dict[tuple[int, str, int], int] = {}
_THREAD_LOCKS: dict[str, threading.Lock] = {}


def _thread_lock_for(path: Path) -> threading.Lock:
    with _LOCK_STATE:
        return _THREAD_LOCKS.setdefault(str(path), threading.Lock())


def lock_path_for(write_path: str | os.PathLike[str]) -> Path:
    """同一账本的 ledger/journal 共用一把数据锁。

    ``state.json`` 与 ``state.json.journal`` 都映射到 ``state.json.lock``。
    """
    path = Path(write_path)
    name = str(path)
    if name.endswith(".journal"):
        name = name[: -len(".journal")]
    return Path(name + ".lock")


def operation_lock_path_for(ledger_path: str | os.PathLike[str]) -> Path:
    """整次 apply_plan 操作的跨进程锁；与逐文件数据锁分离，避免嵌套死锁。"""
    return Path(str(ledger_path) + ".oplock")


def _fsync_parent_dir(parent: Path) -> None:
    """尽力 fsync 父目录；不支持目录 fsync 的平台直接返回。

    目录可打开但 fsync 失败时（例如真实 I/O 错误）向上抛，让调用方失败关闭。
    """
    try:
        fd = os.open(str(parent), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError as exc:
        if exc.errno in (errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP,
                         errno.EBADF):
            return
        raise
    finally:
        os.close(fd)


_DAILY_BACKUP_RE = re.compile(r"^(?P<base>.+)\.(?P<day>\d{4}-\d{2}-\d{2})\.bak$")


def _backup_name(path: Path, index: int) -> Path:
    suffix = "" if index <= 0 else f".{index}"
    return path.with_name(f"{path.name}.bak{suffix}")


def rotate_backups(path: str | os.PathLike[str], *,
                   generations: int = BACKUP_GENERATIONS,
                   keep_days: int = BACKUP_KEEP_DAYS,
                   today: str = "") -> list[Path]:
    """写盘前**尽力**留档：多代 ``.bak`` + 每日快照；任何失败都不抛。

    仿 ``AppConfig.save`` 的 ``.bak`` 先例：备份是加固，绝不能反过来挡住账本写入，
    所以这里所有 ``OSError`` 一律吞掉（写盘本身的失败仍然由调用方失败关闭）。
    调用方**必须**已持有该文件的数据锁（:func:`lock_path_for`），否则并发写会让
    ``.bak`` 指向半新半旧的内容。

    留档两件事：

    * 多代 ``.bak``：``.bak`` 是上一版，``.bak.1`` 再上一版，最多 ``generations`` 代；
    * 每日快照 ``<文件名>.<YYYY-MM-DD>.bak``：当天**第一次**写盘前留一份，等于
      "昨天结束时的状态"，跨批次的回退点；只保留最近 ``keep_days`` 天。

    返回本次实际写出的备份路径（可能为空 = 尽力而为地没做成）。
    """
    target = Path(path)
    written: list[Path] = []
    try:
        if not target.is_file():
            return written
    except OSError:
        return written

    gens = max(1, int(generations))
    try:
        for index in range(gens - 1, 0, -1):
            older = _backup_name(target, index - 1)
            newer = _backup_name(target, index)
            if older.exists():
                os.replace(older, newer)
        shutil.copy2(target, _backup_name(target, 0))
        written.append(_backup_name(target, 0))
    except OSError:
        pass

    if keep_days > 0:
        day = today or time.strftime("%Y-%m-%d")
        snapshot = target.with_name(f"{target.name}.{day}.bak")
        try:
            if not snapshot.exists():
                shutil.copy2(target, snapshot)
                written.append(snapshot)
        except OSError:
            pass
        try:
            _prune_daily_backups(target, keep=max(1, int(keep_days)))
        except OSError:
            pass
    return written


def _prune_daily_backups(target: Path, *, keep: int) -> None:
    """只保留最近 ``keep`` 天的每日快照（按文件名里的日期排序）。"""
    days: list[tuple[str, Path]] = []
    for candidate in target.parent.glob(f"{target.name}.*.bak"):
        match = _DAILY_BACKUP_RE.match(candidate.name)
        if match and match.group("base") == target.name:
            days.append((match.group("day"), candidate))
    days.sort()
    for _day, candidate in days[:-keep]:
        try:
            candidate.unlink()
        except OSError:
            pass


def preserve_file(path: str | os.PathLike[str], *, tag: str,
                  keep: int = PRESERVE_KEEP) -> Path | None:
    """把当前文件另存为 ``<文件名>.<tag>-<内容摘要>``，返回留档路径。

    用于「文件已经坏了/马上要被重建覆盖，先别丢证据」：内容相同的重复留档按摘要
    去重（读盘失败时退回时间戳命名），只保留最近 ``keep`` 份。任何失败都返回
    ``None``，绝不抛 —— 留档失败不能把失败关闭变成崩溃。
    """
    target = Path(path)
    try:
        if not target.is_file():
            return None
        raw = target.read_bytes()
    except OSError:
        return None
    name = str(tag or "preserved").strip() or "preserved"
    try:
        suffix = hashlib.sha256(raw).hexdigest()[:12]
    except Exception:  # pragma: no cover - hashlib 不会失败，留个兜底
        suffix = str(int(time.time()))
    dest = target.with_name(f"{target.name}.{name}-{suffix}")
    try:
        if not dest.exists():
            shutil.copy2(target, dest)
    except OSError:
        return None
    try:
        _prune_preserved(target, name, keep=max(1, int(keep)))
    except OSError:
        pass
    return dest


def _prune_preserved(target: Path, tag: str, *, keep: int) -> None:
    """同名 tag 的留档只保留最近 ``keep`` 份（按 mtime，新的留下）。"""
    items: list[tuple[float, Path]] = []
    for candidate in target.parent.glob(f"{target.name}.{tag}-*"):
        try:
            items.append((candidate.stat().st_mtime, candidate))
        except OSError:
            continue
    items.sort()
    for _stamp, candidate in items[:-keep]:
        try:
            candidate.unlink()
        except OSError:
            pass


def atomic_write_text(path: str | os.PathLike[str], text: str, *,
                      encoding: str = "utf-8") -> Path:
    """临时文件 + fsync + 原子替换 + 父目录 fsync；任何一步失败都不留下半文件。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp",
                                    dir=str(target.parent))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding=encoding) as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
        _fsync_parent_dir(target.parent)
    except BaseException:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return target


class FileLock:
    """轻量跨进程锁。

    同一进程内嵌套获取同一路径时按重入计数直接返回（``lockf`` 本身允许进程
    重入，但按次数配对释放更稳），跨进程则通过 OS 文件锁互斥。
    """

    def __init__(self, path: str | os.PathLike[str], *,
                 shared: bool = False, timeout: float = 30.0,
                 poll: float = 0.05) -> None:
        self.path = Path(path)
        self.shared = bool(shared)
        self.timeout = max(0.0, float(timeout))
        self.poll = max(0.001, float(poll))
        self._fd: int | None = None
        self._key: tuple[int, str, int] | None = None
        self._thread_lock: threading.Lock | None = None

    def _open(self) -> int:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        return os.open(str(self.path), os.O_RDWR | os.O_CREAT, 0o600)

    def _try_lock(self, fd: int) -> bool:
        if fcntl is None:  # pragma: no cover - 只跑 Android/Termux/Linux，fcntl 必然可用
            raise RuntimeError("当前平台没有可用的跨进程文件锁原语")
        flags = fcntl.LOCK_SH if self.shared else fcntl.LOCK_EX
        try:
            fcntl.lockf(fd, flags | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                return False
            raise
        return True

    def _unlock(self, fd: int) -> None:
        if fcntl is None:  # pragma: no cover - 见 _try_lock
            return
        try:
            fcntl.lockf(fd, fcntl.LOCK_UN)
        except OSError:
            pass

    def acquire(self) -> "FileLock":
        key = (os.getpid(), str(self.path), threading.get_ident())
        with _LOCK_STATE:
            held = _HELD_LOCKS.get(key)
            if held:
                _HELD_LOCKS[key] = held + 1
                self._key = key
                return self
        # 先拿进程内线程锁，再拿 OS 跨进程锁；两层都释放才真正解锁。
        thread_lock = _thread_lock_for(self.path)
        deadline = time.monotonic() + self.timeout
        while True:
            remaining = max(0.0, deadline - time.monotonic())
            if not thread_lock.acquire(timeout=remaining):
                raise LockTimeout(f"等待跨进程锁超时：{self.path}")
            fd: int | None = None
            try:
                fd = self._open()
                locked = self._try_lock(fd)
            except OSError as exc:
                try:
                    if fd is not None:
                        os.close(fd)
                finally:
                    thread_lock.release()
                raise AtomicWriteError(
                    f"无法获取跨进程锁 {self.path}：{exc}") from exc
            if locked:
                with _LOCK_STATE:
                    _HELD_LOCKS[key] = 1
                self._fd = fd
                self._thread_lock = thread_lock
                self._key = key
                return self
            try:
                os.close(fd)
            finally:
                thread_lock.release()
            if time.monotonic() >= deadline:
                raise LockTimeout(f"等待跨进程锁超时：{self.path}")
            time.sleep(self.poll)

    def release(self) -> None:
        key = self._key
        if key is None:
            return
        with _LOCK_STATE:
            held = _HELD_LOCKS.get(key, 0)
            if held > 1:
                _HELD_LOCKS[key] = held - 1
                return
            _HELD_LOCKS.pop(key, None)
        try:
            if self._fd is not None:
                try:
                    self._unlock(self._fd)
                finally:
                    os.close(self._fd)
                    self._fd = None
        finally:
            if self._thread_lock is not None:
                self._thread_lock.release()
                self._thread_lock = None
            self._key = None

    def __enter__(self) -> "FileLock":
        return self.acquire()

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()


__all__ = [
    "AtomicWriteError",
    "BACKUP_GENERATIONS",
    "BACKUP_KEEP_DAYS",
    "FileLock",
    "LockTimeout",
    "PRESERVE_KEEP",
    "atomic_write_text",
    "lock_path_for",
    "operation_lock_path_for",
    "preserve_file",
    "rotate_backups",
]
