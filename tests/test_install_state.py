"""Tests for the install-lifecycle state directory helper (Milestone 12, issue #31)."""

from __future__ import annotations

from pathlib import Path

import pytest

from code_review import install_state


def test_state_dir_defaults_to_home_dot_code_review(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(install_state.STATE_DIR_ENV_VAR, raising=False)

    assert install_state.state_dir() == Path.home() / ".code-review"


def test_state_dir_honors_env_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    override = tmp_path / "state"
    monkeypatch.setenv(install_state.STATE_DIR_ENV_VAR, str(override))

    assert install_state.state_dir() == override
