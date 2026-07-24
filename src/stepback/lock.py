"""A cross-process advisory file lock.

stepback mutating operations (checkpoint, rewind, redo) take an exclusive lock
on a single file inside the repo's metadata directory so two processes (for
example a running watcher and a manual ``stepback rewind`` in another terminal)
can never write the state file or the working tree at the same time.

The lock is advisory and best-effort: on POSIX it uses ``fcntl.flock``; on
platforms without ``fcntl`` it degrades to an ``O_CREAT | O_EXCL`` lock file with
a stale-lock timeout so a crashed holder cannot wedge the tool forever.
"""

from __future__ import annotations

import errno
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

try:  # POSIX
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]


class LockTimeout(RuntimeError):
    """Raised when the lock could not be acquired within the timeout."""


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
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
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
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)
        return

    # Fallback: exclusive-create lock file, with a stale-lock escape hatch.
    deadline = time.monotonic() + timeout
    stale_after = max(timeout, 60.0)
    while True:
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o644)
            os.close(fd)
            break
        except OSError as exc:
            if exc.errno != errno.EEXIST:
                raise
            try:
                age = time.time() - path.stat().st_mtime
                if age > stale_after:
                    path.unlink(missing_ok=True)
                    continue
            except OSError:
                pass
            if time.monotonic() >= deadline:
                raise LockTimeout(
                    f"could not acquire lock {path} within {timeout}s"
                ) from None
            time.sleep(poll)
    try:
        yield
    finally:
        path.unlink(missing_ok=True)
