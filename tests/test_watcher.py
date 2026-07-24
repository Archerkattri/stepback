"""Watcher tests: debounce coalescing, ignored paths, flush, and degradation."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from types import SimpleNamespace

from stepback.watcher import DebouncedWatcher, _DebounceHandler


def _file_event(path: str):
    return SimpleNamespace(src_path=path, dest_path="", is_directory=False)


def test_debounce_coalesces_a_burst():
    fired = threading.Event()
    calls = {"n": 0}

    def on_settle():
        calls["n"] += 1
        fired.set()

    h = _DebounceHandler(on_settle, quiet_seconds=0.1)
    for _ in range(10):
        h.on_any_event(_file_event("/work/a.txt"))
        time.sleep(0.005)
    assert fired.wait(2.0)
    time.sleep(0.15)
    assert calls["n"] == 1  # ten events -> one settle


def test_ignored_dirs_do_not_trigger():
    calls = {"n": 0}
    h = _DebounceHandler(lambda: calls.__setitem__("n", calls["n"] + 1), 0.05)
    h.on_any_event(_file_event("/work/.git/index"))
    h.on_any_event(_file_event("/work/.stepback/state.json"))
    time.sleep(0.15)
    assert calls["n"] == 0


def test_directory_events_ignored():
    calls = {"n": 0}
    h = _DebounceHandler(lambda: calls.__setitem__("n", calls["n"] + 1), 0.05)
    h.on_any_event(SimpleNamespace(src_path="/work/d", dest_path="", is_directory=True))
    time.sleep(0.15)
    assert calls["n"] == 0


def test_flush_fires_pending_immediately():
    calls = {"n": 0}
    h = _DebounceHandler(lambda: calls.__setitem__("n", calls["n"] + 1), 10.0)
    h.on_any_event(_file_event("/work/a.txt"))  # long quiet window, not yet fired
    assert calls["n"] == 0
    h.flush()
    assert calls["n"] == 1


def test_callback_exception_never_propagates():
    def boom():
        raise RuntimeError("checkpoint failed")

    h = _DebounceHandler(boom, 0.02)
    h.on_any_event(_file_event("/work/a.txt"))
    time.sleep(0.1)  # should not raise into the watcher thread
    h.flush()  # also swallowed


def test_watcher_starts_and_stops(tmp_path: Path):
    fired = {"n": 0}
    w = DebouncedWatcher(tmp_path, lambda: fired.__setitem__("n", fired["n"] + 1), 0.1)
    with w:
        # some backend was selected (native, polling, or none) without raising
        assert w.backend in {"native", "polling", "none"}
    # stop() is clean and idempotent
    w.stop()
