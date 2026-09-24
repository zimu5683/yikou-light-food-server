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
import os
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
    "FileLock",
    "LockTimeout",
    "atomic_write_text",
    "lock_path_for",
    "operation_lock_path_for",
]
