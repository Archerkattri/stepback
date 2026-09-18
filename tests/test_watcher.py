"""Watcher tests: debounce coalescing, ignored paths, flush, and degradation."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from types import SimpleNamespace

from stepback.watcher import DebouncedWatcher, _DebounceHandler


def _file_event(path: str):
    return SimpleNamespace(src_path=path, dest_path="", is_directory=False)


def _wait_until(pred, timeout: float = 5.0) -> bool:
    """Poll for a debounced timer callback; fixed sleeps flake on loaded CI."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.01)
    return bool(pred())


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


def test_callback_exception_is_recorded_and_reported():
    errors: list[Exception] = []

    def boom():
        raise RuntimeError("checkpoint failed")

    h = _DebounceHandler(boom, 0.02, on_error=errors.append)
    h.on_any_event(_file_event("/work/a.txt"))
    assert _wait_until(lambda: errors), "debounced callback never fired"
    assert "checkpoint failed" in str(errors[0])
    assert h.last_error is not None
    assert "RuntimeError" in h.last_error


def test_watcher_starts_and_stops(tmp_path: Path):
    fired = {"n": 0}
    w = DebouncedWatcher(tmp_path, lambda: fired.__setitem__("n", fired["n"] + 1), 0.1)
    with w:
        # some backend was selected (native, polling, or none) without raising
        assert w.backend in {"native", "polling", "none"}
    # stop() is clean and idempotent
    w.stop()


def test_watcher_exposes_callback_error(tmp_path: Path):
    def boom():
        raise ValueError("bad")

    w = DebouncedWatcher(tmp_path, boom, 0.02)
    w._handler.on_any_event(_file_event(str(tmp_path / "a.txt")))
    assert _wait_until(lambda: w.last_error is not None), "debounced callback never fired"
    assert "ValueError" in w.last_error
