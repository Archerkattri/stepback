"""Layer 1 (file checkpoint/rewind) correctness tests."""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

from stepback.engine import Engine


def _git(cwd: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True
    )
    return out.stdout.strip()


def _init_repo(path: Path) -> None:
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "t@t")
    _git(path, "config", "user.name", "t")


def _md5(path: Path) -> str:
    return hashlib.md5(path.read_bytes()).hexdigest()


# -- no-git (shadow) mode ---------------------------------------------------


def test_shadow_mode_snapshot_and_exact_rewind(tmp_path: Path):
    (tmp_path / "a.txt").write_text("hello\n")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.txt").write_text("beta\n")

    eng = Engine(tmp_path)
    assert eng.repo.mode == "shadow"
    # shadow store created, but NOT a real .git the user would see
    assert (tmp_path / ".stepback" / "shadow.git").is_dir()
    assert not (tmp_path / ".git").exists()

    cp = eng.checkpoint(label="baseline")
    assert cp is not None

    # mutate: edit, add, delete
    (tmp_path / "a.txt").write_text("hello world\n")
    (tmp_path / "c.txt").write_text("new file\n")
    (tmp_path / "sub" / "b.txt").unlink()

    eng.rewind(cp)

    assert (tmp_path / "a.txt").read_text() == "hello\n"          # edit reverted
    assert (tmp_path / "sub" / "b.txt").read_text() == "beta\n"   # deletion restored
    assert not (tmp_path / "c.txt").exists()                       # addition removed


def test_redo_reverses_a_rewind(tmp_path: Path):
    (tmp_path / "f.txt").write_text("v1\n")
    eng = Engine(tmp_path)
    cp = eng.checkpoint()

    (tmp_path / "f.txt").write_text("v2\n")
    (tmp_path / "g.txt").write_text("added\n")

    eng.rewind(cp)
    assert (tmp_path / "f.txt").read_text() == "v1\n"
    assert not (tmp_path / "g.txt").exists()

    entry, _ = eng.redo()
    assert entry is not None
    assert (tmp_path / "f.txt").read_text() == "v2\n"     # back to post-edit
    assert (tmp_path / "g.txt").read_text() == "added\n"


def test_redo_empty_returns_none(tmp_path: Path):
    (tmp_path / "f.txt").write_text("x\n")
    eng = Engine(tmp_path)
    eng.checkpoint()
    entry, hints = eng.redo()
    assert entry is None and hints == []


def test_binary_roundtrip_is_byte_exact(tmp_path: Path):
    blob = bytes(range(256)) * 40  # includes NUL bytes
    (tmp_path / "data.bin").write_bytes(blob)
    eng = Engine(tmp_path)
    before = _md5(tmp_path / "data.bin")
    cp = eng.checkpoint()

    (tmp_path / "data.bin").write_bytes(b"corrupted")
    eng.rewind(cp)

    assert (tmp_path / "data.bin").read_bytes() == blob
    assert _md5(tmp_path / "data.bin") == before


def test_noop_checkpoint_is_deduped(tmp_path: Path):
    (tmp_path / "f.txt").write_text("same\n")
    eng = Engine(tmp_path)
    first = eng.checkpoint()
    second = eng.checkpoint()  # nothing changed
    assert first is not None
    assert second is None
    assert len(eng.state.checkpoints) == 1


def test_nested_dirs_and_deletion_cleanup(tmp_path: Path):
    deep = tmp_path / "x" / "y" / "z"
    deep.mkdir(parents=True)
    (deep / "leaf.txt").write_text("leaf\n")
    eng = Engine(tmp_path)
    cp = eng.checkpoint()

    (tmp_path / "top.txt").write_text("top\n")
    eng.rewind(cp)
    assert not (tmp_path / "top.txt").exists()
    assert (deep / "leaf.txt").read_text() == "leaf\n"


# -- git (shared) mode ------------------------------------------------------


def test_shared_mode_does_not_touch_head_index_or_branch(tmp_path: Path):
    _init_repo(tmp_path)
    (tmp_path / "tracked.txt").write_text("v1\n")
    _git(tmp_path, "add", "tracked.txt")
    _git(tmp_path, "commit", "-qm", "init")

    head_before = _git(tmp_path, "rev-parse", "HEAD")
    index_before = _md5(tmp_path / ".git" / "index")
    status_before = _git(tmp_path, "status", "--porcelain")

    # user has uncommitted work when stepback runs
    (tmp_path / "tracked.txt").write_text("v1\nuncommitted\n")
    (tmp_path / "untracked.txt").write_text("scratch\n")

    eng = Engine(tmp_path)
    assert eng.repo.mode == "shared"
    cp = eng.checkpoint()
    assert cp is not None

    assert _git(tmp_path, "rev-parse", "HEAD") == head_before
    assert _md5(tmp_path / ".git" / "index") == index_before
    # user's working state and staging are exactly as they were
    (tmp_path / "tracked.txt").write_text("v1\nuncommitted\n")  # ensure unchanged text
    assert _git(tmp_path, "status", "--porcelain") == (
        status_before + ("\n" if status_before else "")
        + " M tracked.txt\n?? untracked.txt"
    ).strip()

    # checkpoint ref exists under the isolated namespace only
    refs = _git(tmp_path, "for-each-ref", "--format=%(refname)")
    assert any(r.startswith("refs/checkpoints/") for r in refs.splitlines())
    assert _git(tmp_path, "branch", "--format=%(refname:short)") == "master"


def test_shared_mode_respects_gitignore(tmp_path: Path):
    _init_repo(tmp_path)
    (tmp_path / ".gitignore").write_text("ignored/\n*.log\n")
    (tmp_path / "keep.txt").write_text("keep\n")
    (tmp_path / "ignored").mkdir()
    (tmp_path / "ignored" / "junk.txt").write_text("junk\n")
    (tmp_path / "debug.log").write_text("noise\n")

    eng = Engine(tmp_path)
    cp = eng.checkpoint()
    paths = eng._ls_paths(cp.tree)
    assert "keep.txt" in paths
    assert ".gitignore" in paths
    assert "ignored/junk.txt" not in paths
    assert "debug.log" not in paths


def test_shared_mode_rewind_restores_exactly(tmp_path: Path):
    _init_repo(tmp_path)
    (tmp_path / "src.py").write_text("def good():\n    return 1\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "init")

    eng = Engine(tmp_path)
    cp = eng.checkpoint()

    # agent makes a mess
    (tmp_path / "src.py").write_text("def broken(:\n")
    (tmp_path / "oops.py").write_text("garbage\n")

    plan = eng.plan_restore(cp.tree)
    assert plan.changed == 2  # src.py modified + oops.py added
    eng.rewind(cp)

    assert (tmp_path / "src.py").read_text() == "def good():\n    return 1\n"
    assert not (tmp_path / "oops.py").exists()
    # user's real HEAD still intact
    assert _git(tmp_path, "log", "--oneline").count("\n") == 0  # single commit


def test_diff_shows_checkpoint_changes(tmp_path: Path):
    (tmp_path / "f.txt").write_text("one\n")
    eng = Engine(tmp_path)
    eng.checkpoint()
    (tmp_path / "f.txt").write_text("one\ntwo\n")
    cp2 = eng.checkpoint()
    patch = eng.diff(cp2)
    assert "+two" in patch
    assert "f.txt" in patch
