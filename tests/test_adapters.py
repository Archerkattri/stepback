"""Layer 2 (conversation rewind) tests — best-effort adapters.

These use fake HOME directories so no real Claude Code / Codex state is touched.
"""

from __future__ import annotations

import json
from pathlib import Path

from stepback.adapters import adapter_by_name, detect_adapters
from stepback.adapters.claude_code import ClaudeCodeAdapter, _slug
from stepback.adapters.codex import CodexAdapter
from stepback.engine import Engine


def test_claude_slug_matches_known_format():
    assert _slug(Path("/home/krishi/workspace/brain")) == "-home-krishi-workspace-brain"


def test_claude_adapter_degrades_when_absent(tmp_path: Path):
    home = tmp_path / "home"
    home.mkdir()
    a = ClaudeCodeAdapter(tmp_path, home=home)
    assert a.detect() is False
    assert a.session_files() == []
    assert a.snapshot(tmp_path / "dest") == {}
    assert a.restore({}, tmp_path / "dest") is False
    # never raises, always yields a usable hint
    assert "claude" in a.resume_hint({})


def test_claude_adapter_snapshot_and_restore(tmp_path: Path):
    home = tmp_path / "home"
    work = tmp_path / "work"
    work.mkdir()
    proj = home / ".claude" / "projects" / _slug(work)
    proj.mkdir(parents=True)
    transcript = proj / "abcd-1234.jsonl"
    transcript.write_text('{"role":"user","content":"v1"}\n')

    a = ClaudeCodeAdapter(work, home=home)
    assert a.detect() is True

    dest = tmp_path / "snap"
    meta = a.snapshot(dest)
    assert meta["uuid"] == "abcd-1234"
    assert a.resume_hint(meta) == "claude --resume abcd-1234"

    # conversation moves on, then we rewind it
    transcript.write_text('{"role":"user","content":"v1"}\n{"role":"user","content":"v2-bad"}\n')
    assert a.restore(meta, dest) is True
    assert transcript.read_text() == '{"role":"user","content":"v1"}\n'


def test_codex_adapter_degrades_when_absent(tmp_path: Path):
    home = tmp_path / "home"
    home.mkdir()
    a = CodexAdapter(tmp_path, home=home)
    assert a.detect() is False
    assert a.snapshot(tmp_path / "d") == {}
    assert a.restore({}, tmp_path / "d") is False


def test_codex_adapter_snapshot_and_restore(tmp_path: Path):
    work = tmp_path / "work"
    work.mkdir()
    home = tmp_path / "home"
    sessions = home / ".codex" / "sessions" / "2026" / "07" / "24"
    sessions.mkdir(parents=True)
    sess = sessions / "rollout-abc.jsonl"
    sess.write_text(json.dumps({"type": "session_meta", "payload": {"id": "abc", "cwd": str(work)}}) + "\nturn1\n")

    a = CodexAdapter(work, home=home)
    assert a.detect() is True

    dest = tmp_path / "snap"
    meta = a.snapshot(dest)
    assert meta["session_file"] == "rollout-abc.jsonl"
    assert "codex" in a.resume_hint(meta)

    sess.write_text(
        json.dumps({"type": "session_meta", "payload": {"id": "abc", "cwd": str(work)}})
        + "\nturn1\nturn2-bad\n"
    )
    assert a.restore(meta, dest) is True
    assert sess.read_text().endswith("turn1\n")  # restored to the exact spot


def test_claude_adapter_picks_most_recent_transcript(tmp_path: Path):
    import os
    import time

    home = tmp_path / "home"
    work = tmp_path / "work"
    work.mkdir()
    proj = home / ".claude" / "projects" / _slug(work)
    proj.mkdir(parents=True)
    old = proj / "old.jsonl"
    old.write_text("old\n")
    new = proj / "new.jsonl"
    new.write_text("new\n")
    # make `new` unambiguously newer
    now = time.time()
    os.utime(old, (now - 100, now - 100))
    os.utime(new, (now, now))

    a = ClaudeCodeAdapter(work, home=home)
    meta = a.snapshot(tmp_path / "snap")
    assert meta["uuid"] == "new"


