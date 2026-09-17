"""Crash-recovery journal for file restore operations.

The journal is deliberately small and file-focused.  It records enough state
to restore the pre-operation tree after a process dies between filesystem
replacements.  Conversation adapters are outside this contract and remain
best-effort.
"""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class OperationJournal:
    operation_id: str
    operation: str
    phase: str
    started_at: str
    pre_tree: str
    target_tree: str
    selected_paths: tuple[str, ...] = field(default_factory=tuple)
    recovery_ref: str | None = None
    error: str | None = None

    @classmethod
    def begin(
        cls,
        operation: str,
        *,
        started_at: str,
        pre_tree: str,
        target_tree: str,
        selected_paths: tuple[str, ...] = (),
    ) -> OperationJournal:
        return cls(
            operation_id=uuid.uuid4().hex,
            operation=operation,
            phase="prepared",
            started_at=started_at,
            pre_tree=pre_tree,
            target_tree=target_tree,
            selected_paths=tuple(selected_paths),
        )

    @classmethod
    def load(cls, path: Path) -> OperationJournal | None:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                return None
            paths = raw.get("selected_paths", ())
            if not isinstance(paths, (list, tuple)):
                return None
            return cls(
                operation_id=str(raw["operation_id"]),
                operation=str(raw["operation"]),
                phase=str(raw["phase"]),
                started_at=str(raw["started_at"]),
                pre_tree=str(raw["pre_tree"]),
                target_tree=str(raw["target_tree"]),
                selected_paths=tuple(str(p) for p in paths),
                recovery_ref=(str(raw["recovery_ref"]) if raw.get("recovery_ref") else None),
                error=(str(raw["error"]) if raw.get("error") else None),
            )
        except (OSError, ValueError, TypeError, KeyError):
            return None

    def save(self, path: Path) -> None:
        data = asdict(self)
        data["selected_paths"] = list(self.selected_paths)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            dir=str(path.parent), prefix=f".{path.name}.{self.operation_id}.", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def advance(self, path: Path, phase: str, *, error: str | None = None) -> None:
        self.phase = phase
        self.error = error
        self.save(path)

    @staticmethod
    def clear(path: Path) -> None:
        path.unlink(missing_ok=True)
