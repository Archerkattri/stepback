"""Git-backed repository resolution and a safe git command runner.

stepback stores every checkpoint as a git tree/commit under an isolated ref
namespace (``refs/checkpoints/...``).  It NEVER touches the user's branch,
index, HEAD, or normal history:

* In a real git repo ("shared" mode) the user's own object database is reused
  for storage (cheap, content-addressed, deduplicated against existing blobs)
  but all refs live under ``refs/checkpoints/`` and every plumbing call is
  driven with a private temporary index via ``GIT_INDEX_FILE`` so the real
  ``.git/index`` is never written.
* Outside a git repo ("shadow" mode) a private object database is created at
  ``<workdir>/.stepback/shadow.git`` and used the exact same way.

Only two things differ between the two modes: the ``git_dir`` used for object
storage and the location of stepback's own metadata.  All plumbing is identical.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

# The well-known SHA-1 of git's empty tree object; used as a diff base.
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"

# Config overrides applied to every git invocation so behaviour is deterministic
# and byte-exact regardless of the user's global git config:
#   core.bare=false     -> allow --work-tree operations against a bare shadow dir
#   gc.auto=0           -> never auto-gc mid-operation (our refs protect objects)
#   core.autocrlf=false -> restore bytes exactly, no line-ending translation
_SAFE_CONFIG = [
    "-c", "core.bare=false",
    "-c", "gc.auto=0",
    "-c", "core.autocrlf=false",
    "-c", "core.fileMode=true",
]

# Identity used for checkpoint commits.  Set explicitly so checkpoints work even
# in repos where the user has not configured user.name/user.email.
_COMMIT_ENV = {
    "GIT_AUTHOR_NAME": "stepback",
    "GIT_AUTHOR_EMAIL": "stepback@localhost",
    "GIT_COMMITTER_NAME": "stepback",
    "GIT_COMMITTER_EMAIL": "stepback@localhost",
}


class GitError(RuntimeError):
    """Raised when an underlying git plumbing command fails."""


@dataclass
class Repo:
    """A resolved storage target for checkpoints.

    Attributes:
        work_tree: The directory whose files are checkpointed.
        git_dir: The git object database used to store checkpoints.
        mode: ``"shared"`` (reusing a real repo) or ``"shadow"`` (private store).
    """

    work_tree: Path
    git_dir: Path
    mode: str

    @property
    def meta_dir(self) -> Path:
        """Directory holding stepback's own state, invisible to snapshots.

        It lives *inside* ``git_dir`` so git never descends into it when adding
        working-tree files (git always ignores its own git directory).
        """
        d = self.git_dir / "stepback"
        d.mkdir(parents=True, exist_ok=True)
        return d

    # -- git command runner -------------------------------------------------

    def git(
        self,
        *args: str,
        index: Path | None = None,
        commit_identity: bool = False,
        check: bool = True,
        text_input: str | None = None,
    ) -> subprocess.CompletedProcess:
        """Run a git command scoped to this repo.

        Args:
            args: git subcommand and arguments.
            index: if given, used as ``GIT_INDEX_FILE`` so the real index is
                never touched.
            commit_identity: if True, inject the stepback commit identity env.
            check: raise :class:`GitError` on non-zero exit.
            text_input: fed to the process stdin.
        """
        cmd = [
            "git",
            *_SAFE_CONFIG,
            "--git-dir",
            str(self.git_dir),
            "--work-tree",
            str(self.work_tree),
            *args,
        ]
        env = dict(os.environ)
        # Keep git from picking up an inherited GIT_INDEX_FILE from a parent
        # (e.g. when stepback itself is launched from inside a git hook).
        env.pop("GIT_INDEX_FILE", None)
        if index is not None:
            env["GIT_INDEX_FILE"] = str(index)
        if commit_identity:
            env.update(_COMMIT_ENV)
        proc = subprocess.run(
            cmd,
            cwd=str(self.work_tree),
            env=env,
            input=text_input,
            capture_output=True,
            text=True,
        )
        if check and proc.returncode != 0:
            raise GitError(
                f"git {' '.join(args)} failed ({proc.returncode}): {proc.stderr.strip()}"
            )
        return proc


def _run(cmd: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True)


def resolve_repo(path: Path) -> Repo:
    """Resolve the checkpoint storage target for ``path``.

    If ``path`` is inside a git work tree, checkpoints are stored in that repo's
    object database under ``refs/checkpoints/`` (shared mode).  Otherwise a
    private shadow object database is created (shadow mode).
    """
    path = path.resolve()

    top = _run(["git", "rev-parse", "--show-toplevel"], path)
    if top.returncode == 0 and top.stdout.strip():
        work_tree = Path(top.stdout.strip())
        gd = _run(["git", "rev-parse", "--absolute-git-dir"], work_tree)
        git_dir = Path(gd.stdout.strip())
        return Repo(work_tree=work_tree, git_dir=git_dir, mode="shared")

    # Shadow mode: private object DB under .stepback/shadow.git
    work_tree = path
    git_dir = work_tree / ".stepback" / "shadow.git"
    if not (git_dir / "HEAD").exists():
        git_dir.parent.mkdir(parents=True, exist_ok=True)
        init = _run(["git", "init", "--bare", "--quiet", str(git_dir)], work_tree)
        if init.returncode != 0:
            raise GitError(f"could not init shadow repo: {init.stderr.strip()}")
    # Never let the shadow store snapshot itself.
    info = git_dir / "info"
    info.mkdir(exist_ok=True)
    exclude = info / "exclude"
    lines = set()
    if exclude.exists():
        lines = set(exclude.read_text().splitlines())
    lines.update([".stepback/", ".stepback"])
    exclude.write_text("\n".join(sorted(lines)) + "\n")
    return Repo(work_tree=work_tree, git_dir=git_dir, mode="shadow")
