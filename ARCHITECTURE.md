# Architecture

stepback is two layers with a hard boundary between them. Layer 1 (files) is the
solid core and is fully independent. Layer 2 (conversation) is optional,
best-effort, and quarantined behind an adapter interface so its fragility can
never destabilize Layer 1.

```
         ┌──────────────────────── CLI (cli.py, Typer) ────────────────────────┐
         │  run · list · rewind · redo · diff · status                        │
         └───────────────┬─────────────────────────────────┬──────────────────┘
                         │                                 │
                  ┌──────▼──────┐                   ┌──────▼──────────┐
                  │  Engine     │                   │ DebouncedWatcher│  (run only)
                  │ (engine.py) │                   │  (watcher.py)   │
                  └──┬───────┬──┘                   └─────────────────┘
          Layer 1 ── │       │ ── Layer 2 (optional, best-effort)
        ┌────────────▼──┐  ┌─▼───────────────── adapters/ ────────────────┐
        │ Repo (repo.py)│  │ AgentAdapter (base.py)                        │
        │ + State/store │  │   ClaudeCodeAdapter · CodexAdapter · …        │
        └───────────────┘  └───────────────────────────────────────────────┘
```

## Layer 1 — file checkpoints (git plumbing)

**`repo.py` — storage resolution + safe git runner.**
`resolve_repo(path)` returns a `Repo` in one of two modes:

- **shared** — `path` is inside a git repo. Objects are stored in that repo's
  existing object database (cheap, deduped), refs under `refs/checkpoints/`.
- **shadow** — no git repo. A private bare object DB is created at
  `<workdir>/.stepback/shadow.git` and used identically.

Both modes run the *same* plumbing; only the `git_dir` (and therefore the
metadata location) differs. `Repo.git()` always passes `--git-dir` +
`--work-tree`, a set of deterministic config overrides
(`core.bare=false`, `gc.auto=0`, `core.autocrlf=false`), and — crucially — a
caller-supplied `GIT_INDEX_FILE` for any index-touching operation, so the user's
real `.git/index` is never written.

**Isolation guarantee.** Checkpoints never call `update-ref` on `HEAD` or any
branch, never `commit` onto a branch, and never write the real index. The only
refs created are under `refs/checkpoints/<session>/<n>`. This is asserted by
`test_shared_mode_does_not_touch_head_index_or_branch`.

**Snapshot** (`Engine._snapshot_tree`):
```
GIT_INDEX_FILE=<tmp>  git add -A .        # respects .gitignore, into a throwaway index
GIT_INDEX_FILE=<tmp>  git write-tree      # -> tree SHA
```
The tree is committed with `commit-tree` (stepback identity, so it works even
with no configured git user) and pointed to by a checkpoint ref. Identical trees
are deduped (an idle burst makes no checkpoint).

**Restore** (`Engine._restore_tree`) is exact and deletion-safe:
```
current = snapshot of the work tree now
read-tree <target> into a temp index
checkout-index -a -f            # write/overwrite every file in the target tree
remove (current paths − target paths)   # delete files added since the checkpoint
prune emptied directories
```

**Redo.** `rewind` first snapshots the current state (files + conversation) onto
a redo stack, then restores. `redo` pops and restores it. A brand-new checkpoint
clears the redo stack (a new timeline branch).

**`store.py` — state.** The checkpoint log and redo stack live in
`<git_dir>/stepback/state.json`, written atomically (temp + `os.replace`) so a
crash or flaky filesystem can't corrupt it. Metadata lives inside `git_dir` so
git never snapshots it.

**`watcher.py` — debounced watcher.** A `watchdog` observer coalesces edit bursts
(default 500 ms quiet period) into one checkpoint, ignoring `.git/` and
`.stepback/`. It degrades native → polling → no-live-watch so a constrained host
(e.g. inotify limit reached) never crashes the watched agent; start/end
checkpoints still happen.

## Layer 2 — conversation rewind (adapters)

**`adapters/base.py` — the seam.** `AgentAdapter` is a `Protocol`:
`detect() · session_files() · snapshot(dest) · restore(meta, src) ·
resume_hint(meta)`. Every method is defensive and must never raise.

The engine treats adapters as opaque: on each checkpoint it asks every active
adapter to `snapshot()` its session state into a per-checkpoint directory
(`<git_dir>/stepback/sessions/cp-XXXX/<adapter>/`) and stores the returned
metadata on the checkpoint. On rewind it hands that metadata back to
`restore()`. If there are no adapters, Layer 1 is entirely unaffected.

**Implemented adapters** (both best-effort, both reverse-engineered from private
formats):

- `ClaudeCodeAdapter` — transcripts at `~/.claude/projects/<slug>/<uuid>.jsonl`
  (`<slug>` = cwd with non-alphanumerics → `-`). Captures the most-recently
  modified transcript; resume hint `claude --resume <uuid>`.
- `CodexAdapter` — most-recent session file under `~/.codex/sessions/`; resume
  hint `codex resume`.

**Adding an agent:** implement the five methods, add the class to
`ALL_ADAPTERS` in `adapters/__init__.py`. `detect_adapters()` picks it up
automatically. Keep it best-effort — return `{}` / `False` rather than raising
when anything is missing.

## Failure philosophy

Real result or honest degradation — nothing in between. Watching can fall back;
conversation adapters can no-op; but a file rewind is always exact, and the
user's real git state is always untouched.
