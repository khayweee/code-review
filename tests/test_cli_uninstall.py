"""Real-subprocess tests for `code-review uninstall` (issue #33).

Exercises the real `uv tool uninstall` subprocess against an isolated `UV_TOOL_DIR`/
`UV_TOOL_BIN_DIR` (never a mocked `uv`), installing `tests/conftest.py`'s throwaway
`fake_tool_repo` fixture first so there is something real to remove -- matching this
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


def _isolate_state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    state_dir = tmp_path / "state"
    monkeypatch.setenv("CODE_REVIEW_STATE_DIR", str(state_dir))
    return state_dir


def test_uninstall_removes_the_tool_and_the_state_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_tool_repo: Path
) -> None:
    tool_dir, bin_dir = _isolate_uv(tmp_path, monkeypatch)
    state_dir = _isolate_state_dir(tmp_path, monkeypatch)
    state_dir.mkdir(parents=True)
    (state_dir / "install.log").write_text("2026-01-01T00:00:00Z installed\n")
    _install(fake_tool_repo, tool_dir, bin_dir)
    assert (bin_dir / "code-review").exists()

    result = runner.invoke(app, ["uninstall"])

    assert result.exit_code == 0
    assert "uninstalled" in result.output.lower()
    assert not (bin_dir / "code-review").exists()
    assert not state_dir.exists()


def test_uninstall_when_nothing_installed_fails_with_a_clear_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate_uv(tmp_path, monkeypatch)
    _isolate_state_dir(tmp_path, monkeypatch)

    result = runner.invoke(app, ["uninstall"])

    assert result.exit_code != 0
    assert isinstance(result.exception, SystemExit)
    assert "Traceback" not in result.output
    assert "code-review uninstall failed" in result.output
    assert "not installed" in result.output
