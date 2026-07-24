"""Production-hardening tests: edge cases and correctness invariants.

Covers unusual file names, symlinks, atomic/all-or-nothing restore, the
"never touch the user's git" invariant under awkward repo states (detached
HEAD, mid-merge, staged changes, submodule-like gitlinks), corrupt state
recovery, garbage-collection-safe redo, locking, and the watcher pause.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from stepback.engine import Engine
from stepback.errors import RestoreError
from stepback.lock import LockTimeout, file_lock
from stepback.store import State


def _git(cwd: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True
    )
    return out.stdout.strip()


def _init_repo(path: Path) -> None:
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "t@t")
    _git(path, "config", "user.name", "t")
    _git(path, "config", "commit.gpgsign", "false")


# -- unusual file names -----------------------------------------------------


def test_unicode_and_space_names_roundtrip(tmp_path: Path):
    weird = tmp_path / "héllo wörld.txt"
    weird.write_text("unicode\n")
    (tmp_path / "normal.txt").write_text("n\n")
    eng = Engine(tmp_path)
    cp = eng.checkpoint()

    # add another unicode file, then rewind should delete it exactly
    (tmp_path / "日本語.txt").write_text("added\n")
    weird.write_text("changed\n")
    eng.rewind(cp)

    assert weird.read_text() == "unicode\n"
    assert not (tmp_path / "日本語.txt").exists()


def test_leading_dash_name_roundtrip(tmp_path: Path):
    dash = tmp_path / "-weird-name.txt"
    dash.write_text("v1\n")
    eng = Engine(tmp_path)
    cp = eng.checkpoint()
    (tmp_path / "-added.txt").write_text("junk\n")
    eng.rewind(cp)
    assert dash.read_text() == "v1\n"
    assert not (tmp_path / "-added.txt").exists()


# -- symlinks ---------------------------------------------------------------


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need admin on Windows")
def test_symlink_snapshot_and_restore(tmp_path: Path):
    (tmp_path / "target.txt").write_text("real\n")
    link = tmp_path / "link.txt"
    link.symlink_to("target.txt")
    eng = Engine(tmp_path)
    cp = eng.checkpoint()

    # replace the symlink with a regular file, then rewind
    link.unlink()
    link.write_text("not a link anymore\n")
    eng.rewind(cp)

    assert link.is_symlink()
    assert os.readlink(link) == "target.txt"


# -- atomic restore: file<->directory transitions ---------------------------


def test_restore_handles_file_to_directory_transition(tmp_path: Path):
    (tmp_path / "x").write_text("i am a file\n")
    eng = Engine(tmp_path)
    cp = eng.checkpoint()  # x is a file

    (tmp_path / "x").unlink()
    (tmp_path / "x").mkdir()
    (tmp_path / "x" / "y.txt").write_text("now a dir\n")
    cp2 = eng.checkpoint()  # x is a directory

    eng.rewind(cp)  # back to x-as-file
    assert (tmp_path / "x").is_file()
    assert (tmp_path / "x").read_text() == "i am a file\n"

    eng.redo() if False else eng.rewind(cp2)  # forward to x-as-dir
    assert (tmp_path / "x").is_dir()
    assert (tmp_path / "x" / "y.txt").read_text() == "now a dir\n"


def test_restore_only_touches_changed_files(tmp_path: Path):
    untouched = tmp_path / "keep.txt"
    untouched.write_text("stable\n")
    (tmp_path / "churn.txt").write_text("v1\n")
    eng = Engine(tmp_path)
    cp = eng.checkpoint()

    mtime_before = untouched.stat().st_mtime_ns
    time.sleep(0.01)
    (tmp_path / "churn.txt").write_text("v2\n")
    eng.rewind(cp)

    assert (tmp_path / "churn.txt").read_text() == "v1\n"
    # the unchanged file was not rewritten
    assert untouched.stat().st_mtime_ns == mtime_before


# -- empty / deep / large ---------------------------------------------------


def test_empty_tree_checkpoint(tmp_path: Path):
    eng = Engine(tmp_path)
    cp = eng.checkpoint()  # nothing in the tree
    assert cp is not None
    (tmp_path / "new.txt").write_text("hi\n")
    eng.rewind(cp)  # should remove new.txt, back to empty
    assert not (tmp_path / "new.txt").exists()


def test_deeply_nested_paths(tmp_path: Path):
    deep = tmp_path.joinpath(*[f"d{i}" for i in range(25)])
    deep.mkdir(parents=True)
    (deep / "leaf.txt").write_text("deep\n")
    eng = Engine(tmp_path)
    cp = eng.checkpoint()
    (deep / "leaf.txt").write_text("broken\n")
    eng.rewind(cp)
    assert (deep / "leaf.txt").read_text() == "deep\n"


# -- git isolation under awkward repo states --------------------------------


def test_detached_head_is_left_untouched(tmp_path: Path):
    _init_repo(tmp_path)
    (tmp_path / "f.txt").write_text("one\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "c1")
    sha = _git(tmp_path, "rev-parse", "HEAD")
    _git(tmp_path, "checkout", "-q", "--detach", sha)

    eng = Engine(tmp_path)
    assert eng.repo.mode == "shared"
    (tmp_path / "f.txt").write_text("two\n")
    cp = eng.checkpoint()
    assert cp is not None
    (tmp_path / "f.txt").write_text("three\n")
    eng.rewind(cp)

    assert (tmp_path / "f.txt").read_text() == "two\n"
    assert _git(tmp_path, "rev-parse", "HEAD") == sha  # still detached at same sha


def test_mid_merge_state_is_preserved(tmp_path: Path):
    _init_repo(tmp_path)
    (tmp_path / "f.txt").write_text("base\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "base")
    head = _git(tmp_path, "rev-parse", "HEAD")
    # simulate being mid-merge
    (tmp_path / ".git" / "MERGE_HEAD").write_text(head + "\n")

    eng = Engine(tmp_path)
    (tmp_path / "f.txt").write_text("edited during merge\n")
    cp = eng.checkpoint()
    assert cp is not None

    assert (tmp_path / ".git" / "MERGE_HEAD").read_text().strip() == head
    assert _git(tmp_path, "rev-parse", "HEAD") == head


def test_staged_changes_survive_checkpoint_and_rewind(tmp_path: Path):
    _init_repo(tmp_path)
    (tmp_path / "f.txt").write_text("v1\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "init")

    # user stages an edit but does not commit it
    (tmp_path / "f.txt").write_text("v2-staged\n")
    _git(tmp_path, "add", "f.txt")
    index_before = (tmp_path / ".git" / "index").read_bytes()
    staged_diff_before = _git(tmp_path, "diff", "--cached", "--name-only")

    eng = Engine(tmp_path)
    cp = eng.checkpoint()
    # agent then makes a further mess on top
    (tmp_path / "f.txt").write_text("v3-broken\n")
    eng.rewind(cp)

    # working file is back to the checkpoint content
    assert (tmp_path / "f.txt").read_text() == "v2-staged\n"
    # the user's staging area is byte-for-byte unchanged
    assert (tmp_path / ".git" / "index").read_bytes() == index_before
    assert _git(tmp_path, "diff", "--cached", "--name-only") == staged_diff_before


def test_nested_git_repo_does_not_crash(tmp_path: Path):
    _init_repo(tmp_path)
    (tmp_path / "top.txt").write_text("top\n")
    inner = tmp_path / "vendor"
    inner.mkdir()
    _init_repo(inner)
    (inner / "lib.txt").write_text("lib\n")
    _git(inner, "add", "-A")
    _git(inner, "commit", "-qm", "inner")

    eng = Engine(tmp_path)
    cp = eng.checkpoint()  # git records the inner repo as a gitlink; must not crash
    assert cp is not None
    (tmp_path / "top.txt").write_text("changed\n")
    eng.rewind(cp)
    assert (tmp_path / "top.txt").read_text() == "top\n"
    # inner repo left intact
    assert (inner / "lib.txt").read_text() == "lib\n"


# -- corrupt / forward-compatible state -------------------------------------


def test_corrupt_state_is_quarantined_not_lost(tmp_path: Path):
    (tmp_path / "f.txt").write_text("x\n")
    eng = Engine(tmp_path)
    eng.checkpoint()
    state_path = eng.state_path
    assert state_path.exists()

    state_path.write_text("{ this is not valid json ")
    reloaded = State.load(state_path)
    assert reloaded.checkpoints == []  # fresh slate
    # the corrupt file was preserved, not silently deleted
    quarantined = list(state_path.parent.glob("state.json.corrupt-*"))
    assert quarantined, "corrupt state should be moved aside for forensics"


def test_state_ignores_unknown_future_keys(tmp_path: Path):
    (tmp_path / "f.txt").write_text("x\n")
    eng = Engine(tmp_path)
    eng.checkpoint()
    raw = json.loads(eng.state_path.read_text())
    raw["some_future_field"] = 42
    raw["checkpoints"][0]["future_cp_field"] = "ignore me"
    eng.state_path.write_text(json.dumps(raw))
    reloaded = State.load(eng.state_path)
    assert len(reloaded.checkpoints) == 1  # loaded despite unknown keys


# -- redo durability across git gc ------------------------------------------


def test_redo_survives_user_git_gc(tmp_path: Path):
    _init_repo(tmp_path)
    (tmp_path / "f.txt").write_text("v1\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "init")

    eng = Engine(tmp_path)
    cp = eng.checkpoint()
    (tmp_path / "f.txt").write_text("v2\n")
    (tmp_path / "g.txt").write_text("added\n")
    eng.rewind(cp)  # pre-rewind state (v2 + g.txt) goes on the redo stack

    # the user runs an aggressive gc, which prunes unreferenced objects
    _git(tmp_path, "gc", "--prune=now", "--aggressive")

    entry, _ = eng.redo()
    assert entry is not None
    assert (tmp_path / "f.txt").read_text() == "v2\n"     # redo still works
    assert (tmp_path / "g.txt").read_text() == "added\n"


# -- locking ----------------------------------------------------------------


def test_file_lock_is_exclusive(tmp_path: Path):
    lock = tmp_path / "l.lock"
    order: list[str] = []

    def worker() -> None:
        with file_lock(lock, timeout=5):
            order.append("b-in")
            time.sleep(0.05)
            order.append("b-out")

    with file_lock(lock, timeout=5):
        t = threading.Thread(target=worker)
        t.start()
        time.sleep(0.1)
        order.append("a-out")
    t.join()
    # b could not enter until a released
    assert order == ["a-out", "b-in", "b-out"]


def test_file_lock_times_out(tmp_path: Path):
    lock = tmp_path / "l.lock"
    with file_lock(lock, timeout=5):
        with pytest.raises(LockTimeout):
            with file_lock(lock, timeout=0.2, poll=0.02):
                pass


def test_file_lock_fallback_without_fcntl(tmp_path: Path, monkeypatch):
    """Exercise the O_EXCL fallback used on platforms without fcntl."""
    import stepback.lock as lockmod

    monkeypatch.setattr(lockmod, "fcntl", None)
    lock = tmp_path / "l.lock"
    with file_lock(lock, timeout=5):
        assert lock.exists()  # lock file held
        with pytest.raises(LockTimeout):
            with file_lock(lock, timeout=0.2, poll=0.02):
                pass
    assert not lock.exists()  # released on exit


def test_concurrent_checkpoints_are_serialized(tmp_path: Path):
    (tmp_path / "f.txt").write_text("start\n")
    errors: list[Exception] = []

    def churn(tag: str) -> None:
        try:
            eng = Engine(tmp_path)
            for i in range(5):
                (tmp_path / f"{tag}-{i}.txt").write_text(f"{i}\n")
                eng.checkpoint(label=f"{tag}-{i}")
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=churn, args=(t,)) for t in ("x", "y")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"concurrent checkpoints raised: {errors}"
    final = Engine(tmp_path)
    # ids are unique and monotonic despite concurrency
    ids = [c.id for c in final.state.checkpoints]
    assert len(ids) == len(set(ids))
    assert ids == sorted(ids)


# -- watcher pause (rewind while watching) ----------------------------------


def test_rewind_sets_watcher_pause(tmp_path: Path):
    (tmp_path / "f.txt").write_text("v1\n")
    watcher_eng = Engine(tmp_path)
    watcher_eng.checkpoint()
    assert watcher_eng.is_paused() is False

    # a separate process/engine performs a rewind
    other = Engine(tmp_path)
    cp = other.state.latest()
    (tmp_path / "f.txt").write_text("v2\n")
    other.rewind(cp)

    # the watcher-side engine observes the pause and would skip its checkpoint
    assert watcher_eng.is_paused() is True


# -- restore error surfaces cleanly -----------------------------------------


def test_restore_error_is_typed(tmp_path: Path):
    (tmp_path / "f.txt").write_text("v1\n")
    eng = Engine(tmp_path)
    eng.checkpoint()
    # a bogus tree sha makes staging fail; it must raise RestoreError, not a
    # raw GitError, and must not have touched the real tree first.
    with pytest.raises(RestoreError):
        eng._restore_tree("0" * 40)
    assert (tmp_path / "f.txt").read_text() == "v1\n"
