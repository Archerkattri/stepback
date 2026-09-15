"""Best-effort conversation adapter for OpenAI Codex CLI.

Codex stores rollout/session files under ``~/.codex/sessions``. The on-disk
format is private and version-unstable, so this adapter recognizes only the
``session_meta`` record with an exact ``payload.cwd`` match. It never falls
back to the newest account-wide transcript: an unsupported or ambiguous
session means file-only rewind.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import uuid
from pathlib import Path


def _canonical(path: Path) -> str:
    """Return a case-insensitive, symlink-resolved identity for a path."""
    return os.path.normcase(os.path.realpath(os.fspath(path)))


def _within(path: Path, root: Path) -> bool:
    try:
        return os.path.commonpath((_canonical(path), _canonical(root))) == _canonical(root)
    except (OSError, ValueError):
        return False


class CodexAdapter:
    name = "codex"
    _MAX_META_LINES = 64

    def __init__(
        self,
        work_tree: Path,
        home: Path | None = None,
        *,
        session_id: str | None = None,
    ) -> None:
        self.work_tree = Path(work_tree)
        self.home = home or Path.home()
        self.sessions_dir = self.home / ".codex" / "sessions"
        self.session_id = session_id

    def _files(self) -> list[Path]:
        try:
            return [
                p
                for pattern in ("*.jsonl", "*.json")
                for p in self.sessions_dir.rglob(pattern)
                if p.is_file()
            ]
        except OSError:
            return []

    def _metadata(self, path: Path) -> dict[str, str] | None:
        """Read only the bounded metadata prefix of a recognized session file."""
        try:
            with path.open("r", encoding="utf-8") as stream:
                for _ in range(self._MAX_META_LINES):
                    line = stream.readline()
                    if not line:
                        break
                    try:
                        record = json.loads(line)
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        continue
                    if not isinstance(record, dict) or record.get("type") != "session_meta":
                        continue
                    payload = record.get("payload")
                    if not isinstance(payload, dict):
                        continue
                    session_id = payload.get("id")
                    cwd = payload.get("cwd")
                    if isinstance(session_id, str) and session_id and isinstance(cwd, str) and cwd:
                        return {"id": session_id, "cwd": cwd}
        except (OSError, UnicodeError):
            return None
        return None

    def detect(self) -> bool:
        return self._active_session() is not None

    def _active_session(self) -> Path | None:
        files = self._files()
        if self.session_id is not None:
            matches = []
            for path in files:
                metadata = self._metadata(path)
                if metadata is not None and metadata["id"] == self.session_id:
                    matches.append(path)
            return matches[0] if len(matches) == 1 else None

        matching = []
        wanted = _canonical(self.work_tree)
        for path in files:
            metadata = self._metadata(path)
            if metadata is not None and _canonical(Path(metadata["cwd"])) == wanted:
                matching.append(path)
        # More than one eligible session is ambiguous; mtime is not an identity.
        return matching[0] if len(matching) == 1 else None

    def session_files(self) -> list[Path]:
        active = self._active_session()
        return [active] if active is not None else []

    def snapshot(self, dest_dir: Path) -> dict:
        active = self._active_session()
        metadata = self._metadata(active) if active is not None else None
        if active is None or metadata is None:
            return {}
        try:
            before = active.stat()
            dest_dir.mkdir(parents=True, exist_ok=True)
            destination = dest_dir / active.name
            shutil.copy2(active, destination)
            after = active.stat()
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                destination.unlink(missing_ok=True)
                return {}
        except OSError:
            return {}
        return {
            "session_file": active.name,
            "rel_path": active.relative_to(self.sessions_dir).as_posix(),
            "session_id": metadata["id"],
            "work_tree": _canonical(self.work_tree),
            "format": "session_meta-v1",
        }

    def _safe_saved(self, src_dir: Path, filename: object) -> Path | None:
        if not isinstance(filename, str) or not filename or Path(filename).name != filename:
            return None
        candidate = src_dir / filename
        return candidate if _within(candidate, src_dir) and candidate.is_file() else None

    def _safe_target(self, rel_path: object) -> Path | None:
        if not isinstance(rel_path, str) or not rel_path:
            return None
        relative = Path(rel_path)
        if relative.is_absolute() or any(part == ".." for part in relative.parts):
            return None
        target = self.sessions_dir / relative
        return target if _within(target, self.sessions_dir) else None

    def restore(self, meta: dict, src_dir: Path) -> bool:
        if not meta:
            return False
        if meta.get("work_tree") and meta["work_tree"] != _canonical(self.work_tree):
            return False
        saved = self._safe_saved(src_dir, meta.get("session_file"))
        target = self._safe_target(meta.get("rel_path"))
        if saved is None or target is None or target.name != saved.name:
            return False

        current = self._metadata(target) if target.is_file() else None
        expected_id = meta.get("session_id")
        if expected_id and (current is None or current["id"] != expected_id):
            return False

        temporary: Path | None = None
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            before = target.stat() if target.exists() else None
            descriptor, temporary_name = tempfile.mkstemp(
                dir=str(target.parent),
                prefix=f".{target.name}.stepback-",
                suffix=f"-{uuid.uuid4().hex}.tmp",
            )
            os.close(descriptor)
            temporary = Path(temporary_name)
            shutil.copy2(saved, temporary)
            after = target.stat() if target.exists() else None
            # If a live writer changed the destination during the copy, leave it
            # alone and degrade to file-only restore.
            if before is not None and after is not None and (
                before.st_size,
                before.st_mtime_ns,
            ) != (after.st_size, after.st_mtime_ns):
                return False
            os.replace(temporary, target)
            temporary = None
            return True
        except OSError:
            return False
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    def resume_hint(self, meta: dict) -> str:
        # Codex resume semantics vary by version; keep it generic.
        return "codex resume  # (or: codex --continue)"
