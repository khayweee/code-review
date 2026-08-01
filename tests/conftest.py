"""Shared fixtures across `tests/`.

`fake_tool_repo` builds a throwaway git repo that is a real, buildable Python package
named literally `code-review` -- installable via `uv tool install git+file://<path>`
against real `uv` subprocesses. It stands in for this project's real GitHub source in
Milestone 12's install/update/uninstall tests (issues #31-#33), so those tests never touch
this machine's real `~/.local/share/uv/tools`, real `~/.local/bin`, or the real
`khayweee/code-review` GitHub repo, matching this project's "real subprocess, no mocking
the external tool" testing convention (see tests/pipeline/test_executor.py).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

_PYPROJECT = """\
[project]
name = "code-review"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = []

[project.scripts]
code-review = "fake_code_review:main"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["fake_code_review"]
"""


def _run_git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


def bump_fake_tool_repo(repo: Path) -> None:
    """Commit a new revision to `repo` with no version bump -- `uv tool upgrade` still
    detects it, since a git source is resolved against the remote ref, not the
    `pyproject.toml` version (see cli.py's `_describe_upgrade` docstring context: this
    project deliberately has no automated version-bumping, issue #28)."""
    (repo / "fake_code_review" / "__init__.py").write_text(
        "def main() -> None:\n    print('fake code-review v2')\n"
    )
    _run_git(["add", "-A"], repo)
    _run_git(["commit", "-q", "-m", "v2"], repo)


@pytest.fixture
def fake_tool_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "fake-code-review-repo"
    repo.mkdir()
    _run_git(["init", "-q"], repo)
    _run_git(["config", "user.email", "test@example.com"], repo)
    _run_git(["config", "user.name", "Test"], repo)

    (repo / "fake_code_review").mkdir()
    (repo / "fake_code_review" / "__init__.py").write_text(
        "def main() -> None:\n    print('fake code-review v1')\n"
    )
    (repo / "pyproject.toml").write_text(_PYPROJECT)

    _run_git(["add", "-A"], repo)
    _run_git(["commit", "-q", "-m", "v1"], repo)
    return repo
