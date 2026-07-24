"""stepback command-line interface.

    stepback run -- <agent command...>   watch a session, checkpoint each settled edit burst
    stepback list                        list checkpoints, newest first
    stepback rewind [ID]                 preview + restore to a checkpoint (redo-able)
    stepback redo                        reverse the last rewind
    stepback diff <ID>                   show what a checkpoint changed
    stepback status                      show mode, session, watcher, and adapter state
"""

from __future__ import annotations

import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import NoReturn

import typer

from .adapters import detect_adapters
from .engine import Engine
from .errors import StepbackError

# Exit codes (documented, stable):
EXIT_OK = 0
EXIT_ERROR = 1        # expected failure (no such checkpoint, nothing to redo, ...)
EXIT_USAGE = 2        # bad invocation
EXIT_NOT_FOUND = 127  # agent command not found

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="git time-travel for any AI coding agent.",
    epilog=(
        "Examples:\n"
        "  stepback run -- claude        watch a Claude Code session\n"
        "  stepback list                 see checkpoints\n"
        "  stepback rewind               preview + restore the most recent one\n"
        "  stepback rewind 6 --dry-run   see what rewinding to #6 would change\n"
        "  stepback redo                 undo the last rewind"
    ),
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)


def _engine(with_adapters: bool = False) -> Engine:
    cwd = Path.cwd()
    adapters = detect_adapters(cwd) if with_adapters else []
    return Engine(cwd, adapters=adapters)


def _echo(msg: str = "", err: bool = False) -> None:
    typer.echo(msg, err=err)


def _fail(msg: str, code: int = EXIT_ERROR) -> NoReturn:
    typer.echo(f"stepback: {msg}", err=True)
    raise typer.Exit(code)


def _use_color() -> bool:
    return sys.stdout.isatty()


def _relative_time(iso: str) -> str:
    """Render an ISO timestamp as a compact relative age (e.g. '3m ago')."""
    try:
        then = datetime.fromisoformat(iso)
    except ValueError:
        return iso
    now = datetime.now(then.tzinfo) if then.tzinfo else datetime.now()
    secs = (now - then).total_seconds()
    if secs < 0:
        return "just now"
    if secs < 60:
        return f"{int(secs)}s ago"
    if secs < 3600:
        return f"{int(secs // 60)}m ago"
    if secs < 86400:
        return f"{int(secs // 3600)}h ago"
    return f"{int(secs // 86400)}d ago"


def _print_diff(patch: str) -> None:
    """Print a unified diff, colorized when writing to a terminal."""
    if not _use_color():
        sys.stdout.write(patch)
        return
    for line in patch.splitlines(keepends=True):
        if line.startswith("+") and not line.startswith("+++"):
            typer.secho(line, fg=typer.colors.GREEN, nl=False)
        elif line.startswith("-") and not line.startswith("---"):
            typer.secho(line, fg=typer.colors.RED, nl=False)
        elif line.startswith("@@"):
            typer.secho(line, fg=typer.colors.CYAN, nl=False)
        elif line.startswith(("diff ", "index ", "+++", "---")):
            typer.secho(line, fg=typer.colors.YELLOW, nl=False)
        else:
            sys.stdout.write(line)


@app.command(
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True}
)
def run(ctx: typer.Context) -> None:
    """Watch the working directory while running an agent, checkpointing edits.

    Everything after ``--`` is the agent command, e.g. ``stepback run -- claude``.
    """
    argv = list(ctx.args)
    if not argv:
        _fail("no command given.  Usage: stepback run -- <agent command>", EXIT_USAGE)

    from .watcher import DebouncedWatcher  # lazy: only run needs watchdog

    try:
        eng = _engine(with_adapters=True)
    except StepbackError as exc:
        _fail(str(exc))
    session = eng.start_session()
    names = ", ".join(a.name for a in eng.adapters) or "none"
    _echo(f"stepback: watching {eng.repo.work_tree} [{eng.repo.mode} mode]")
    _echo(f"stepback: session {session} · conversation adapters: {names}")

    eng.checkpoint(label="session start")

    def on_settle() -> None:
        # A restore (rewind/redo) in another terminal sets a short pause so the
        # events it causes do not checkpoint the restore and clear the redo stack.
        if eng.is_paused():
            return
        try:
            cp = eng.checkpoint()
        except StepbackError:
            return
        if cp is not None:
            _echo(f"stepback: checkpoint #{cp.id} ({cp.summary})")

    code = 0
    watcher = DebouncedWatcher(eng.repo.work_tree, on_settle, quiet_seconds=0.5)
    eng.mark_watching()
    with watcher:
        if watcher.backend == "none":
            _echo(
                "stepback: live file watching unavailable on this host; "
                "capturing start + end checkpoints only."
            )
        elif watcher.backend == "polling":
            _echo("stepback: using polling file watcher (native watcher unavailable).")
        try:
            code = subprocess.call(argv)
        except FileNotFoundError:
            _echo(f"stepback: command not found: {argv[0]}", err=True)
            code = EXIT_NOT_FOUND
        except KeyboardInterrupt:
            code = 130
        finally:
            eng.clear_watching()

    try:
        final = eng.checkpoint(label="session end")
    except StepbackError:
        final = None
    if final is not None:
        _echo(f"stepback: checkpoint #{final.id} ({final.summary})")
    _echo("stepback: session ended.  `stepback list` to see checkpoints.")
    raise typer.Exit(code)


