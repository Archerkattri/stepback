"""stepback — git time-travel for any AI coding agent.

Undo a bad Codex/Claude/aider session in one command, no matter which tool made
the mess.  Layer 1 checkpoints the *files*; Layer 2 (best-effort) checkpoints
the *conversation* too.
"""

from importlib.metadata import PackageNotFoundError, version

from .engine import Engine
from .repo import Repo, resolve_repo

try:
    # Installed distributions are authoritative.  The literal is an
    # intentional source-tree fallback for editable/check-out execution before
    # package metadata exists.
    __version__ = version("stepback")
except PackageNotFoundError:
    __version__ = "0.1.2"
__all__ = ["Engine", "Repo", "resolve_repo", "__version__"]
