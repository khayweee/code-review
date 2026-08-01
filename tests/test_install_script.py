"""Real-subprocess tests for `scripts/install.sh` (issue #31).

Exercises the real script against a real `uv` binary and an isolated `UV_TOOL_DIR`/
`UV_TOOL_BIN_DIR`/`CODE_REVIEW_STATE_DIR` (never a mocked `uv` or a stubbed script) --
matching this project's existing "real subprocess, no mocking the external tool" testing
convention (see tests/pipeline/test_executor.py, tests/agent/test_claude_cli.py).
`tests/conftest.py`'s `fake_tool_repo` fixture stands in for this project's real GitHub
source via `CODE_REVIEW_INSTALL_SOURCE`, so these tests never touch the real
`khayweee/code-review` repo or this machine's real `~/.local/bin`/`~/.code-review`.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

INSTALL_SCRIPT = Path(__file__).parent.parent / "scripts" / "install.sh"


def _run(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/bin/sh", str(INSTALL_SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
    )


def _isolated_env(tmp_path: Path, source: str) -> dict[str, str]:
    return {
        **os.environ,
        "CODE_REVIEW_INSTALL_SOURCE": source,
        "CODE_REVIEW_STATE_DIR": str(tmp_path / "state"),
        "UV_TOOL_DIR": str(tmp_path / "tools"),
        "UV_TOOL_BIN_DIR": str(tmp_path / "bin"),
    }


def test_fails_fast_with_a_clear_message_when_uv_is_not_on_path(tmp_path: Path) -> None:
    empty_path_dir = tmp_path / "empty-path"
    empty_path_dir.mkdir()

    result = subprocess.run(
        ["/bin/sh", str(INSTALL_SCRIPT)],
        env={"PATH": str(empty_path_dir), "HOME": str(tmp_path / "home")},
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "not installed or not on PATH" in result.stderr
    assert "docs.astral.sh/uv" in result.stderr
    # Not a raw shell error like "command not found" bubbling up from deep in the script.
    assert "command not found" not in result.stderr


def test_installs_the_tool_creates_state_dir_and_prints_success(
    tmp_path: Path, fake_tool_repo: Path
) -> None:
    env = _isolated_env(tmp_path, f"git+file://{fake_tool_repo}")

    result = _run(env)

    assert result.returncode == 0
    assert "code-review is installed and ready" in result.stdout
    assert (tmp_path / "bin" / "code-review").exists()
    assert (tmp_path / "state").is_dir()
    assert (tmp_path / "state" / "install.log").exists()


def test_rerunning_an_already_installed_tool_succeeds_idempotently(
    tmp_path: Path, fake_tool_repo: Path
) -> None:
    env = _isolated_env(tmp_path, f"git+file://{fake_tool_repo}")

    first = _run(env)
    second = _run(env)

    assert first.returncode == 0
    assert second.returncode == 0
    assert "code-review is installed and ready" in second.stdout
    assert (tmp_path / "bin" / "code-review").exists()
