"""CLI behaviour tests: exit codes, dry-run, confirmation, clean error output.

Runs the Typer app in-process against a temporary working directory with an
isolated HOME, so no real Claude Code / Codex session state is detected or
touched.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from stepback.cli import app

runner = CliRunner()


@pytest.fixture()
def workdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    return work


def _run_agent(script: str) -> None:
    """Drive a checkpoint by running a bash snippet under `stepback run`."""
    result = runner.invoke(app, ["run", "--", "bash", "-c", script])
    assert result.exit_code == 0, result.output


def test_list_empty(workdir: Path):
    result = runner.invoke(app, ["list"])
    assert result.exit_code == 0
    assert "no checkpoints yet" in result.output


def test_run_then_list(workdir: Path):
    (workdir / "a.txt").write_text("one\n")
    _run_agent("echo two > a.txt; echo new > b.txt")
    result = runner.invoke(app, ["list"])
    assert result.exit_code == 0
    assert "#1" in result.output
    assert "ago" in result.output  # relative time rendering


def test_status_reports_mode_and_no_watcher(workdir: Path):
    (workdir / "a.txt").write_text("one\n")
    _run_agent("echo two > a.txt")
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    assert "shadow" in result.output
    assert "watcher    : not running" in result.output
    assert "adapters   :" in result.output


def test_rewind_dry_run_changes_nothing(workdir: Path):
    (workdir / "a.txt").write_text("good\n")
    _run_agent("echo good > a.txt")
    (workdir / "a.txt").write_text("BROKEN\n")

    result = runner.invoke(app, ["rewind", "--dry-run"])
    assert result.exit_code == 0
    assert "dry run" in result.output
    assert (workdir / "a.txt").read_text() == "BROKEN\n"  # untouched


def test_rewind_confirm_declined_aborts(workdir: Path):
    (workdir / "a.txt").write_text("good\n")
    _run_agent("echo good > a.txt")
    (workdir / "a.txt").write_text("BROKEN\n")

    result = runner.invoke(app, ["rewind"], input="n\n")
    assert result.exit_code == 1
    assert "aborted" in result.output
    assert (workdir / "a.txt").read_text() == "BROKEN\n"


def test_rewind_yes_restores(workdir: Path):
    (workdir / "a.txt").write_text("good\n")
    _run_agent("echo good > a.txt; echo x > extra.txt")
    (workdir / "a.txt").write_text("BROKEN\n")
    (workdir / "junk.txt").write_text("junk\n")

    result = runner.invoke(app, ["rewind", "-y"])
    assert result.exit_code == 0
    assert "restored to checkpoint" in result.output
    assert (workdir / "a.txt").read_text() == "good\n"
    assert not (workdir / "junk.txt").exists()


def test_rewind_then_redo(workdir: Path):
    (workdir / "a.txt").write_text("good\n")
    _run_agent("echo good > a.txt")
    (workdir / "a.txt").write_text("BROKEN\n")
    runner.invoke(app, ["rewind", "-y"])
    assert (workdir / "a.txt").read_text() == "good\n"

    result = runner.invoke(app, ["redo"])
    assert result.exit_code == 0
    assert "redone" in result.output
    assert (workdir / "a.txt").read_text() == "BROKEN\n"


def test_rewind_no_checkpoints_is_clean_error(workdir: Path):
    result = runner.invoke(app, ["rewind"])
    assert result.exit_code == 1
    assert "no checkpoints" in result.output
    assert "Traceback" not in result.output


def test_rewind_bad_id_is_clean_error(workdir: Path):
    (workdir / "a.txt").write_text("x\n")
    _run_agent("echo y > a.txt")
    result = runner.invoke(app, ["rewind", "999"])
    assert result.exit_code == 1
    assert "no such checkpoint" in result.output
    assert "Traceback" not in result.output


def test_redo_nothing_is_clean_error(workdir: Path):
    (workdir / "a.txt").write_text("x\n")
    _run_agent("echo y > a.txt")
    result = runner.invoke(app, ["redo"])
    assert result.exit_code == 1
    assert "nothing to redo" in result.output


def test_diff_shows_patch(workdir: Path):
    (workdir / "a.txt").write_text("one\n")
    _run_agent("printf 'one\\ntwo\\n' > a.txt")
    result = runner.invoke(app, ["diff", "1"])
    assert result.exit_code == 0
    assert "a.txt" in result.output


def test_diff_bad_id_is_clean_error(workdir: Path):
    result = runner.invoke(app, ["diff", "42"])
    assert result.exit_code == 1
    assert "no such checkpoint" in result.output


def test_diff_working_tree(workdir: Path):
    (workdir / "a.txt").write_text("one\n")
    _run_agent("echo one > a.txt")
    (workdir / "a.txt").write_text("mangled\n")
    result = runner.invoke(app, ["diff", "1", "--working"])
    assert result.exit_code == 0
    assert "a.txt" in result.output
    assert "one" in result.output


def test_run_missing_command_is_usage_error(workdir: Path):
    result = runner.invoke(app, ["run"])
    assert result.exit_code == 2
    assert "no command given" in result.output


def test_run_agent_not_found(workdir: Path):
    result = runner.invoke(app, ["run", "--", "this-command-does-not-exist-xyz"])
    assert result.exit_code == 127
    assert "command not found" in result.output


def test_no_args_shows_help(workdir: Path):
    result = runner.invoke(app, [])
    # no_args_is_help: prints help and exits non-zero
    assert "Usage" in result.output
    assert "run" in result.output
    assert "rewind" in result.output
