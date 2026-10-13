"""Filesystem-observer integration checks for watcher lifecycle and callbacks."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from stepback.watcher import DebouncedWatcher


def _wait_until(pred, timeout: float = 5.0) -> bool:
    """Poll observer-thread state without relying on a fixed long sleep."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.01)
    return bool(pred())


def test_watcher_starts_and_stops(tmp_path: Path):
    watcher = DebouncedWatcher(tmp_path, lambda: None, 0.1)
    with watcher:
        assert watcher.backend in {"native", "polling", "none"}
    watcher.stop()


def test_watcher_coalesces_real_file_changes_and_ignores_internal_dirs(tmp_path: Path):
    settled = threading.Event()
    calls: list[float] = []

    def on_settle() -> None:
        calls.append(time.monotonic())
        settled.set()

    watcher = DebouncedWatcher(tmp_path, on_settle, quiet_seconds=0.1)
    with watcher:
        if watcher.backend == "none":
            pytest.skip("no native or polling filesystem observer is available")

        for relative in (Path(".git/index"), Path(".stepback/state.json")):
            path = tmp_path / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("internal\n")
        time.sleep(0.25)
        assert not settled.is_set(), "internal state changes must not trigger checkpoints"

        changed = tmp_path / "source.py"
        for version in range(4):
            changed.write_text(f"version = {version}\n")
            time.sleep(0.02)
        assert settled.wait(5), "real filesystem change did not reach the watcher callback"
        time.sleep(0.2)

    assert len(calls) == 1, "a burst of filesystem events should settle as one callback"


def test_watcher_exposes_callback_error_from_filesystem_event(tmp_path: Path):
    def boom() -> None:
        raise ValueError("bad")

    watcher = DebouncedWatcher(tmp_path, boom, quiet_seconds=0.02)
    with watcher:
        if watcher.backend == "none":
            pytest.skip("no native or polling filesystem observer is available")
        (tmp_path / "a.txt").write_text("trigger\n")
        assert _wait_until(lambda: watcher.last_error is not None), "filesystem callback never fired"
    assert "ValueError" in watcher.last_error
