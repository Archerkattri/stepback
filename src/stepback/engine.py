"""The checkpoint engine: snapshot, restore, rewind, redo, list, diff.

Layer 1 (files) is implemented entirely with git plumbing against an isolated
ref namespace and a private temporary index, so it never disturbs the user's
branch, index, HEAD, or history.  Layer 2 (conversation) is delegated to
best-effort adapters and is strictly optional.
"""

from __future__ import annotations

import json
import os
import posixpath
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .adapters import AgentAdapter, adapter_by_name
from .errors import RestoreError
from .journal import OperationJournal
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


@dataclass
class SelectiveRestorePlan:
    """A preview of restoring only selected paths from a checkpoint."""

    target_tree: str
    paths: tuple[str, ...]
    stat: str
    changed: int

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
        self.restore_path = self.repo.meta_dir / "restore.pid"
        self.journal_path = self.repo.meta_dir / "operation.json"
        self._watch_marker: str | None = None
        self._restore_marker: str | None = None
        self.adapters = adapters if adapters is not None else []

    # -- persistence --------------------------------------------------------

    def _save(self) -> None:
        self.state.save(self.state_path)

    def pending_operation(self) -> OperationJournal | None:
        """Return an unfinished file-restore operation, if one is recorded."""
        return OperationJournal.load(self.journal_path)

    def _begin_journal(
        self,
        operation: str,
        *,
        pre_tree: str,
        target_tree: str,
        selected_paths: tuple[str, ...] = (),
    ) -> OperationJournal:
        journal = OperationJournal.begin(
            operation,
            started_at=_now_iso(),
            pre_tree=pre_tree,
            target_tree=target_tree,
            selected_paths=selected_paths,
        )
        journal.save(self.journal_path)
        return journal

    def _finish_journal(self, journal: OperationJournal) -> None:
        OperationJournal.clear(self.journal_path)

    def recover_pending(self) -> OperationJournal | None:
        """Restore the pre-operation file tree recorded by a stale journal.

        This is intentionally an explicit command: an interrupted process must
        not silently rewrite user files when a new StepBack process starts.
        """
        self._begin_restore()
        self.pause()
        try:
            with self._transaction():
                journal = OperationJournal.load(self.journal_path)
                if journal is None:
                    return None
                try:
                    if journal.selected_paths:
                        self._restore_tree_paths(journal.pre_tree, journal.selected_paths)
                    else:
                        self._restore_tree(journal.pre_tree)
                    journal.advance(self.journal_path, "recovered")
                    if journal.recovery_ref:
                        self.repo.git("update-ref", "-d", journal.recovery_ref, check=False)
                    OperationJournal.clear(self.journal_path)
                    self._save()
                    return journal
                except Exception as exc:
                    journal.advance(self.journal_path, "recovery-failed", error=f"{type(exc).__name__}: {exc}")
                    raise
        finally:
            self._end_restore()
            self.pause()

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

    def _process_identity(self, pid: int) -> str | None:
        """Return a process-start identity where the platform exposes one."""
        if sys.platform == "win32":
            # QueryLimitedInformation/GetProcessTimes are read-only and do not
            # send a signal.  The creation time also lets status reject PID
            # reuse instead of mistaking a new process for our watcher.
            try:
                import ctypes
                from ctypes import wintypes

                kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
                kernel32.OpenProcess.argtypes = [
                    wintypes.DWORD,
                    wintypes.BOOL,
                    wintypes.DWORD,
                ]
                kernel32.OpenProcess.restype = wintypes.HANDLE
                kernel32.GetProcessTimes.argtypes = [
                    wintypes.HANDLE,
                    ctypes.POINTER(wintypes.FILETIME),
                    ctypes.POINTER(wintypes.FILETIME),
                    ctypes.POINTER(wintypes.FILETIME),
                    ctypes.POINTER(wintypes.FILETIME),
                ]
                kernel32.GetProcessTimes.restype = wintypes.BOOL
                kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
                kernel32.CloseHandle.restype = wintypes.BOOL
                handle = kernel32.OpenProcess(0x1000, False, pid)
                if not handle:
                    return None
                try:
                    created = wintypes.FILETIME()
                    exited = wintypes.FILETIME()
                    kernel = wintypes.FILETIME()
                    user = wintypes.FILETIME()
                    if not kernel32.GetProcessTimes(
                        handle,
                        ctypes.byref(created),
                        ctypes.byref(exited),
                        ctypes.byref(kernel),
                        ctypes.byref(user),
                    ):
                        return None
                    return f"{created.dwHighDateTime:08x}{created.dwLowDateTime:08x}"
                finally:
                    kernel32.CloseHandle(handle)
            except (OSError, AttributeError):
                return None

        # macOS has no /proc: use the read-only `ps` start time as the
        # identity.  Like the Linux tick below, it is only ever compared
        # for equality, so its exact format does not matter.
        if sys.platform == "darwin":
            try:
                proc = subprocess.run(
                    ["ps", "-o", "lstart=", "-p", str(pid)],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                start = proc.stdout.strip()
                return start or None
            except (OSError, subprocess.SubprocessError):
                return None

        # Linux exposes a monotonic process start tick in /proc.
        try:
            stat = Path(f"/proc/{pid}/stat").read_text()
            rest = stat[stat.rfind(")") + 2 :].split()
            return rest[19] if len(rest) > 19 else None
        except (OSError, ValueError):
            return None

    def _process_alive(self, pid: int) -> bool | None:
        """Read process liveness without sending a signal on Windows."""
        if sys.platform == "win32":
            try:
                import ctypes
                from ctypes import wintypes

                kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
                kernel32.OpenProcess.argtypes = [
                    wintypes.DWORD,
                    wintypes.BOOL,
                    wintypes.DWORD,
                ]
                kernel32.OpenProcess.restype = wintypes.HANDLE
                kernel32.GetExitCodeProcess.argtypes = [
                    wintypes.HANDLE,
                    ctypes.POINTER(ctypes.c_ulong),
                ]
                kernel32.GetExitCodeProcess.restype = wintypes.BOOL
                kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
                kernel32.CloseHandle.restype = wintypes.BOOL
                handle = kernel32.OpenProcess(0x1000, False, pid)
                if not handle:
                    error = ctypes.get_last_error()
                    # ERROR_INVALID_PARAMETER means the PID does not exist;
                    # access denied is unknown, not proof of a dead process.
                    return False if error == 87 else None
                try:
                    code = ctypes.c_ulong()
                    if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                        return None
                    return code.value == 259  # STILL_ACTIVE
                finally:
                    kernel32.CloseHandle(handle)
            except (OSError, AttributeError):
                return None

        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return None
        except OSError:
            return None
        return True

    def _write_owner_marker(self, path: Path, marker: str) -> None:
        """Publish a marker atomically so readers never see a partial line."""
        tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            tmp.write_text(marker)
            os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)

    def _begin_restore(self) -> None:
        pid = os.getpid()
        identity = self._process_identity(pid) or "-"
        self._restore_marker = f"{pid} {identity}"
        try:
            self._write_owner_marker(self.restore_path, self._restore_marker)
        except OSError:
            self._restore_marker = None

    def _end_restore(self) -> None:
        marker = self._restore_marker
        self._restore_marker = None
        if marker is None:
            return
        try:
            if self.restore_path.read_text() == marker:
                self.restore_path.unlink(missing_ok=True)
        except OSError:
            pass

    def is_paused(self) -> bool:
        """True if a rewind/redo recently asked the watcher to hold off."""
        # A deadline is intentionally only a trailing-event grace period.  The
        # owner marker covers restores longer than that grace period and is
        # released by the owner (or recognized as dead after a crash).
        try:
            if time.time() < float(self.pause_path.read_text().strip()):
                return True
        except (OSError, ValueError):
            pass
        try:
            raw = self.restore_path.read_text().strip().split()
            if len(raw) < 2:
                return False
            pid, identity = int(raw[0]), raw[1]
        except (OSError, ValueError):
            return False
        alive = self._process_alive(pid)
        current_identity = self._process_identity(pid)
        if alive is False or (
            current_identity is not None and identity != "-" and current_identity != identity
        ):
            try:
                if self.restore_path.read_text().strip() == " ".join(raw):
                    self.restore_path.unlink(missing_ok=True)
            except OSError:
                pass
            return False
        return alive is not False

    # -- watcher heartbeat (for `status`) ----------------------------------

    @property
    def _watch_pid_path(self) -> Path:
        return self.repo.meta_dir / "watcher.pid"

    @property
    def _watch_status_path(self) -> Path:
        return self.repo.meta_dir / "watcher.status.json"

    def _load_watcher_status(self) -> dict:
        try:
            data = json.loads(self._watch_status_path.read_text())
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_watcher_status(self, data: dict) -> None:
        tmp = self._watch_status_path.with_name(
            f".{self._watch_status_path.name}.{uuid.uuid4().hex}.tmp"
        )
        try:
            tmp.write_text(json.dumps(data, sort_keys=True))
            os.replace(tmp, self._watch_status_path)
        finally:
            tmp.unlink(missing_ok=True)

    def mark_watching(self, backend: str = "starting") -> None:
        pid = os.getpid()
        marker = f"{pid} {_now_iso()} {self._process_identity(pid) or '-'}"
        self._watch_marker = marker
        try:
            self._write_owner_marker(self._watch_pid_path, marker)
            self._save_watcher_status(
                {
                    "running": True,
                    "pid": pid,
                    "since": marker.split(maxsplit=2)[1],
                    "identity": marker.split(maxsplit=2)[2],
                    "backend": backend,
                    "pending": False,
                    "last_success": None,
                    "last_checkpoint": None,
                    "last_error": None,
                }
            )
        except OSError:
            pass

    def clear_watching(self) -> None:
        try:
            marker = self._watch_marker
            if marker is None or self._watch_pid_path.read_text() == marker:
                self._watch_pid_path.unlink(missing_ok=True)
                data = self._load_watcher_status()
                if marker is None or data.get("pid") == os.getpid():
                    data["running"] = False
                    data["pending"] = False
                    self._save_watcher_status(data)
        except OSError:
            pass
        finally:
            self._watch_marker = None

    def update_watcher_status(self, **updates: object) -> None:
        """Persist small watcher diagnostics without replacing another owner."""
        try:
            data = self._load_watcher_status()
            if self._watch_marker is not None and data.get("pid") != os.getpid():
                return
            data.update(updates)
            self._save_watcher_status(data)
        except OSError:
            pass

    def note_watcher_backend(self, backend: str) -> None:
        self.update_watcher_status(backend=backend)

    def note_watcher_pending(self, pending: bool) -> None:
        self.update_watcher_status(pending=pending)

    def note_watcher_success(self, checkpoint: Checkpoint | None) -> None:
        self.update_watcher_status(
            last_success=_now_iso(),
            last_checkpoint=checkpoint.id if checkpoint is not None else None,
            last_error=None,
        )

    def note_watcher_error(self, exc: Exception) -> None:
        self.update_watcher_status(
            last_error=f"{type(exc).__name__}: {exc}",
            last_error_time=_now_iso(),
        )

    def watcher_diagnostics(self) -> dict:
        """Return persisted watcher diagnostics, including stopped runs."""
        return self._load_watcher_status()

    def watcher_status(self) -> str | None:
        """A short description of a live watcher process, or None if none."""
        try:
            marker = self._watch_pid_path.read_text().strip()
            raw = marker.split(maxsplit=2)
        except OSError:
            return None
        if not raw:
            return None
        try:
            pid = int(raw[0])
        except ValueError:
            return None
        alive = self._process_alive(pid)
        identity = raw[2] if len(raw) > 2 else "-"
        current_identity = self._process_identity(pid)
        if alive is False or (
            current_identity is not None and identity != "-" and current_identity != identity
        ):
            try:
                if self._watch_pid_path.read_text().strip() == marker:
                    self._watch_pid_path.unlink(missing_ok=True)
                    data = self._load_watcher_status()
                    data["running"] = False
                    data["pending"] = False
                    self._save_watcher_status(data)
            except OSError:
                pass
            return None
        since = raw[1] if len(raw) > 1 else "?"
        if alive is None:
            return f"unknown (pid {pid}, since {since}; permission denied or unavailable)"
        if identity == "-" and current_identity is None:
            return f"unknown (pid {pid}, since {since}; process identity unavailable)"
        data = self._load_watcher_status()
        backend = data.get("backend", "unknown")
        pending = "yes" if data.get("pending") else "no"
        error = data.get("last_error")
        suffix = f", backend {backend}, pending {pending}"
        if error:
            suffix += f", last error: {error}"
        return f"running (pid {pid}, since {since}{suffix})"

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

    def _tree_entries(self, tree: str) -> dict[str, tuple[str, str, str]]:
        """Return ``path -> (mode, type, object-id)`` for a tree."""
        out = self.repo.git("ls-tree", "-r", "-z", tree).stdout
        entries: dict[str, tuple[str, str, str]] = {}
        for record in out.split("\0"):
            if not record:
                continue
            meta, path = record.split("\t", 1)
            mode, kind, oid = meta.split(" ", 2)
            entries[path] = (mode, kind, oid)
        return entries

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
        # Keep the temporary path short and outside the work tree. A long
        # repository path plus a deep restored filename can exceed Windows'
        # legacy path limit when staged below the private git directory.
        stage = Path(tempfile.mkdtemp(prefix="stepback-restore-"))
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

            # Verify the same canonical tree that checkpointing uses. This
            # catches a failed/partial delete, promotion failure, or a concurrent
            # writer before the caller reports a successful restore.
            if self._snapshot_tree() != target_tree:
                raise RestoreError(
                    "restore incomplete: working tree does not match target",
                    operation="verify",
                )
        except GitError as exc:
            raise RestoreError(
                f"could not stage restore: {exc}", operation="stage"
            ) from exc
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
            relative = self._relative_path(dst)
            raise RestoreError(
                f"could not promote {relative}: {exc}",
                path=relative,
                operation="promote",
            ) from exc

    def _clear_conflicting_parents(self, dst: Path) -> None:
        parent = dst.parent
        while parent != self.repo.work_tree and parent != parent.parent:
            if parent.exists() and not parent.is_dir():
                parent.unlink(missing_ok=True)
                return
            parent = parent.parent

    def _relative_path(self, path: Path) -> str:
        try:
            return str(path.relative_to(self.repo.work_tree))
        except ValueError:
            return str(path)

    def _remove_path(self, fpath: Path) -> None:
        try:
            if fpath.is_dir() and not fpath.is_symlink():
                shutil.rmtree(fpath)
            else:
                fpath.unlink(missing_ok=True)
        except FileNotFoundError:
            return  # absent/already removed is an acceptable race
        except OSError as exc:
            relative = self._relative_path(fpath)
            raise RestoreError(
                f"could not remove {relative}: {exc}",
                path=relative,
                operation="remove",
            ) from exc
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

    def _normalize_restore_paths(self, current_tree: str, target_tree: str,
                                 paths: list[str] | tuple[str, ...]) -> tuple[str, ...]:
        """Validate and expand user paths against the two trees.

        A directory name is accepted as a convenient prefix.  Paths outside
        the work tree, empty paths, and paths absent from both snapshots are
        rejected before any filesystem mutation occurs.
        """
        current = self._ls_paths(current_tree)
        target = self._ls_paths(target_tree)
        available = current | target
        if not paths:
            raise ValueError("at least one --path is required")

        expanded: set[str] = set()
        for raw_value in paths:
            raw = str(raw_value).replace("\\", "/")
            if not raw or "\x00" in raw:
                raise ValueError("restore paths must be non-empty and NUL-free")
            norm = posixpath.normpath(raw)
            if norm in (".", "") or norm.startswith("/") or norm == ".." or norm.startswith("../"):
                raise ValueError(f"restore path escapes the work tree: {raw!r}")
            if ":" in norm.split("/", 1)[0]:
                raise ValueError(f"restore path must be relative: {raw!r}")
            matches = {p for p in available if p == norm or p.startswith(norm + "/")}
            if not matches:
                raise ValueError(f"path is absent from both snapshots: {raw!r}")
            expanded.update(matches)

        # A selected path must include every tree entry participating in a
        # file/directory transition.  Otherwise a partial restore could leave
        # an unselected file blocking the requested target layout.
        for selected in expanded:
            for other in available:
                if (
                    selected != other
                    and (
                        selected.startswith(other + "/")
                        or other.startswith(selected + "/")
                    )
                    and (
                        (selected in current) != (selected in target)
                        or (other in current and other not in target)
                        or (other in target and other not in current)
                    )
                    and other not in expanded
                ):
                    raise ValueError(
                        f"path selection crosses an unselected file/directory transition: {other!r}"
                    )
        return tuple(sorted(expanded))

    def plan_restore_paths(self, target_tree: str, paths: list[str] | tuple[str, ...]) -> SelectiveRestorePlan:
        """Preview a selective restore without changing files or adapters."""
        current_tree = self._snapshot_tree()
        selected = self._normalize_restore_paths(current_tree, target_tree, paths)
        changed_all = self._changed_paths(current_tree, target_tree)
        changed = len(changed_all & set(selected))
        stat = self.repo.git(
            "diff", "--stat", current_tree, target_tree, "--", *selected
        ).stdout.rstrip()
        return SelectiveRestorePlan(
            target_tree=target_tree, paths=selected, stat=stat, changed=changed
        )

    def _record_redo(self, current_tree: str) -> RedoEntry:
        """Durably save the pre-restore tree so a partial restore remains undoable."""
        token = uuid.uuid4().hex[:8]
        current_commit = self._commit_tree(
            current_tree, None, f"pre-selective-restore @ {_now_iso()}"
        )
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
        self._save()
        return self.state.redo[-1]

    def _protect_recovery_tree(self, tree: str) -> str:
        """Keep a pre-operation tree reachable until its journal is cleared."""
        token = uuid.uuid4().hex[:8]
        commit = self._commit_tree(tree, None, f"pre-operation recovery @ {_now_iso()}")
        ref = f"refs/checkpoints/_recovery/{token}"
        self.repo.git("update-ref", ref, commit)
        return ref

    def _restore_tree_paths(self, target_tree: str, paths: tuple[str, ...]) -> None:
        """Restore only ``paths`` from ``target_tree`` and verify the boundary."""
        idx = self._tmp_index()
        stage = Path(tempfile.mkdtemp(prefix="stepback-selective-restore-"))
        current_tree = self._snapshot_tree()
        current_entries = self._tree_entries(current_tree)
        target_entries = self._tree_entries(target_tree)
        selected = set(paths)
        try:
            to_write = sorted(selected & set(target_entries))
            to_delete = sorted((selected & set(current_entries)) - set(target_entries))
            if to_write:
                self.repo.git("read-tree", target_tree, index=idx)
                self.repo.git(
                    "checkout-index", "-f", f"--prefix={stage}{os.sep}", "--", *to_write, index=idx
                )
            for rel in to_delete:
                self._remove_path(self.repo.work_tree / rel)
            same_device = self._same_device(stage)
            for rel in to_write:
                self._promote(stage / rel, self.repo.work_tree / rel, same_device)

            after_tree = self._snapshot_tree()
            after_entries = self._tree_entries(after_tree)
            outside = self._changed_paths(current_tree, after_tree) - selected
            if outside:
                raise RestoreError(
                    "selective restore changed an unselected path",
                    operation="verify",
                    path=sorted(outside)[0],
                )
            for rel in selected:
                if after_entries.get(rel) != target_entries.get(rel):
                    raise RestoreError(
                        "selective restore did not reach the requested target",
                        operation="verify",
                        path=rel,
                    )
        except GitError as exc:
            raise RestoreError(f"could not stage selective restore: {exc}", operation="stage") from exc
        finally:
            idx.unlink(missing_ok=True)
            shutil.rmtree(stage, ignore_errors=True)

    def restore_paths(self, checkpoint: Checkpoint, paths: list[str] | tuple[str, ...]) -> list[str]:
        """Restore selected files from ``checkpoint`` while leaving others intact.

        The operation is redo-able as one transaction.  Conversation adapters
        are intentionally not restored: a file-level selection cannot imply a
        compatible conversation rewind.
        """
        self._begin_restore()
        self.pause()
        try:
            with self._transaction():
                current_tree = self._snapshot_tree()
                selected = self._normalize_restore_paths(current_tree, checkpoint.tree, paths)
                journal = self._begin_journal(
                    "selective-restore",
                    pre_tree=current_tree,
                    target_tree=checkpoint.tree,
                    selected_paths=selected,
                )
                try:
                    redo = self._record_redo(current_tree)
                    journal.recovery_ref = redo.ref
                    journal.advance(self.journal_path, "recovery-saved")
                    journal.advance(self.journal_path, "applying")
                    self._restore_tree_paths(checkpoint.tree, selected)
                    journal.advance(self.journal_path, "verified")
                    self._save()
                    self._finish_journal(journal)
                except Exception as exc:
                    journal.advance(self.journal_path, "failed", error=f"{type(exc).__name__}: {exc}")
                    raise
            return []
        finally:
            self._end_restore()
            self.pause()

    def rewind(self, checkpoint: Checkpoint) -> list[str]:
        """Restore to ``checkpoint``, pushing current state onto the redo stack.

        Returns resume hints from any conversation adapters that were restored.
        The pre-rewind state is committed, referenced, and saved to the redo
        stack *before* the destructive restore, so a crash mid-restore is always
        recoverable with ``stepback redo``.
        """
        # Tell any running watcher to ignore the events this restore will cause.
        self._begin_restore()
        self.pause()
        try:
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
                journal = self._begin_journal(
                    "rewind",
                    pre_tree=current_tree,
                    target_tree=checkpoint.tree,
                )
                try:
                    journal.recovery_ref = redo_ref
                    journal.advance(self.journal_path, "recovery-saved")
                    journal.advance(self.journal_path, "applying")
                    self._restore_tree(checkpoint.tree)
                    hints = self._restore_adapters(checkpoint.adapters, checkpoint.session_dir)
                    journal.advance(self.journal_path, "verified")
                    self._save()
                    self._finish_journal(journal)
                except Exception as exc:
                    journal.advance(self.journal_path, "failed", error=f"{type(exc).__name__}: {exc}")
                    raise
            return hints
        finally:
            self._end_restore()
            self.pause()  # cover trailing filesystem events from the restore

    def redo(self) -> tuple[RedoEntry | None, list[str]]:
        """Reverse the most recent rewind."""
        self._begin_restore()
        self.pause()
        try:
            with self._transaction():
                if not self.state.redo:
                    return None, []
                # Keep the entry on the durable stack until the restore succeeds so
                # a failed redo can be retried, including from a fresh Engine.
                entry = self.state.redo[-1]
                current_tree = self._snapshot_tree()
                recovery_ref = self._protect_recovery_tree(current_tree)
                journal = self._begin_journal(
                    "redo",
                    pre_tree=current_tree,
                    target_tree=entry.tree,
                )
                try:
                    journal.recovery_ref = recovery_ref
                    journal.advance(self.journal_path, "recovery-saved")
                    journal.advance(self.journal_path, "applying")
                    self._restore_tree(entry.tree)
                    hints = self._restore_adapters(entry.adapters, entry.session_dir)
                    journal.advance(self.journal_path, "verified")
                    self.state.redo.pop()
                    if entry.ref:
                        self.repo.git("update-ref", "-d", entry.ref, check=False)
                    self.repo.git("update-ref", "-d", recovery_ref, check=False)
                    self._save()
                    self._finish_journal(journal)
                except Exception as exc:
                    journal.advance(self.journal_path, "failed", error=f"{type(exc).__name__}: {exc}")
                    raise
            return entry, hints
        finally:
            self._end_restore()
            self.pause()

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
