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


def test_review_command_not_implemented_yet() -> None:
    result = runner.invoke(app, ["review", "some-branch", "--intent", "test"])
    assert result.exit_code != 0
