"""A cross-process advisory file lock.

stepback mutating operations (checkpoint, rewind, redo) take an exclusive lock
on a single file inside the repo's metadata directory so two processes (for
example a running watcher and a manual ``stepback rewind`` in another terminal)
can never write the state file or the working tree at the same time.

The lock is advisory and owned by the operating system: on POSIX it uses
``fcntl.flock`` and on Windows it uses ``LockFileEx``.  Both primitives release
the lock when the owning process exits; the lock file itself is intentionally
never removed as a stale-lock heuristic because clock age is not ownership.
"""

from __future__ import annotations

import errno
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

try:  # POSIX
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]


_Overlapped: Any

if os.name == "nt":  # pragma: no cover - exercised by Windows CI
    import msvcrt
    from ctypes import POINTER, Structure, WinDLL, byref, c_size_t, get_last_error, wintypes

    class _WindowsOverlapped(Structure):
        _fields_ = [
            ("Internal", c_size_t),
            ("InternalHigh", c_size_t),
            ("Offset", wintypes.DWORD),
            ("OffsetHigh", wintypes.DWORD),
            ("hEvent", wintypes.HANDLE),
        ]

    _kernel32 = WinDLL("kernel32", use_last_error=True)
    _lock_file_ex = _kernel32.LockFileEx
    _lock_file_ex.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        POINTER(_WindowsOverlapped),
    ]
    _lock_file_ex.restype = wintypes.BOOL
    _unlock_file_ex = _kernel32.UnlockFileEx
    _unlock_file_ex.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        POINTER(_WindowsOverlapped),
    ]
    _unlock_file_ex.restype = wintypes.BOOL
    _ERROR_LOCK_VIOLATION = 33
    _LOCKFILE_EXCLUSIVE_LOCK = 0x00000002
    _LOCKFILE_FAIL_IMMEDIATELY = 0x00000001
    _Overlapped = _WindowsOverlapped
else:
    _Overlapped = None


class LockTimeout(RuntimeError):
    """Raised when the lock could not be acquired within the timeout."""


def _windows_lock(fd: int, timeout: float, poll: float, path: Path) -> None:
    """Acquire byte zero with a kernel-owned Windows byte-range lock."""
    # LockFileEx requires the range to exist on some Windows filesystem
    # implementations.  The one-byte marker is metadata, never a lease.
    if os.fstat(fd).st_size == 0:
        os.write(fd, b"\0")
    os.lseek(fd, 0, os.SEEK_SET)
    handle = wintypes.HANDLE(msvcrt.get_osfhandle(fd))
    deadline = time.monotonic() + timeout
    overlapped = _Overlapped()
    while True:
        if _lock_file_ex(
            handle,
            _LOCKFILE_EXCLUSIVE_LOCK | _LOCKFILE_FAIL_IMMEDIATELY,
            0,
            1,
            0,
            byref(overlapped),
        ):
            return
        if get_last_error() != _ERROR_LOCK_VIOLATION:
            raise OSError(get_last_error(), f"could not acquire lock {path}")
        if time.monotonic() >= deadline:
            raise LockTimeout(f"could not acquire lock {path} within {timeout}s")
        time.sleep(poll)


def _windows_unlock(fd: int) -> None:
    handle = wintypes.HANDLE(msvcrt.get_osfhandle(fd))
    overlapped = _Overlapped()
    if not _unlock_file_ex(handle, 0, 1, 0, byref(overlapped)):
        raise OSError(get_last_error(), "could not release Windows lock")


@contextmanager
def file_lock(
    path: Path, timeout: float = 30.0, poll: float = 0.05
) -> Iterator[None]:
    """Acquire an exclusive advisory lock on ``path`` for the duration of the block.

    Args:
        path: lock file (created if absent).
        timeout: seconds to wait before raising :class:`LockTimeout`.
        poll: retry interval while waiting.
    """
    path.parent.mkdir(parents=True, exist_ok=True)

    if fcntl is not None:
        fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o644)
        try:
            deadline = time.monotonic() + timeout
            while True:
                try:
                    fcntl.flock(  # type: ignore[attr-defined]
                        fd, fcntl.LOCK_EX | fcntl.LOCK_NB  # type: ignore[attr-defined]
                    )
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise LockTimeout(
                            f"could not acquire lock {path} within {timeout}s"
                        ) from None
                    time.sleep(poll)
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)  # type: ignore[attr-defined]
            finally:
                os.close(fd)
        return

    if os.name == "nt":
        # Windows has no fcntl module; use the kernel primitive instead of a
        # timestamped O_EXCL lease.  The handle must remain open for ownership.
        fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o644)
        acquired = False
        try:
            _windows_lock(fd, timeout, poll, path)
            acquired = True
            yield
        finally:
            if acquired:
                _windows_unlock(fd)
            os.close(fd)
        return

    # Last-resort compatibility path for unusual platforms.  It deliberately
    # has no stale-age deletion: a lock's age cannot prove that its owner died.
    # Supported POSIX and Windows hosts take the OS-owned paths above.
    deadline = time.monotonic() + timeout
    while True:
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o644)
            os.close(fd)
            break
        except OSError as exc:
            if exc.errno != errno.EEXIST:
                raise
            if time.monotonic() >= deadline:
                raise LockTimeout(
                    f"could not acquire lock {path} within {timeout}s"
                ) from None
            time.sleep(poll)
    try:
        yield
    finally:
        path.unlink(missing_ok=True)
