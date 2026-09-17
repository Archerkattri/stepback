"""CLI behaviour tests: exit codes, dry-run, confirmation, clean error output.

Runs the Typer app in-process against a temporary working directory with an
isolated HOME, so no real Claude Code / Codex session state is detected or
touched.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from stepback.cli import app

runner = CliRunner()


def _bash_cmd() -> list[str] | None:
    """Locate a real bash for the agent snippets.

    Off Windows this is plain ``bash``.  On Windows a bare ``bash`` resolves
    to the WSL stub in System32 (which fails without an installed distro and
    sorts before any appended PATH entry), so only an explicit Git Bash is
    accepted.  Returns None on Windows without Git for Windows.
    """
    if sys.platform != "win32":
        return ["bash"]
    override = os.environ.get("STEPBACK_TEST_BASH", "")
    for candidate in (
        override,
        r"C:\Program Files\Git\bin\bash.exe",
        r"C:\Program Files\Git\usr\bin\bash.exe",
    ):
        if candidate and Path(candidate).is_file():
            return [candidate]
    return None


_BASH = _bash_cmd()


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
    if _BASH is None:
        pytest.skip("Git Bash not available on this Windows host")
    result = runner.invoke(app, ["run", "--", *_BASH, "-c", script])
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
    assert "watch backend:" in result.output
    assert "watch last ok:" in result.output
    assert "conversation: unavailable" in result.output
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


def test_rewind_failure_is_nonzero_and_actionable(
    workdir: Path, monkeypatch: pytest.MonkeyPatch
):
    (workdir / "a.txt").write_text("good\n")
    _run_agent("echo good > a.txt")
    extra = workdir / "extra.txt"
    extra.write_text("cannot delete\n")
    original_unlink = Path.unlink

    def deny_target(self: Path, missing_ok: bool = False) -> None:
        if self == extra:
            raise PermissionError("injected delete denial")
        original_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", deny_target)
    result = runner.invoke(app, ["rewind", "-y"])
    assert result.exit_code == 1
    assert "could not remove extra.txt" in result.output
    assert "restored to checkpoint" not in result.output
    assert extra.exists()


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
