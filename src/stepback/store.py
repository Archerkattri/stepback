"""Persistent stepback state: the checkpoint log and the redo stack.

State lives in ``<git_dir>/stepback/state.json`` and is written atomically
(temp file + rename) so a crash or a flaky filesystem can never leave a
half-written, unparseable state file.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path


@dataclass
class Checkpoint:
    id: int
    session: str
    n: int
    tree: str
    commit: str
    parent: str | None
    time: str
    summary: str
    # Per-adapter conversation-snapshot metadata, keyed by adapter name.
    adapters: dict = field(default_factory=dict)
    # Sub-directory (relative to sessions/) holding conversation snapshots.
    session_dir: str | None = None

    @property
    def ref(self) -> str:
        return f"refs/checkpoints/{self.session}/{self.n}"


@dataclass
class RedoEntry:
    tree: str
    commit: str
    time: str
    adapters: dict = field(default_factory=dict)
    session_dir: str | None = None
    # Ref protecting the pre-rewind commit from the user's own ``git gc``.
    ref: str | None = None


@dataclass
class State:
    counter: int = 0
    current_session: str | None = None
    checkpoints: list[Checkpoint] = field(default_factory=list)
    redo: list[RedoEntry] = field(default_factory=list)

    # -- serialization ------------------------------------------------------

    @classmethod
    def load(cls, path: Path) -> State:
        if not path.exists():
            return cls()
        try:
            raw = json.loads(path.read_text())
            if not isinstance(raw, dict):
                raise ValueError("state root is not an object")
            return cls(
                counter=int(raw.get("counter", 0)),
                current_session=raw.get("current_session"),
                checkpoints=[
                    _load_checkpoint(c) for c in raw.get("checkpoints", []) or []
                ],
                redo=[_load_redo(r) for r in raw.get("redo", []) or []],
            )
        except (json.JSONDecodeError, OSError, ValueError, TypeError):
            # Corrupt or partially written state: preserve it for forensics
            # rather than silently discarding the checkpoint log, then start
            # from a clean slate so the tool keeps working.
            _quarantine_corrupt(path)
            return cls()

    def save(self, path: Path) -> None:
        data = {
            "counter": self.counter,
            "current_session": self.current_session,
            "checkpoints": [asdict(c) for c in self.checkpoints],
            "redo": [asdict(r) for r in self.redo],
        }
        _atomic_write(path, json.dumps(data, indent=2))

    # -- helpers ------------------------------------------------------------

    def by_id(self, cid: int) -> Checkpoint | None:
        for c in self.checkpoints:
            if c.id == cid:
                return c
        return None

    def latest(self) -> Checkpoint | None:
        return self.checkpoints[-1] if self.checkpoints else None

    def next_id(self) -> int:
        self.counter += 1
        return self.counter


def _known_fields(cls) -> set[str]:
    return {f.name for f in fields(cls)}


def _load_checkpoint(raw: dict) -> Checkpoint:
    data = {k: v for k, v in raw.items() if k in _known_fields(Checkpoint)}
    return Checkpoint(**data)


def _load_redo(raw: dict) -> RedoEntry:
    data = {k: v for k, v in raw.items() if k in _known_fields(RedoEntry)}
    return RedoEntry(**data)


def _quarantine_corrupt(path: Path) -> None:
    """Move an unreadable state file aside so it is never silently overwritten."""
    try:
        stamp = int(time.time())
        path.replace(path.with_suffix(path.suffix + f".corrupt-{stamp}"))
    except OSError:
        pass


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".state-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