def test_detect_adapters_finds_codex(tmp_path: Path, monkeypatch):
    home = tmp_path / "home"
    sessions = home / ".codex" / "sessions"
    sessions.mkdir(parents=True)
    (sessions / "s.jsonl").write_text(
        json.dumps({"type": "session_meta", "payload": {"id": "s", "cwd": str(tmp_path)}})
        + "\n"
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    names = {a.name for a in detect_adapters(tmp_path)}
    assert "codex" in names
    assert adapter_by_name("codex", tmp_path) is not None
    assert adapter_by_name("nonexistent", tmp_path) is None


def _codex_session(path: Path, *, session_id: str, cwd: Path) -> None:
    path.write_text(
        json.dumps({"type": "session_meta", "payload": {"id": session_id, "cwd": str(cwd)}})
        + "\nturn\n"
    )


def test_codex_session_is_bound_to_exact_work_tree_not_mtime(tmp_path: Path):
    home = tmp_path / "home"
    sessions = home / ".codex" / "sessions" / "2026" / "07" / "24"
    sessions.mkdir(parents=True)
    project_a = tmp_path / "project-a"
    project_b = tmp_path / "project-b"
    project_a.mkdir()
    project_b.mkdir()
    session_a = sessions / "a.jsonl"
    session_b = sessions / "b.jsonl"
    _codex_session(session_a, session_id="a", cwd=project_a)
    _codex_session(session_b, session_id="b", cwd=project_b)
    import os
    import time

    now = time.time()
    os.utime(session_a, (now - 100, now - 100))
    os.utime(session_b, (now, now))

    adapter = CodexAdapter(project_a, home=home)
    assert adapter.session_files() == [session_a]
    assert adapter.snapshot(tmp_path / "snap")["session_id"] == "a"


def test_codex_ambiguous_project_sessions_degrade_to_file_only(tmp_path: Path):
    home = tmp_path / "home"
    sessions = home / ".codex" / "sessions"
    sessions.mkdir(parents=True)
    _codex_session(sessions / "a.jsonl", session_id="a", cwd=tmp_path)
    _codex_session(sessions / "b.jsonl", session_id="b", cwd=tmp_path)
    adapter = CodexAdapter(tmp_path, home=home)
    assert adapter.detect() is False
    assert adapter.snapshot(tmp_path / "snap") == {}


def test_codex_explicit_session_selection_is_supported(tmp_path: Path):
    home = tmp_path / "home"
    sessions = home / ".codex" / "sessions"
    sessions.mkdir(parents=True)
    first = sessions / "first.jsonl"
    second = sessions / "second.jsonl"
    _codex_session(first, session_id="first", cwd=tmp_path)
    _codex_session(second, session_id="second", cwd=tmp_path)
    adapter = CodexAdapter(tmp_path, home=home, session_id="second")
    assert adapter.session_files() == [second]


def test_codex_unrecognized_metadata_is_not_selected(tmp_path: Path):
    home = tmp_path / "home"
    sessions = home / ".codex" / "sessions"
    sessions.mkdir(parents=True)
    (sessions / "unknown.jsonl").write_text("turn without session metadata\n")
    adapter = CodexAdapter(tmp_path, home=home)
    assert adapter.detect() is False


def test_codex_restore_rejects_traversal_and_work_tree_mismatch(tmp_path: Path):
    home = tmp_path / "home"
    sessions = home / ".codex" / "sessions"
    sessions.mkdir(parents=True)
    adapter = CodexAdapter(tmp_path, home=home)
    src = tmp_path / "snap"
    src.mkdir()
    (src / "session.jsonl").write_text("saved\n")
    meta = {"session_file": "session.jsonl", "rel_path": "../escape.jsonl"}
    assert adapter.restore(meta, src) is False
    meta = {
        "session_file": "session.jsonl",
        "rel_path": "session.jsonl",
        "work_tree": str(tmp_path / "other"),
    }
    assert adapter.restore(meta, src) is False


class _ExplodingAdapter:
    name = "boom"

    def __init__(self, *_):
        pass

    def detect(self) -> bool:
        return True

    def session_files(self):
        raise RuntimeError("nope")

    def snapshot(self, dest):
        raise RuntimeError("nope")

    def restore(self, meta, src) -> bool:
        raise RuntimeError("nope")

    def resume_hint(self, meta) -> str:
        raise RuntimeError("nope")


def test_misbehaving_adapter_never_breaks_file_layer(tmp_path: Path):
    """An adapter that raises in every method must not affect Layer 1."""
    (tmp_path / "f.txt").write_text("v1\n")
    eng = Engine(tmp_path, adapters=[_ExplodingAdapter()])
    cp = eng.checkpoint()
    assert cp is not None
    assert cp.adapters == {}  # the exploding adapter contributed nothing
    (tmp_path / "f.txt").write_text("v2\n")
    hints = eng.rewind(cp)
    assert (tmp_path / "f.txt").read_text() == "v1\n"  # file rewind still exact
    assert hints == []


def test_engine_works_with_zero_adapters(tmp_path: Path):
    """The file layer must be fully functional with no adapters at all."""
    (tmp_path / "f.txt").write_text("a\n")
    eng = Engine(tmp_path, adapters=[])
    cp = eng.checkpoint()
    (tmp_path / "f.txt").write_text("b\n")
    eng.rewind(cp)
    assert (tmp_path / "f.txt").read_text() == "a\n"


def test_engine_snapshots_conversation_with_adapter(tmp_path: Path):
    home = tmp_path / "home"
    work = tmp_path / "work"
    work.mkdir()
    proj = home / ".claude" / "projects" / _slug(work)
    proj.mkdir(parents=True)
    transcript = proj / "sess.jsonl"
    transcript.write_text("turn1\n")

    adapter = ClaudeCodeAdapter(work, home=home)
    eng = Engine(work, adapters=[adapter])
    (work / "code.py").write_text("v1\n")
    cp = eng.checkpoint()
    assert "claude-code" in cp.adapters

    # both files and conversation move forward
    (work / "code.py").write_text("v2\n")
    transcript.write_text("turn1\nturn2-bad\n")

    hints = eng.rewind(cp)
    assert (work / "code.py").read_text() == "v1\n"
    assert transcript.read_text() == "turn1\n"     # conversation rewound too
    assert any("claude --resume" in h for h in hints)
