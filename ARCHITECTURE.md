# Architecture

stepback is two layers with a hard boundary between them. Layer 1 (files) is the
solid core and is fully independent. Layer 2 (conversation) is optional,
best-effort, and quarantined behind an adapter interface so its fragility can
never destabilize Layer 1.

```
         +----------------------- CLI (cli.py, Typer) -----------------------+
         |  run . list . rewind . redo . diff . status                       |
         +---------------+-------------------------------+-------------------+
                         |                               |
                  +------v------+                 +------v----------+
                  |  Engine     |                 | DebouncedWatcher|  (run only)
                  | (engine.py) |                 |  (watcher.py)   |
                  +--+-------+--+                 +-----------------+
          Layer 1 -- |       | -- Layer 2 (optional, best-effort)
        +------------v--+  +-v----------------- adapters/ ----------------+
        | Repo (repo.py)|  | AgentAdapter (base.py)                       |
        | + State/store |  |   ClaudeCodeAdapter . CodexAdapter . ...     |
        | + lock.py     |  |                                              |
        +---------------+  +----------------------------------------------+
```

## Layer 1: file checkpoints (git plumbing)

**`repo.py`: storage resolution + safe git runner.**
`resolve_repo(path)` returns a `Repo` in one of two modes:

- **shared**: `path` is inside a git work tree. Objects are stored in that
  repo's existing object database (cheap, deduped), refs under
  `refs/checkpoints/`.
- **shadow**: no git repo. A private bare object DB is created at
  `<workdir>/.stepback/shadow.git` and used identically. Concurrent first-time
  creation is serialised with a lock and a re-check, so two processes can't race
  the template copy.

Both modes run the same plumbing; only the `git_dir` (and therefore the metadata
location) differs. `Repo.git()` always passes `--git-dir` + `--work-tree`, a set
of deterministic config overrides, and a caller-supplied `GIT_INDEX_FILE` for any
index-touching operation, so the user's real `.git/index` is never written. It
also strips inherited `GIT_INDEX_FILE`, `GIT_DIR`, and `GIT_WORK_TREE` from the
environment (in case stepback is launched from inside a git hook), and turns a
missing `git` binary into a clean `GitError`.

The config overrides: `core.bare=false`, `gc.auto=0`, `core.autocrlf=false`,
`core.fileMode=true`, `core.quotePath=false` (raw UTF-8 path names, so names with
unicode/spaces round-trip), `core.symlinks=true`.

**Isolation guarantee.** Checkpoints never call `update-ref` on `HEAD` or any
branch, never `commit` onto a branch, and never write the real index. The only
refs created are under `refs/checkpoints/`. Asserted by tests under a normal
repo, a detached HEAD, a mid-merge state (`MERGE_HEAD` present), and with staged
but uncommitted changes (the user's index stays byte-for-byte identical).

**Snapshot** (`Engine._snapshot_tree`):
```
GIT_INDEX_FILE=<tmp>  git add -A .        # respects .gitignore, into a throwaway index
GIT_INDEX_FILE=<tmp>  git write-tree      # -> tree SHA
```
The tree is committed with `commit-tree` (stepback identity, so it works even
with no configured git user) and pointed to by a checkpoint ref. Identical trees
are deduped (an idle burst makes no checkpoint).

**Restore** (`Engine._restore_tree`) is exact, minimal, and atomic:
```
current = snapshot of the work tree now
if current == target: return               # nothing to do
to_write  = files that differ AND exist in target
to_delete = files present now but absent from target
checkout-index --prefix=<stage>/ -- <to_write>   # materialise wanted versions first
delete to_delete (dirs before their now-stale parents)
os.replace each staged file over the real file   # atomic per file, same filesystem
```
Only files that actually differ are touched, so a huge tree with one changed file
does one write. Staging happens fully before the real tree is touched, so a
failure mid-staging leaves the tree untouched. Deletions run before writes so a
file->directory (or the reverse) transition never collides at the same path.
When staging is on a different filesystem than the work tree, it falls back to a
copy.

**Redo and crash recovery.** `rewind` snapshots the current state, commits it,
creates a ref for it under `refs/checkpoints/_redo/<token>` (so the user's own
`git gc` can't prune it), pushes it onto the redo stack, and saves state, all
*before* the destructive restore. So an interrupted rewind is always recoverable
with `stepback redo`. `redo` pops the entry, restores it, and deletes its ref. A
brand-new checkpoint clears the redo stack and its refs (a new timeline branch).

**Concurrency.** Every mutating operation (`checkpoint`, `rewind`, `redo`,
`start_session`) runs inside `Engine._transaction()`, which takes a cross-process
advisory lock (`lock.py`, `fcntl.flock` on POSIX, `O_EXCL` lock file with a
stale-lock timeout elsewhere) and re-reads state from disk under the lock, so a
running watcher and a manual command in another terminal serialise cleanly and
neither loses the other's checkpoints.

**Watcher pause.** A restore rewrites files, which the watcher would otherwise
see and checkpoint (clearing the redo stack). Before and after a restore the
engine writes a short-lived `paused` marker with a deadline; the watcher's
`on_settle` checks `is_paused()` and skips while it's set. This works across
processes because both resolve the same metadata directory.

**`store.py`: state.** The checkpoint log and redo stack live in
`<git_dir>/stepback/state.json`, written atomically (temp + fsync + `os.replace`)
so a crash or flaky filesystem can't corrupt it. On load, an unreadable file is
moved aside to `state.json.corrupt-<ts>` (never silently discarded) and a clean
slate is returned. Unknown future keys are ignored, so an older stepback can read
a newer state file.

**`watcher.py`: debounced watcher.** A `watchdog` observer coalesces edit bursts
(default 500 ms quiet period) into one checkpoint, ignoring `.git/` and
`.stepback/`. It degrades native -> polling -> no-live-watch so a constrained host
(inotify limit reached) never crashes the watched agent; start/end checkpoints
still happen. A checkpoint callback that raises is swallowed, never propagated
into the watched agent.

## Layer 2: conversation rewind (adapters)

**`adapters/base.py`: the seam.** `AgentAdapter` is a `Protocol`:
`detect . session_files . snapshot(dest) . restore(meta, src) .
resume_hint(meta)`. Every method is defensive and must never raise. The engine
wraps every adapter call in a `try/except`, so even an adapter that raises in
every method cannot affect Layer 1 (tested).

The engine treats adapters as opaque: on each checkpoint it asks every active
adapter to `snapshot()` its session state into a per-checkpoint directory
(`<git_dir>/stepback/sessions/cp-XXXX/<adapter>/`) and stores the returned
metadata on the checkpoint. On rewind it hands that metadata back to `restore()`.
If there are no adapters, Layer 1 is entirely unaffected.

**Implemented adapters** (both best-effort, both reverse-engineered from private
formats):

- `ClaudeCodeAdapter`: transcripts at `~/.claude/projects/<slug>/<uuid>.jsonl`
  (`<slug>` = cwd with non-alphanumerics -> `-`). Captures the most-recently
  modified transcript; resume hint `claude --resume <uuid>`.
- `CodexAdapter`: most-recent session file under `~/.codex/sessions/`; resume
  hint `codex resume`.

**Adding an agent:** implement the five methods, add the class to `ALL_ADAPTERS`
in `adapters/__init__.py`. `detect_adapters()` picks it up automatically. Keep it
best-effort: return `{}` / `False` rather than raising when anything is missing.

## Failure philosophy

Real result or honest degradation, nothing in between. Watching can fall back;
conversation adapters can no-op; but a file rewind is always exact, the user's
real git state is always untouched, and an interrupted rewind is always
recoverable.
