"""State persistence tests: round-trip, atomic write, redo field."""

from __future__ import annotations

from pathlib import Path

from stepback.store import Checkpoint, RedoEntry, State, _atomic_write


def test_state_roundtrip(tmp_path: Path):
    path = tmp_path / "state.json"
    s = State(counter=2, current_session="s1")
    s.checkpoints.append(
        Checkpoint(
            id=1, session="s1", n=0, tree="t", commit="c", parent=None,
            time="2026-07-24T00:00:00+00:00", summary="1 file(s) [+1]",
        )
    )
    s.redo.append(
        RedoEntry(tree="rt", commit="rc", time="t", ref="refs/checkpoints/_redo/x")
    )
    s.save(path)

    loaded = State.load(path)
    assert loaded.counter == 2
    assert loaded.current_session == "s1"
    assert loaded.checkpoints[0].id == 1
    assert loaded.redo[0].ref == "refs/checkpoints/_redo/x"


def test_by_id_and_latest(tmp_path: Path):
    s = State()
    for i in range(1, 4):
        s.checkpoints.append(
            Checkpoint(id=i, session="s", n=i, tree=f"t{i}", commit=f"c{i}",
                       parent=None, time="t", summary="s")
        )
    assert s.by_id(2).tree == "t2"
    assert s.by_id(99) is None
    assert s.latest().id == 3


def test_next_id_is_monotonic():
    s = State()
    assert [s.next_id() for _ in range(3)] == [1, 2, 3]


def test_atomic_write_replaces_cleanly(tmp_path: Path):
    path = tmp_path / "sub" / "f.txt"
    _atomic_write(path, "hello")
    assert path.read_text() == "hello"
    _atomic_write(path, "world")
    assert path.read_text() == "world"
    # no temp files left behind
    assert list(path.parent.glob(".state-*")) == []


def test_load_missing_returns_empty(tmp_path: Path):
    assert State.load(tmp_path / "nope.json").checkpoints == []
