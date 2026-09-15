"""A debounced background file watcher.

Observes the work tree during an agent session and fires a callback once edits
settle (a quiet period with no further filesystem events), so a burst of writes
from the agent collapses into a single checkpoint.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer
from watchdog.observers.polling import PollingObserver

# Directories that must never trigger or be included in a checkpoint.
IGNORED_DIRS = {".git", ".stepback"}


class _DebounceHandler(FileSystemEventHandler):
    def __init__(
        self,
        on_settle: Callable[[], None],
        quiet_seconds: float,
        on_error: Callable[[Exception], None] | None = None,
        on_pending: Callable[[bool], None] | None = None,
    ):
        self._on_settle = on_settle
        self._on_error = on_error
        self._on_pending = on_pending
        self._quiet = quiet_seconds
        self._timer: threading.Timer | None = None
        self._lock = threading.Lock()
        self._pending = False
        self.last_error: str | None = None
        self.last_error_time: float | None = None
        self.last_success_time: float | None = None

    def _relevant(self, event) -> bool:
        paths = [getattr(event, "src_path", ""), getattr(event, "dest_path", "")]
        for p in paths:
            if not p:
                continue
            parts = set(Path(p).parts)
            if parts & IGNORED_DIRS:
                return False
        return True

    def on_any_event(self, event) -> None:
        if event.is_directory:
            return
        if not self._relevant(event):
            return
        notify = False
        with self._lock:
            notify = not self._pending
            self._pending = True
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(self._quiet, self._fire)
            self._timer.daemon = True
            self._timer.start()
        if notify and self._on_pending is not None:
            try:
                self._on_pending(True)
            except Exception:
                pass

    def _fire(self) -> None:
        with self._lock:
            self._pending = False
            self._timer = None
        if self._on_pending is not None:
            try:
                self._on_pending(False)
            except Exception:
                pass
        try:
            self._on_settle()
        except Exception as exc:  # noqa: BLE001
            # A checkpoint failure must never take down the watched agent, but
            # it must remain visible to status/logging instead of disappearing.
            self.last_error = f"{type(exc).__name__}: {exc}"
            self.last_error_time = time.time()
            if self._on_error is not None:
                try:
                    self._on_error(exc)
                except Exception:
                    pass
        else:
            self.last_success_time = time.time()

    def flush(self) -> None:
        """Fire immediately if edits are pending (used on shutdown)."""
        with self._lock:
            pending = self._pending
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
            self._pending = False
        if pending and self._on_pending is not None:
            try:
                self._on_pending(False)
            except Exception:
                pass
        if pending:
            try:
                self._on_settle()
            except Exception as exc:  # noqa: BLE001
                self.last_error = f"{type(exc).__name__}: {exc}"
                self.last_error_time = time.time()
                if self._on_error is not None:
                    try:
                        self._on_error(exc)
                    except Exception:
                        pass
            else:
                self.last_success_time = time.time()

    @property
    def pending(self) -> bool:
        with self._lock:
            return self._pending


class DebouncedWatcher:
    def __init__(
        self,
        path: Path,
        on_settle: Callable[[], None],
        quiet_seconds: float = 0.5,
        on_error: Callable[[Exception], None] | None = None,
        on_pending: Callable[[bool], None] | None = None,
    ):
        self.path = Path(path)
        self._handler = _DebounceHandler(on_settle, quiet_seconds, on_error, on_pending)
        self._observer: Any = None
        self.backend = "none"

    @property
    def last_error(self) -> str | None:
        return self._handler.last_error

    @property
    def pending(self) -> bool:
        return self._handler.pending

    def __enter__(self) -> DebouncedWatcher:
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()

    def start(self) -> None:
        """Start watching, degrading native -> polling -> none as needed.

        On constrained hosts the native (inotify) backend can be exhausted;
        rather than crash the watched agent, fall back to a polling observer,
        and finally to no live watching at all (start/end checkpoints still
        happen).  A failure to watch must never take down the agent.
        """
        for factory, name in ((Observer, "native"), (PollingObserver, "polling")):
            try:
                obs = factory()
                obs.schedule(self._handler, str(self.path), recursive=True)
                obs.start()
                self._observer = obs
                self.backend = name
                return
            except Exception:
                self._observer = None
                continue
        self.backend = "none"

    def stop(self) -> None:
        if self._observer is not None:
            try:
                self._observer.stop()
                self._observer.join(timeout=5)
            except Exception:
                pass
        # Capture any edits still within the quiet window.
        self._handler.flush()
