"""stepback — git time-travel for any AI coding agent.

Undo a bad Codex/Claude/aider session in one command, no matter which tool made
the mess.  Layer 1 checkpoints the *files*; Layer 2 (best-effort) checkpoints
the *conversation* too.
"""

from .engine import Engine
from .repo import Repo, resolve_repo

__version__ = "0.1.0"
__all__ = ["Engine", "Repo", "resolve_repo", "__version__"]
