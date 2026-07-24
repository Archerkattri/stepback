# stepback

**git time-travel for any AI coding agent — undo a bad Codex/Claude/aider session in one command, no matter which tool made the mess.**

AI coding agents are great until they aren't. One bad turn and your working tree
is full of half-broken edits and a junk file or two. `git stash` doesn't help
(the agent never committed), and hitting "undo" in your editor won't unwind a
multi-file change. `stepback` sits *beside* your agent, snapshots your files as
it works, and lets you rewind to any point — instantly and exactly.

```console
$ stepback run -- claude          # or: -- codex   /   -- aider   /   any agent
  ... let the agent work ...
$ stepback list
  #7   14:22:10  3 file(s) [~3]      +conv:claude-code
  #6   14:19:41  1 file(s) [+1]      +conv:claude-code
$ stepback rewind 6                 # preview the diff, confirm, done
  restored to checkpoint #6.  (`stepback redo` to undo this rewind)
    resume conversation:  claude --resume 9f3c…
```

## Why this exists (and how it's different)

There are a handful of "undo my AI agent" tools already (walkback, doover,
bashback, …). They share two limits that `stepback` is built to beat:

1. **They're tied to one agent.** Most hook into a single tool's lifecycle.
   `stepback`'s file layer is **filesystem-based**, so it works with *any* agent
   that edits files on disk — Claude Code, Codex, aider, Cursor's CLI, a plain
   `bash` script, whatever comes next.

2. **They only rewind the files.** `stepback` optionally also rewinds the
   **conversation state** — the agent's on-disk session transcript — so after a
   rewind you can *resume the agent from that point in the chat*, not just reset
   the code. No other tool in this niche does this.

And it does all of that **without ever touching your real git history**:
checkpoints live in an isolated ref namespace, written with a private index, so
your branch, `HEAD`, staging area, and commits are never modified. Run it inside
a repo you're actively working in — it stays out of your way.

## Two layers

### Layer 1 — file checkpoint/rewind (solid, the core)

A debounced file watcher snapshots your working directory each time an edit
burst settles. Snapshots are content-addressed git objects stored under
`refs/checkpoints/<session>/<n>`:

- **Never touches your work.** Uses git plumbing (`hash-object`, `write-tree`,
  `commit-tree`, `update-ref`) with a *separate* index via `GIT_INDEX_FILE`.
  Your branch, `HEAD`, index, staging, and history are untouched — verified by
  tests.
- **Exact restores.** Rewinding reproduces the tree byte-for-byte: edits
  reverted, added files removed, deleted files brought back. Handles binary
  files, `.gitignore`, and large trees (it *is* git).
- **Safe.** Rewind always shows a diff preview and asks for confirmation before
  changing anything, and pushes your current state onto a **redo** stack so a
  rewind is itself reversible.
- **Works without git too.** Outside a git repo, stepback creates a private
  object store at `.stepback/shadow.git` and uses the identical machinery.

### Layer 2 — conversation rewind (the wedge, best-effort)

When an agent is detected, stepback also snapshots its on-disk **session state**
with each checkpoint and restores it on rewind, then prints a resume hint. This
is what lets you rewind the *conversation*, not just the code.

> **Honesty note.** Layer 2 depends on **private, undocumented, version-unstable
> session formats** that each agent vendor can change at any time. It is
> deliberately **best-effort and isolated behind a clean adapter interface**: if
> the agent isn't detected or its format isn't recognized, stepback silently
> degrades to file-only rewind. It never crashes, and it never blocks Layer 1.

| Agent | Adapter | State captured | Resume hint | Status |
|---|---|---|---|---|
| Claude Code | `claude-code` | active transcript `~/.claude/projects/<slug>/<uuid>.jsonl` | `claude --resume <uuid>` | best-effort |
| Codex CLI | `codex` | most-recent session under `~/.codex/sessions/` | `codex resume` | best-effort |
| others | — | — | — | file-only rewind |

Adding an agent is one small class implementing `detect / session_files /
snapshot / restore / resume_hint` (see `ARCHITECTURE.md`).

## What works / what's best-effort

- **Works, tested, solid:** file snapshot & exact rewind (incl. binary,
  additions, deletions, nested dirs), git-history isolation, `.gitignore`
  respect, redo, no-git shadow store, diff preview, checkpoint dedup.
- **Best-effort:** conversation snapshot/restore for Claude Code and Codex
  (private formats — may break on agent updates; degrades to file-only).
- **Degrades gracefully:** if the OS file-watch limit is hit, stepback falls
  back to a polling watcher, then to start/end-only checkpoints — it never takes
  down the agent it's watching.

## Install

```console
pip install stepback        # (once published)
# or, from source:
pip install -e .
```

Requires Python 3.11+. Depends only on `typer` and `watchdog`.

## Quickstart

```console
# Wrap any agent. stepback watches, checkpoints, and stays out of git's way.
stepback run -- claude
stepback run -- codex
stepback run -- aider
stepback run -- bash -c 'your script that edits files'

stepback status            # storage mode + detected agents
stepback list              # checkpoints, newest first
stepback diff <id>         # what a checkpoint changed (add -w to diff vs working tree)
stepback rewind [id]       # preview + restore (defaults to the most recent checkpoint)
stepback rewind <id> -y    # skip the confirmation
stepback redo              # reverse the last rewind
```

## Limitations (read these)

- Snapshots respect `.gitignore` — ignored files (build artifacts, `.env`,
  `node_modules`) are **not** captured or restored. This is usually what you
  want; it's called out here so it's never a surprise.
- Layer 2 conversation formats are private to each agent and can break on any
  update. Treat conversation rewind as a bonus, not a guarantee.
- Restoring overwrites the current working tree for tracked/non-ignored files
  (after the diff preview + your confirmation).

## License

MIT © 2026 Krishi Attri
