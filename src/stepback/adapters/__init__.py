"""Agent adapters for Layer 2 (conversation rewind).

Adapters are discovered here.  The file engine works with zero adapters; these
are optional, best-effort integrations that snapshot/restore an agent's on-disk
session state alongside each file checkpoint.
"""

from __future__ import annotations

from pathlib import Path

from .base import AgentAdapter
from .claude_code import ClaudeCodeAdapter
from .codex import CodexAdapter

# Registry of all known adapter classes.
ALL_ADAPTERS = [ClaudeCodeAdapter, CodexAdapter]


def detect_adapters(work_tree: Path) -> list[AgentAdapter]:
    """Return instances of every adapter that detects an active session.

    Detection never raises; a misbehaving adapter is simply skipped.
    """
    active: list[AgentAdapter] = []
    for cls in ALL_ADAPTERS:
        try:
            adapter = cls(work_tree)
            if adapter.detect():
                active.append(adapter)
        except Exception:
            continue
    return active


def adapter_by_name(name: str, work_tree: Path) -> AgentAdapter | None:
    for cls in ALL_ADAPTERS:
        try:
            inst = cls(work_tree)
            if inst.name == name:
                return inst
        except Exception:
            continue
    return None


__all__ = [
    "AgentAdapter",
    "ClaudeCodeAdapter",
    "CodexAdapter",
    "ALL_ADAPTERS",
    "detect_adapters",
    "adapter_by_name",
]
