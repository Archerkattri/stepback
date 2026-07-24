# Contributing

Thanks for looking at stepback. This is a small, focused tool. The bar for
changes is: keep Layer 1 (files) exact and never let it touch the user's real git
state.

## Setup

```console
pip install -e ".[dev]"
```

Requires Python 3.11+ and `git` on `PATH`.

## Checks (the same ones CI runs)

```console
ruff check .
mypy
pytest -q
```

All three must pass. CI runs them on Python 3.11 and 3.12.

## Layout

```
src/stepback/
  repo.py       storage resolution + the safe git runner (isolated index/refs)
  engine.py     checkpoint / restore / rewind / redo / diff, locking, pause
  store.py      state.json: checkpoint log + redo stack, atomic writes
  watcher.py    debounced file watcher, native -> polling -> none degradation
  lock.py       cross-process advisory lock
  errors.py     user-facing exception types (map to clean CLI messages)
  cli.py        Typer CLI
  adapters/     Layer 2 conversation adapters (best-effort, behind a Protocol)
tests/          engine, hardening, CLI, adapters, watcher, store
```

Read `ARCHITECTURE.md` before changing the engine or repo layer.

## Ground rules

- **Never touch the user's real git.** No writes to `HEAD`, branches, or the real
  index. Every index-touching git call goes through a private `GIT_INDEX_FILE`.
  If you add a code path, add a test that proves the user's `HEAD`/index/branch
  are unchanged.
- **Layer 1 works with zero adapters.** Conversation adapters are optional and
  best-effort. Every adapter method must be defensive and never raise; the engine
  assumes it can call any of them and get a no-op on failure.
- **Restores stay exact and recoverable.** If you change restore, keep it
  all-or-nothing recoverable (stage first, save the redo entry before anything
  destructive) and keep the byte-exact round-trip tests green.
- **Don't weaken a test to make it pass.** Fix the code. Add a test for every bug
  you fix and every edge case you handle.

## Adding an agent adapter

Implement the five `AgentAdapter` methods (`detect`, `session_files`, `snapshot`,
`restore`, `resume_hint`) in `adapters/<name>.py`, add the class to
`ALL_ADAPTERS` in `adapters/__init__.py`, and add a test that exercises a
snapshot/restore round-trip against a fake `HOME`. Keep it best-effort: return
`{}` / `False` instead of raising when files are missing.

## Commits

Clear, imperative subject lines. Keep unrelated changes in separate commits.
