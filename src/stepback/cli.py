"""stepback command-line interface.

    stepback run -- <agent command...>   watch a session, checkpoint on each settled edit burst
    stepback list                        list checkpoints
    stepback rewind [ID]                 preview + restore to a checkpoint (redo-able)
    stepback redo                        reverse the last rewind
    stepback diff <ID>                   show what a checkpoint changed
    stepback status                      show mode, session, and adapter detection
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import typer

from .adapters import detect_adapters
from .engine import Engine

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="git time-travel for any AI coding agent.",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)


def _engine(with_adapters: bool = False) -> Engine:
    cwd = Path.cwd()
    adapters = detect_adapters(cwd) if with_adapters else []
    return Engine(cwd, adapters=adapters)


def _echo(msg: str = "") -> None:
    typer.echo(msg)


@app.command(
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True}
)
def run(ctx: typer.Context) -> None:
    """Watch the working directory while running an agent, checkpointing edits.

    Usage: stepback run -- claude    (everything after ``--`` is the agent cmd)
    """
    argv = list(ctx.args)
    if not argv:
        _echo("error: no command given.  Usage: stepback run -- <agent command>")
        raise typer.Exit(2)

    from .watcher import DebouncedWatcher  # lazy: only run needs watchdog

    eng = _engine(with_adapters=True)
    session = eng.start_session()
    names = ", ".join(a.name for a in eng.adapters) or "none"
    _echo(f"stepback: watching {eng.repo.work_tree} [{eng.repo.mode} mode]")
    _echo(f"stepback: session {session} · conversation adapters: {names}")

    eng.checkpoint(label="session start")

    def on_settle() -> None:
        cp = eng.checkpoint()
        if cp is not None:
            _echo(f"stepback: checkpoint #{cp.id} ({cp.summary})")

    code = 0
    watcher = DebouncedWatcher(eng.repo.work_tree, on_settle, quiet_seconds=0.5)
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
            _echo(f"stepback: command not found: {argv[0]}")
            code = 127
        except KeyboardInterrupt:
            code = 130

    final = eng.checkpoint(label="session end")
    if final is not None:
        _echo(f"stepback: checkpoint #{final.id} ({final.summary})")
    _echo(f"stepback: session ended.  `stepback list` to see checkpoints.")
    raise typer.Exit(code)


@app.command(name="list")
def list_cmd() -> None:
    """List checkpoints, newest first."""
    eng = _engine()
    if not eng.state.checkpoints:
        _echo("no checkpoints yet.  Start one with:  stepback run -- <agent>")
        return
    _echo(f"checkpoints in {eng.repo.work_tree} [{eng.repo.mode} mode]:")
    for cp in reversed(eng.state.checkpoints):
        conv = ""
        if cp.adapters:
            conv = "  +conv:" + ",".join(cp.adapters.keys())
        _echo(f"  #{cp.id:<4} {cp.time}  {cp.summary}{conv}")
    if eng.state.redo:
        _echo(f"\n({len(eng.state.redo)} state(s) on the redo stack — `stepback redo`)")


@app.command()
def rewind(
    checkpoint_id: int = typer.Argument(
        None, help="Checkpoint id (default: most recent)."
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation."),
) -> None:
    """Preview and restore the working tree to a checkpoint (redo-able)."""
    eng = _engine(with_adapters=True)
    cp = eng.state.by_id(checkpoint_id) if checkpoint_id else eng.state.latest()
    if cp is None:
        _echo("no such checkpoint." if checkpoint_id else "no checkpoints yet.")
        raise typer.Exit(1)

    plan = eng.plan_restore(cp.tree)
    _echo(f"rewind to checkpoint #{cp.id}  ({cp.time})")
    _echo(f"  {cp.summary}")
    if plan.is_noop:
        _echo("working tree already matches this checkpoint — nothing to do.")
        raise typer.Exit(0)
    _echo("\nthis will change your working tree:")
    _echo(plan.stat or f"  {plan.changed} file(s) differ")
    if cp.adapters:
        _echo(f"\nwill also restore conversation state: {', '.join(cp.adapters)}")

    if not yes:
        if not typer.confirm("\nproceed?", default=False):
            _echo("aborted.")
            raise typer.Exit(1)

    hints = eng.rewind(cp)
    _echo(f"restored to checkpoint #{cp.id}.  (`stepback redo` to undo this rewind)")
    for h in hints:
        _echo(f"  resume conversation:  {h}")


@app.command()
def redo() -> None:
    """Reverse the most recent rewind."""
    eng = _engine(with_adapters=True)
    entry, hints = eng.redo()
    if entry is None:
        _echo("nothing to redo.")
        raise typer.Exit(1)
    _echo(f"redone — working tree restored to the pre-rewind state ({entry.time}).")
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
    eng = _engine()
    cp = eng.state.by_id(checkpoint_id)
    if cp is None:
        _echo("no such checkpoint.")
        raise typer.Exit(1)
    out = eng.diff_working(cp) if working else eng.diff(cp)
    if not out.strip():
        _echo("(no differences)")
    else:
        sys.stdout.write(out)


@app.command()
def status() -> None:
    """Show storage mode, current session, and detected agents."""
    eng = _engine(with_adapters=True)
    _echo(f"work tree : {eng.repo.work_tree}")
    _echo(f"mode      : {eng.repo.mode}  (git-dir: {eng.repo.git_dir})")
    _echo(f"session   : {eng.state.current_session or '(none)'}")
    _echo(f"checkpoints: {len(eng.state.checkpoints)}   redo stack: {len(eng.state.redo)}")
    detected = detect_adapters(eng.repo.work_tree)
    if detected:
        _echo("conversation adapters detected:")
        for a in detected:
            _echo(f"  - {a.name}")
    else:
        _echo("conversation adapters detected: none (file-only rewind)")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
