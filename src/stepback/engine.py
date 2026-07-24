"""The checkpoint engine: snapshot, restore, rewind, redo, list, diff.

Layer 1 (files) is implemented entirely with git plumbing against an isolated
ref namespace and a private temporary index, so it never disturbs the user's
branch, index, HEAD, or history.  Layer 2 (conversation) is delegated to
best-effort adapters and is strictly optional.
"""

from __future__ import annotations

import os
import shutil
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .adapters import AgentAdapter, adapter_by_name
from .errors import RestoreError
from .lock import file_lock
from .repo import EMPTY_TREE, GitError, Repo, resolve_repo
from .store import Checkpoint, RedoEntry, State

# How long, after a rewind/redo, the watcher should ignore the filesystem events
# caused by rewriting the tree (so a restore does not immediately checkpoint
# itself and clear the redo stack).
_PAUSE_SECONDS = 3.0


def _now_iso() -> str:
    return datetime.now(UTC).astimezone().isoformat(timespec="seconds")


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
        self.pause_path = self.repo.meta_dir / "paused"
        self.adapters = adapters if adapters is not None else []

    # -- persistence --------------------------------------------------------

    def _save(self) -> None:
        self.state.save(self.state_path)

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        """Hold the repo lock and work on a fresh view of on-disk state.

        Guards every mutating operation so a running watcher and a manual
        command in another terminal can never interleave their state writes or
        their working-tree changes.
        """
        with file_lock(self.repo.lock_path):
            # Re-read state under the lock: another process may have appended
            # checkpoints since this Engine was constructed.
            self.state = State.load(self.state_path)
            yield

    # -- watcher pause (cross-process) -------------------------------------

    def pause(self, seconds: float = _PAUSE_SECONDS) -> None:
        """Ask any running watcher to ignore edits until ``seconds`` from now."""
        try:
            deadline = time.time() + seconds
            self.pause_path.write_text(f"{deadline:.3f}")
        except OSError:
            pass

    def is_paused(self) -> bool:
        """True if a rewind/redo recently asked the watcher to hold off."""
        try:
            return time.time() < float(self.pause_path.read_text().strip())
        except (OSError, ValueError):
            return False

    # -- watcher heartbeat (for `status`) ----------------------------------

    @property
    def _watch_pid_path(self) -> Path:
        return self.repo.meta_dir / "watcher.pid"

    def mark_watching(self) -> None:
        try:
            self._watch_pid_path.write_text(f"{os.getpid()} {_now_iso()}")
        except OSError:
            pass

    def clear_watching(self) -> None:
        try:
            self._watch_pid_path.unlink(missing_ok=True)
        except OSError:
            pass

    def watcher_status(self) -> str | None:
        """A short description of a live watcher process, or None if none."""
        try:
            raw = self._watch_pid_path.read_text().strip().split(maxsplit=1)
        except OSError:
            return None
        if not raw:
            return None
        try:
            pid = int(raw[0])
        except ValueError:
            return None
        try:
            os.kill(pid, 0)  # signal 0: liveness probe, does not kill
        except ProcessLookupError:
            self.clear_watching()
            return None
        except (PermissionError, OSError):
            pass  # exists but owned by another user; treat as alive
        since = raw[1] if len(raw) > 1 else "?"
        return f"running (pid {pid}, since {since})"

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
        # -z + core.quotePath=false: raw NUL-separated names, so paths with
        # spaces, unicode, or a leading dash round-trip exactly.
        out = self.repo.git("ls-tree", "-r", "-z", "--name-only", tree).stdout
        return {p for p in out.split("\0") if p}

    def _changed_paths(self, tree_a: str, tree_b: str) -> set[str]:
        """Paths that differ between two trees (added, modified, or deleted)."""
        out = self.repo.git("diff", "-z", "--name-only", tree_a, tree_b).stdout
        return {p for p in out.split("\0") if p}

    def _diff_stat(self, tree_a: str, tree_b: str) -> tuple[str, int]:
        """Return (--stat text, number of changed files) between two trees."""
        stat = self.repo.git("diff", "--stat", tree_a, tree_b).stdout.rstrip()
        return stat, len(self._changed_paths(tree_a, tree_b))

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

        Only files that actually differ are touched.  Every modified/added file
        is written to a staging area first and then moved into place with an
        atomic same-filesystem rename, so a crash can never leave a half-written
        file.  Files present now but absent from the target are removed.  The
        whole operation is crash-recoverable via the redo stack (the caller
        durably records the pre-restore state before calling this).
        """
        idx = self._tmp_index()
        stage = self.repo.meta_dir / f"restore-{uuid.uuid4().hex}"
        try:
            current_tree = self._snapshot_tree()
            if current_tree == target_tree:
                return  # already identical, nothing to touch

            current_paths = self._ls_paths(current_tree)
            target_paths = self._ls_paths(target_tree)
            changed = self._changed_paths(current_tree, target_tree)

            to_write = sorted(changed & target_paths)
            to_delete = sorted(current_paths - target_paths)

            # 1. Materialise the wanted versions of changed files into a staging
            #    dir, fully, before touching the real tree.  If any of this
            #    fails, the real tree is still untouched.
            if to_write:
                self.repo.git("read-tree", target_tree, index=idx)
                stage.mkdir(parents=True, exist_ok=True)
                self.repo.git(
                    "checkout-index",
                    "-f",
                    f"--prefix={stage}{os.sep}",
                    "--",
                    *to_write,
                    index=idx,
                )

            # 2. Deletions first, so a file -> directory transition (or the
            #    reverse) never collides at the same path.
            for rel in to_delete:
                self._remove_path(self.repo.work_tree / rel)

            # 3. Promote staged files into place (atomic per file).
            same_device = self._same_device(stage)
            for rel in to_write:
                self._promote(stage / rel, self.repo.work_tree / rel, same_device)
        except GitError as exc:
            raise RestoreError(f"could not stage restore: {exc}") from exc
        finally:
            idx.unlink(missing_ok=True)
            shutil.rmtree(stage, ignore_errors=True)

    def _same_device(self, stage: Path) -> bool:
        try:
            stage.mkdir(parents=True, exist_ok=True)
            return stage.stat().st_dev == self.repo.work_tree.stat().st_dev
        except OSError:
            return False

    def _promote(self, src: Path, dst: Path, same_device: bool) -> None:
        """Move a staged file over its destination, atomically when possible."""
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
        except (OSError, FileExistsError):
            # A parent path component is a file left over from a prior state;
            # remove it so the directory can be created.
            self._clear_conflicting_parents(dst)
            dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            if same_device:
                os.replace(src, dst)
            else:
                if dst.is_dir() and not dst.is_symlink():
                    shutil.rmtree(dst, ignore_errors=True)
                shutil.copy2(src, dst, follow_symlinks=False)
        except OSError as exc:
            raise RestoreError(f"could not restore {dst}: {exc}") from exc

    def _clear_conflicting_parents(self, dst: Path) -> None:
        parent = dst.parent
        while parent != self.repo.work_tree and parent != parent.parent:
            if parent.exists() and not parent.is_dir():
                parent.unlink(missing_ok=True)
                return
            parent = parent.parent

    def _remove_path(self, fpath: Path) -> None:
        try:
            if fpath.is_dir() and not fpath.is_symlink():
                shutil.rmtree(fpath, ignore_errors=True)
            else:
                fpath.unlink(missing_ok=True)
        except OSError:
            return
        # Prune now-empty parent directories, but never above the work tree.
        parent = fpath.parent
        while parent != self.repo.work_tree and parent.is_dir():
            try:
                next(parent.iterdir())
                break  # not empty
            except StopIteration:
                try:
                    parent.rmdir()
                except OSError:
                    break
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
        with self._transaction():
            session = "s" + uuid.uuid4().hex[:8]
            self.state.current_session = session
            self._save()
        return session

    def checkpoint(self, label: str | None = None, session: str | None = None) -> Checkpoint | None:
        """Take a checkpoint of the current work tree (+ adapter sessions).

        Returns None if nothing changed since the previous checkpoint (dedup by
        tree SHA), so an idle burst does not create noise.
        """
        with self._transaction():
            session = session or self.state.current_session
            if session is None:
                session = "s" + uuid.uuid4().hex[:8]
                self.state.current_session = session
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
            self._clear_redo_refs()
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
        The pre-rewind state is committed, referenced, and saved to the redo
        stack *before* the destructive restore, so a crash mid-restore is always
        recoverable with ``stepback redo``.
        """
        # Tell any running watcher to ignore the events this restore will cause.
        self.pause()
        with self._transaction():
            # 1. Preserve current state (files + sessions) for redo, durably.
            token = uuid.uuid4().hex[:8]
            current_tree = self._snapshot_tree()
            current_commit = self._commit_tree(
                current_tree, None, f"pre-rewind @ {_now_iso()}"
            )
            # A ref keeps the pre-rewind commit safe from the user's own git gc.
            redo_ref = f"refs/checkpoints/_redo/{token}"
            self.repo.git("update-ref", redo_ref, current_commit)
            redo_meta, redo_dir = self._snapshot_adapters(f"redo-{token}")
            self.state.redo.append(
                RedoEntry(
                    tree=current_tree,
                    commit=current_commit,
                    time=_now_iso(),
                    adapters=redo_meta,
                    session_dir=redo_dir,
                    ref=redo_ref,
                )
            )
            self._save()  # redo is durable before we touch the tree

            # 2. Restore files, then conversation state.
            self._restore_tree(checkpoint.tree)
            hints = self._restore_adapters(checkpoint.adapters, checkpoint.session_dir)
            self._save()
        self.pause()  # cover trailing filesystem events from the restore
        return hints

    def redo(self) -> tuple[RedoEntry | None, list[str]]:
        """Reverse the most recent rewind."""
        self.pause()
        with self._transaction():
            if not self.state.redo:
                return None, []
            entry = self.state.redo.pop()
            self._restore_tree(entry.tree)
            hints = self._restore_adapters(entry.adapters, entry.session_dir)
            if entry.ref:
                self.repo.git("update-ref", "-d", entry.ref, check=False)
            self._save()
        self.pause()
        return entry, hints

    def _clear_redo_refs(self) -> None:
        for entry in self.state.redo:
            if entry.ref:
                self.repo.git("update-ref", "-d", entry.ref, check=False)

    def diff(self, checkpoint: Checkpoint) -> str:
        """Return the patch a checkpoint introduced (vs its parent)."""
        base = checkpoint.parent or EMPTY_TREE
        return self.repo.git("diff", base, checkpoint.tree).stdout

    def diff_working(self, checkpoint: Checkpoint) -> str:
        """Return the patch between the current work tree and a checkpoint."""
        current = self._snapshot_tree()
        return self.repo.git("diff", current, checkpoint.tree).stdout