@app.command(name="list")
def list_cmd() -> None:
    """List checkpoints, newest first."""
    try:
        eng = _engine()
    except StepbackError as exc:
        _fail(str(exc))
    if not eng.state.checkpoints:
        _echo("no checkpoints yet.  Start one with:  stepback run -- <agent>")
        return
    _echo(f"checkpoints in {eng.repo.work_tree} [{eng.repo.mode} mode]:")
    for cp in reversed(eng.state.checkpoints):
        conv = ""
        if cp.adapters:
            conv = "  +conv:" + ",".join(cp.adapters.keys())
        rel = _relative_time(cp.time)
        _echo(f"  #{cp.id:<4} {rel:>9}  {cp.summary}{conv}")
    if eng.state.redo:
        _echo(f"\n({len(eng.state.redo)} state(s) on the redo stack — `stepback redo`)")


@app.command()
def rewind(
    checkpoint_id: int = typer.Argument(
        None, help="Checkpoint id (default: most recent)."
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt."),
    dry_run: bool = typer.Option(
        False, "--dry-run", "-n", help="Show what would change, then stop."
    ),
) -> None:
    """Preview and restore the working tree to a checkpoint (redo-able)."""
    try:
        eng = _engine(with_adapters=True)
    except StepbackError as exc:
        _fail(str(exc))
    cp = eng.state.latest() if checkpoint_id is None else eng.state.by_id(checkpoint_id)
    if cp is None:
        _fail("no such checkpoint." if checkpoint_id is not None else "no checkpoints yet.")

    plan = eng.plan_restore(cp.tree)
    _echo(f"rewind to checkpoint #{cp.id}  ({_relative_time(cp.time)}, {cp.time})")
    _echo(f"  {cp.summary}")
    if plan.is_noop:
        _echo("working tree already matches this checkpoint — nothing to do.")
        raise typer.Exit(EXIT_OK)
    _echo("\nthis will change your working tree:")
    _echo(plan.stat or f"  {plan.changed} file(s) differ")
    if cp.adapters:
        _echo(f"\nwill also restore conversation state: {', '.join(cp.adapters)}")

    if dry_run:
        _echo("\ndry run — nothing changed.  Re-run without --dry-run to apply.")
        raise typer.Exit(EXIT_OK)

    if not yes and not typer.confirm("\nproceed?", default=False):
        _echo("aborted.")
        raise typer.Exit(EXIT_ERROR)

    try:
        hints = eng.rewind(cp)
    except StepbackError as exc:
        _fail(str(exc))
    _echo(f"restored to checkpoint #{cp.id}.  (`stepback redo` to undo this rewind)")
    for h in hints:
        _echo(f"  resume conversation:  {h}")


@app.command()
def redo() -> None:
    """Reverse the most recent rewind."""
    try:
        eng = _engine(with_adapters=True)
        entry, hints = eng.redo()
    except StepbackError as exc:
        _fail(str(exc))
    if entry is None:
        _fail("nothing to redo.")
    _echo(f"redone — working tree restored to the pre-rewind state ({_relative_time(entry.time)}).")
    for h in hints:
        _echo(f"  resume conversation:  {h}")


@app.command()
def diff(
    checkpoint_id: int = typer.Argument(..., help="Checkpoint id."),
    working: bool = typer.Option(
        False, "--working", "-w", help="Diff vs current working tree instead of parent."
    ),
) -> None:
    """Show a checkpoint's diff (vs its parent, or vs the working tree)."""
    try:
        eng = _engine()
        cp = eng.state.by_id(checkpoint_id)
        if cp is None:
            _fail("no such checkpoint.")
        out = eng.diff_working(cp) if working else eng.diff(cp)
    except StepbackError as exc:
        _fail(str(exc))
    if not out.strip():
        _echo("(no differences)")
    else:
        _print_diff(out)


@app.command()
def status() -> None:
    """Show storage mode, session, watcher state, and detected agents."""
    try:
        eng = _engine(with_adapters=True)
    except StepbackError as exc:
        _fail(str(exc))
    _echo(f"work tree  : {eng.repo.work_tree}")
    _echo(f"mode       : {eng.repo.mode}  (git-dir: {eng.repo.git_dir})")
    _echo(f"session    : {eng.state.current_session or '(none)'}")
    _echo(
        f"checkpoints: {len(eng.state.checkpoints)}   "
        f"redo stack: {len(eng.state.redo)}"
    )
    last = eng.state.latest()
    if last is not None:
        _echo(f"last cp    : #{last.id}  {_relative_time(last.time)}  ({last.summary})")
    else:
        _echo("last cp    : (none)")
    watcher = eng.watcher_status()
    _echo(f"watcher    : {watcher or 'not running'}")
    detected = detect_adapters(eng.repo.work_tree)
    if detected:
        _echo("adapters   : " + ", ".join(a.name for a in detected))
    else:
        _echo("adapters   : none (file-only rewind)")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
