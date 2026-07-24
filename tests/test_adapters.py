"""Layer 2 (conversation rewind) tests — best-effort adapters.

These use fake HOME directories so no real Claude Code / Codex state is touched.
"""

from __future__ import annotations

from pathlib import Path

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
