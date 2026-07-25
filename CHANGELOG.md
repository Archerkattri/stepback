# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project follows
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.2] - 2026-07-25

### Fixed
- Replaced Pepy's not-yet-indexed new-package badge with the profile's own
  live per-package lifetime-download counter.

## [0.1.1] - 2026-07-25

### Changed
- Sharpened the project landing page, install path, package metadata, and
  discovery keywords for the public launch.
- Added a reusable visual walkthrough of the checkpoint-and-rewind workflow.

## [0.1.0] - 2026-07-24

### Added
- Atomic, minimal restore: only files that differ are touched, wanted versions
  are staged first, and each is moved into place with a same-filesystem rename,
  so a crash can never leave a half-written file.
- Crash-recoverable rewind: the pre-rewind state is committed, ref-protected
  (safe from the user's own `git gc`), and saved to the redo stack before any
  destructive change, so an interrupted rewind is recovered with `stepback redo`.
- Cross-process advisory locking (`lock.py`) around every mutating operation,
  with a re-read of state under the lock, so a running watcher and a manual
  command in another terminal can't corrupt state.
- Watcher pause: a rewind/redo tells a running watcher to ignore the filesystem
  events it causes, so a restore no longer checkpoints itself and clears redo.
- `rewind --dry-run/-n` to preview the plan without touching the tree.
- Colorized diff output, relative timestamps in `list`, and a `status` that
  reports storage mode, session, live watcher (via a heartbeat pidfile), last
  checkpoint, and detected adapters.
- Documented, stable exit codes (`0/1/2/127`) and clean one-line error messages
  instead of tracebacks on expected failures.
- Corrupt/partial `state.json` is quarantined (`state.json.corrupt-<ts>`) rather
  than silently discarded; unknown future keys are ignored on load.
- CI workflow (ruff + mypy + pytest on Python 3.11 and 3.12), ruff and mypy
  configuration, and a `dev` extra.
- Test suite expanded to cover unicode/space/leading-dash names, symlinks,
  detached HEAD, mid-merge, staged changes, nested repos, empty/deep trees,
  file<->directory transitions, concurrency, locking, gc-safe redo, corrupt
  state, and the full CLI surface.

### Fixed
- File names with unicode, spaces, or a leading dash now round-trip through
  rewind exactly (`core.quotePath=false` + NUL-separated path parsing).
- Redo entries survive an aggressive user `git gc` (they are now ref-protected).
- Concurrent shadow-store initialisation no longer races on the git template
  copy.
- Symlinks are stored and restored as symlinks.
- A missing `git` binary produces a clean error instead of a traceback.

Initial working version: git shadow-ref file checkpoints with isolated index,
exact restore, redo, no-git shadow store, debounced watcher, Typer CLI
(run/list/rewind/redo/diff/status), and best-effort Claude Code and Codex
conversation adapters.
