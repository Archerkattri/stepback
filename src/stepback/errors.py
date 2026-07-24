"""Exception types that map to clean, user-facing CLI messages.

Anything raised as a :class:`StepbackError` (or subclass) is an *expected*
failure and is printed by the CLI as a one-line message, never a traceback.
"""

from __future__ import annotations


class StepbackError(RuntimeError):
    """Base class for expected, user-facing stepback failures."""


class RestoreError(StepbackError):
    """A rewind or redo could not complete the working-tree restore."""


class BusyError(StepbackError):
    """Another stepback process holds the lock for this repository."""
