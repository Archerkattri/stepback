"""Best-effort conversation adapter for Claude Code.

Claude Code stores per-project session transcripts as JSONL under
``~/.claude/projects/<slug>/<uuid>.jsonl`` where ``<slug>`` is the working
directory with every non-alphanumeric character replaced by ``-`` (e.g.
``/home/krishi/workspace/brain`` -> ``-home-krishi-workspace-brain``).  A global
command history lives at ``~/.claude/history.jsonl``.

WARNING: these paths and the JSONL format are private, undocumented Claude Code
internals and change between versions.  This adapter is intentionally
best-effort: it captures the most-recently-modified transcript and restores it,
and no-ops if anything is missing.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path


def _slug(path: Path) -> str:
    return re.sub(r"[^a-zA-Z0-9]", "-", str(path.resolve()))


class ClaudeCodeAdapter:
    name = "claude-code"

    def __init__(self, work_tree: Path, home: Path | None = None) -> None:
        self.work_tree = work_tree
        self.home = home or Path.home()
        self.project_dir = self.home / ".claude" / "projects" / _slug(work_tree)

    def detect(self) -> bool:
        try:
            return self.project_dir.is_dir() and any(
                self.project_dir.glob("*.jsonl")
            )
        except OSError:
            return False

    def _active_transcript(self) -> Path | None:
        try:
            transcripts = sorted(
                self.project_dir.glob("*.jsonl"),
                key=lambda p: p.stat().st_mtime,
            )
        except OSError:
            return None
        return transcripts[-1] if transcripts else None

    def session_files(self) -> list[Path]:
        files: list[Path] = []
        active = self._active_transcript()
        if active is not None:
            files.append(active)
        hist = self.home / ".claude" / "history.jsonl"
        if hist.exists():
            files.append(hist)
        return files

    def snapshot(self, dest_dir: Path) -> dict:
        active = self._active_transcript()
        if active is None:
            return {}
        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(active, dest_dir / active.name)
        except OSError:
            return {}
        return {
            "uuid": active.stem,
            "transcript": active.name,
            "original_dir": str(self.project_dir),
        }

    def restore(self, meta: dict, src_dir: Path) -> bool:
        if not meta:
            return False
        saved = src_dir / meta.get("transcript", "")
        if not saved.exists():
            return False
        try:
            self.project_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(saved, self.project_dir / saved.name)
        except OSError:
            return False
        return True

    def resume_hint(self, meta: dict) -> str:
        uuid = meta.get("uuid") if meta else None
        if uuid:
            return f"claude --resume {uuid}"
        return "claude --continue"
