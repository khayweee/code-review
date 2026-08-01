"""Real-subprocess tests for `code-review update` (issue #32).

Exercises the real `uv tool upgrade` subprocess against an isolated `UV_TOOL_DIR`/
`UV_TOOL_BIN_DIR` (never a mocked `uv`), installing `tests/conftest.py`'s throwaway
`fake_tool_repo` fixture first so there is something real to upgrade -- matching this
project's existing "real subprocess, no mocking the external tool" testing convention
(see tests/pipeline/test_executor.py, tests/agent/test_claude_cli.py).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from code_review.cli import app
from tests.conftest import bump_fake_tool_repo

runner = CliRunner()


def _install(repo: Path, tool_dir: Path, bin_dir: Path) -> None:
    env = {**os.environ, "UV_TOOL_DIR": str(tool_dir), "UV_TOOL_BIN_DIR": str(bin_dir)}
    subprocess.run(
        ["uv", "tool", "install", f"git+file://{repo}"],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )


def _isolate_uv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    tool_dir = tmp_path / "tools"
    bin_dir = tmp_path / "bin"
    monkeypatch.setenv("UV_TOOL_DIR", str(tool_dir))
    monkeypatch.setenv("UV_TOOL_BIN_DIR", str(bin_dir))
    return tool_dir, bin_dir


def test_update_reports_already_up_to_date(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_tool_repo: Path
) -> None:
    tool_dir, bin_dir = _isolate_uv(tmp_path, monkeypatch)
    _install(fake_tool_repo, tool_dir, bin_dir)

    result = runner.invoke(app, ["update"])

    assert result.exit_code == 0
    assert "already up to date" in result.output


def test_update_reports_the_version_it_moved_to_after_an_upstream_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_tool_repo: Path
) -> None:
    tool_dir, bin_dir = _isolate_uv(tmp_path, monkeypatch)
    _install(fake_tool_repo, tool_dir, bin_dir)
    bump_fake_tool_repo(fake_tool_repo)

    result = runner.invoke(app, ["update"])

    assert result.exit_code == 0
    assert "upgraded to 0.1.0" in result.output


def test_update_surfaces_underlying_failure_as_a_clear_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Nothing installed under this isolated tool dir -- `uv tool upgrade` itself fails.
    _isolate_uv(tmp_path, monkeypatch)

    result = runner.invoke(app, ["update"])

    assert result.exit_code != 0
    # A controlled `typer.Exit` (SystemExit), not an unhandled exception with a traceback.
    assert isinstance(result.exception, SystemExit)
    assert "Traceback" not in result.output
    assert "code-review update failed" in result.output
    assert "not installed" in result.output
