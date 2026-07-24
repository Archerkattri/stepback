"""Best-effort conversation adapter for OpenAI Codex CLI.

Codex stores rollout/session files under ``~/.codex/sessions/<YYYY>/<MM>/<DD>/``
(layout varies by version).  This adapter captures the most-recently-modified
session file anywhere under ``~/.codex/sessions`` and restores it in place.

WARNING: as with all Layer 2 adapters, the Codex on-disk session format is a
private, version-unstable internal.  This adapter is best-effort and no-ops if
the directory or files are absent.
"""

from __future__ import annotations

import shutil
from pathlib import Path


class CodexAdapter:
    name = "codex"

    def __init__(self, work_tree: Path, home: Path | None = None) -> None:
        self.work_tree = work_tree
        self.home = home or Path.home()
        self.sessions_dir = self.home / ".codex" / "sessions"

    def detect(self) -> bool:
        try:
            return self.sessions_dir.is_dir() and any(
                self.sessions_dir.rglob("*.jsonl")
            ) or (
                self.sessions_dir.is_dir()
                and any(self.sessions_dir.rglob("*.json"))
            )
        except OSError:
            return False

    def _active_session(self) -> Path | None:
        try:
            candidates = [
                p
                for pat in ("*.jsonl", "*.json")
                for p in self.sessions_dir.rglob(pat)
                if p.is_file()
            ]
        except OSError:
            return None
        if not candidates:
            return None
        return max(candidates, key=lambda p: p.stat().st_mtime)

    def session_files(self) -> list[Path]:
        active = self._active_session()
        return [active] if active is not None else []

    def snapshot(self, dest_dir: Path) -> dict:
        active = self._active_session()
        if active is None:
            return {}
        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(active, dest_dir / active.name)
        except OSError:
            return {}
        return {
            "session_file": active.name,
            # Path relative to sessions_dir so we can restore to the same spot.
            "rel_path": str(active.relative_to(self.sessions_dir)),
        }

    def restore(self, meta: dict, src_dir: Path) -> bool:
        if not meta:
            return False
        saved = src_dir / meta.get("session_file", "")
        rel = meta.get("rel_path")
        if not saved.exists() or not rel:
            return False
        target = self.sessions_dir / rel
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(saved, target)
        except OSError:
            return False
        return True

    def resume_hint(self, meta: dict) -> str:
        # Codex resume semantics vary by version; keep it generic.
        return "codex resume  # (or: codex --continue)"
