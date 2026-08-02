"""Scaffold smoke test: the package and CLI are importable and wired up correctly."""

from typer.testing import CliRunner

from code_review import __version__
from code_review.cli import app

runner = CliRunner()


def test_version_is_set() -> None:
    assert __version__


def test_cli_help() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "review" in result.stdout


def test_review_command_is_registered_and_reachable() -> None:
    # Milestone 12 (issues #31-#33) added `update`/`uninstall` alongside `review`, so
    # Typer's single-command collapse no longer applies -- `review` must now be named
    # explicitly as a subcommand (see `--help`'s "Usage: code-review [OPTIONS] COMMAND").
    # Milestone 13 (#40) wires `review` up for real; `CliRunner`'s captured stdio is never a
    # TTY, so this smoke test only proves the command is reachable and fails fast with a
    # controlled error rather than an unhandled exception -- see `tests/test_cli_review.py`
    # for full coverage of the TTY requirement and the wired pipeline run.
    result = runner.invoke(app, ["review", "some-branch", "--intent", "test"])
    assert result.exit_code != 0
    assert isinstance(result.exception, SystemExit)


# The old empty/whitespace `--intent` rejection tests that lived here (Milestone 3, issue
# #19) predate #40's TTY check, which now runs before intent validation -- under
# `CliRunner` (never a real TTY), any `--intent` value hits the TTY error first, so those
# assertions no longer exercised what they claimed. See `tests/test_cli_review.py`'s
# `test_review_tty_check_runs_before_intent_validation` (proves the ordering) and
# `test_review_rejects_empty_intent_under_a_real_terminal` (a real pty, where the
# `BadParameter` path is actually reachable) for the tests that replaced them.
