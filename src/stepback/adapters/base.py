"""The AgentAdapter interface — the clean seam behind which the fragile,
format-unstable Layer 2 (conversation rewind) is contained.

The file checkpoint engine (Layer 1) works with **zero** adapters.  A conversation
adapter is an optional bolt-on that snapshots and restores an agent's on-disk
session state alongside each file snapshot.  Every adapter is best-effort: if the
agent is not detected or its session format is not recognized, the adapter simply
does nothing and stepback degrades cleanly to file-only rewind.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable


@runtime_checkable
class AgentAdapter(Protocol):
    """Snapshots/restores a single AI coding agent's on-disk session state.

    Implementations MUST be defensive: any method may be called even when the
    agent is not installed or its files are missing, and must never raise.
    """

    name: str

    def detect(self) -> bool:
        """Return True if this agent appears to be active for the work tree."""
        ...

    def session_files(self) -> list[Path]:
        """On-disk files that make up the agent's current session state."""
        ...

    def snapshot(self, dest_dir: Path) -> dict:
        """Copy current session state into ``dest_dir``; return metadata.

        The returned dict is opaque to the engine and is handed back verbatim to
        :meth:`restore` and :meth:`resume_hint`.  Return ``{}`` if nothing could
        be captured.
        """
        ...

    def restore(self, meta: dict, src_dir: Path) -> bool:
        """Restore session state previously written into ``src_dir``.

        Return True if something was restored, False otherwise.  Must not raise.
        """
        ...

    def resume_hint(self, meta: dict) -> str:
        """A human-facing command to resume the restored conversation."""
        ...
