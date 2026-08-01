"""Scaffold smoke test: the package and CLI are importable and wired up correctly."""

import re

from typer.testing import CliRunner

from code_review import __version__
from code_review.cli import app

runner = CliRunner()

# Typer/rich colorize and individually style CLI error output (down to splitting a
# `--flag`'s two hyphens across separate style spans), so substring assertions against
# `result.output` need the ANSI escape codes stripped first, or a plain-looking substring
# like "--intent" can silently fail to match even though the visible text is correct.
_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*m")


def _plain(output: str) -> str:
    return _ANSI_ESCAPE.sub("", output)


def test_version_is_set() -> None:
    assert __version__


def test_cli_help() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "review" in result.stdout


def test_review_command_not_implemented_yet() -> None:
    # Milestone 12 (issues #31-#33) added `update`/`uninstall` alongside `review`, so
    # Typer's single-command collapse no longer applies -- `review` must now be named
    # explicitly as a subcommand (see `--help`'s "Usage: code-review [OPTIONS] COMMAND").
    result = runner.invoke(app, ["review", "some-branch", "--intent", "test"])
    assert result.exit_code != 0
    assert isinstance(result.exception, NotImplementedError)


def test_review_command_rejects_empty_intent_before_constructing_anything() -> None:
    result = runner.invoke(app, ["review", "some-branch", "--intent", ""])
    output = _plain(result.output)

    assert result.exit_code == 2  # Typer's BadParameter exit code
    assert "--intent" in output
    assert "must be non-empty and not just whitespace" in output


def test_review_command_rejects_whitespace_only_intent_before_constructing_anything() -> None:
    result = runner.invoke(app, ["review", "some-branch", "--intent", "   "])
    output = _plain(result.output)

    assert result.exit_code == 2  # Typer's BadParameter exit code
    assert "--intent" in output
    assert "must be non-empty and not just whitespace" in output
