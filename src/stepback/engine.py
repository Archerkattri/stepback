"""The checkpoint engine: snapshot, restore, rewind, redo, list, diff.

Layer 1 (files) is implemented entirely with git plumbing against an isolated
ref namespace and a private temporary index, so it never disturbs the user's
branch, index, HEAD, or history.  Layer 2 (conversation) is delegated to
best-effort adapters and is strictly optional.
"""

from __future__ import annotations

import shutil
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .adapters import AgentAdapter, adapter_by_name, detect_adapters
from .repo import EMPTY_TREE, Repo, resolve_repo
from .store import Checkpoint, RedoEntry, State


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


@dataclass
class RestorePlan:
    """A preview of what restoring to a target tree would do."""

    target_tree: str
    stat: str          # human-readable --stat diff (working tree -> target)
    changed: int       # number of files that differ

    @property
    def is_noop(self) -> bool:
        return self.changed == 0


class Engine:
    def __init__(self, work_tree: Path, adapters: list[AgentAdapter] | None = None):
        self.repo: Repo = resolve_repo(Path(work_tree))
        self.state_path = self.repo.meta_dir / "state.json"
        self.state = State.load(self.state_path)
        self.sessions_root = self.repo.meta_dir / "sessions"
        self.adapters = adapters if adapters is not None else []

    # -- persistence --------------------------------------------------------

    def _save(self) -> None:
        self.state.save(self.state_path)

    # -- low-level tree ops -------------------------------------------------

    def _tmp_index(self) -> Path:
        return self.repo.meta_dir / f"tmp-{uuid.uuid4().hex}.index"

    def _snapshot_tree(self) -> str:
        """Stage the whole work tree into a throwaway index and write a tree.

        Respects .gitignore (via ``git add``), handles binary files, additions
        and deletions, and never touches the real index.
        """
        idx = self._tmp_index()
        try:
            self.repo.git("add", "-A", ".", index=idx)
            tree = self.repo.git("write-tree", index=idx).stdout.strip()
        finally:
            idx.unlink(missing_ok=True)
        return tree

    def _commit_tree(self, tree: str, parent: str | None, message: str) -> str:
        args = ["commit-tree", tree, "-m", message]
        if parent:
            args += ["-p", parent]
        return self.repo.git(*args, commit_identity=True).stdout.strip()

    def _ls_paths(self, tree: str) -> set[str]:
        out = self.repo.git("ls-tree", "-r", "--name-only", tree).stdout
        return {line for line in out.splitlines() if line}

    def _diff_stat(self, tree_a: str, tree_b: str) -> tuple[str, int]:
        """Return (--stat text, number of changed files) between two trees."""
        stat = self.repo.git("diff", "--stat", tree_a, tree_b).stdout.rstrip()
        names = self.repo.git(
            "diff", "--name-only", tree_a, tree_b
        ).stdout.splitlines()
        return stat, len([n for n in names if n])

    def _summary(self, parent_tree: str | None, tree: str) -> str:
        base = parent_tree or EMPTY_TREE
        out = self.repo.git("diff", "--name-status", base, tree).stdout
        added = modified = deleted = 0
        for line in out.splitlines():
            if not line:
                continue
            code = line[0]
            if code == "A":
                added += 1
            elif code == "D":
                deleted += 1
            else:
                modified += 1
        total = added + modified + deleted
        if total == 0:
            return "no file changes"
        parts = []
        if added:
            parts.append(f"+{added}")
        if modified:
            parts.append(f"~{modified}")
        if deleted:
            parts.append(f"-{deleted}")
        return f"{total} file(s) [{' '.join(parts)}]"

    def _restore_tree(self, target_tree: str) -> None:
        """Make the work tree match ``target_tree`` exactly (non-ignored files).

        Overwrites/creates every file in the tree and removes files that exist
        now but are absent from the tree.  Exact and deletion-safe.
        """
        current_paths = self._ls_paths(self._snapshot_tree())
        target_paths = self._ls_paths(target_tree)

        idx = self._tmp_index()
        try:
            self.repo.git("read-tree", target_tree, index=idx)
            self.repo.git("checkout-index", "-a", "-f", index=idx)
        finally:
            idx.unlink(missing_ok=True)

        # Remove files that existed before but are not in the target tree.
        for rel in sorted(current_paths - target_paths):
            fpath = self.repo.work_tree / rel
            try:
                fpath.unlink(missing_ok=True)
            except OSError:
                continue
            # Prune now-empty parent directories, but never above the work tree.
            parent = fpath.parent
            while parent != self.repo.work_tree and parent.is_dir():
                try:
                    next(parent.iterdir())
                    break  # not empty
                except StopIteration:
                    parent.rmdir()
                    parent = parent.parent
                except OSError:
                    break

    # -- adapter (Layer 2) helpers -----------------------------------------

    def _session_dir(self, tag: str) -> Path:
        d = self.sessions_root / tag
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _snapshot_adapters(self, tag: str) -> tuple[dict, str | None]:
        """Snapshot every active adapter's session state under ``tag``."""
        if not self.adapters:
            return {}, None
        meta: dict = {}
        base = self._session_dir(tag)
        for adapter in self.adapters:
            try:
                a_meta = adapter.snapshot(base / adapter.name)
            except Exception:
                a_meta = {}
            if a_meta:
                meta[adapter.name] = a_meta
        return meta, (tag if meta else None)

    def _restore_adapters(self, meta: dict, session_dir: str | None) -> list[str]:
        """Restore adapter sessions; return resume hints for those restored."""
        hints: list[str] = []
        if not meta or not session_dir:
            return hints
        base = self.sessions_root / session_dir
        configured = {a.name: a for a in self.adapters}
        for name, a_meta in meta.items():
            # Prefer the engine's own configured adapter instance (it carries the
            # right config, e.g. HOME); fall back to constructing a fresh one.
            adapter = configured.get(name) or adapter_by_name(name, self.repo.work_tree)
            if adapter is None:
                continue
            try:
                if adapter.restore(a_meta, base / name):
                    hints.append(adapter.resume_hint(a_meta))
            except Exception:
                continue
        return hints

    # -- public API ---------------------------------------------------------

    def start_session(self) -> str:
        session = "s" + uuid.uuid4().hex[:8]
        self.state.current_session = session
        self._save()
        return session

    def checkpoint(self, label: str | None = None, session: str | None = None) -> Checkpoint | None:
        """Take a checkpoint of the current work tree (+ adapter sessions).

        Returns None if nothing changed since the previous checkpoint (dedup by
        tree SHA), so an idle burst does not create noise.
        """
        session = session or self.state.current_session or self.start_session()
        tree = self._snapshot_tree()

        prev = self.state.latest()
        if prev is not None and prev.tree == tree:
            return None  # nothing changed

        n = sum(1 for c in self.state.checkpoints if c.session == session)
        parent = prev.commit if prev else None
        message = label or f"checkpoint {n} @ {_now_iso()}"
        commit = self._commit_tree(tree, parent, message)
        self.repo.git("update-ref", f"refs/checkpoints/{session}/{n}", commit)

        cid = self.state.next_id()
        adapters_meta, session_dir = self._snapshot_adapters(f"cp-{cid:04d}")
        cp = Checkpoint(
            id=cid,
            session=session,
            n=n,
            tree=tree,
            commit=commit,
            parent=prev.tree if prev else None,
            time=_now_iso(),
            summary=self._summary(prev.tree if prev else None, tree),
            adapters=adapters_meta,
            session_dir=session_dir,
        )
        self.state.checkpoints.append(cp)
        # A fresh checkpoint invalidates the redo stack (new timeline branch).
        self.state.redo.clear()
        self._save()
        return cp

    def plan_restore(self, target_tree: str) -> RestorePlan:
        current_tree = self._snapshot_tree()
        stat, changed = self._diff_stat(current_tree, target_tree)
        return RestorePlan(target_tree=target_tree, stat=stat, changed=changed)

    def rewind(self, checkpoint: Checkpoint) -> list[str]:
        """Restore to ``checkpoint``, pushing current state onto the redo stack.

        Returns resume hints from any conversation adapters that were restored.
        """
        # 1. Preserve current state (files + sessions) for redo.
        current_tree = self._snapshot_tree()
        current_commit = self._commit_tree(
            current_tree, None, f"pre-rewind @ {_now_iso()}"
        )
        redo_tag = f"redo-{uuid.uuid4().hex[:8]}"
        redo_meta, redo_dir = self._snapshot_adapters(redo_tag)
        self.state.redo.append(
            RedoEntry(
                tree=current_tree,
                commit=current_commit,
                time=_now_iso(),
                adapters=redo_meta,
                session_dir=redo_dir,
            )
        )
        # 2. Restore files, then conversation state.
        self._restore_tree(checkpoint.tree)
        hints = self._restore_adapters(checkpoint.adapters, checkpoint.session_dir)
        self._save()
        return hints

    def redo(self) -> tuple[RedoEntry | None, list[str]]:
        """Reverse the most recent rewind."""
        if not self.state.redo:
            return None, []
        entry = self.state.redo.pop()
        self._restore_tree(entry.tree)
        hints = self._restore_adapters(entry.adapters, entry.session_dir)
        self._save()
        return entry, hints

    def diff(self, checkpoint: Checkpoint) -> str:
        """Return the patch a checkpoint introduced (vs its parent)."""
        base = checkpoint.parent or EMPTY_TREE
        return self.repo.git("diff", base, checkpoint.tree).stdout

    def diff_working(self, checkpoint: Checkpoint) -> str:
        """Return the patch between the current work tree and a checkpoint."""
        current = self._snapshot_tree()
        return self.repo.git("diff", current, checkpoint.tree).stdout
