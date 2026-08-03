"""Shared real-git-repo fixture for `tests/steps/`.

`origin_and_checkout` builds two real local repos -- one standing in for the remote
("origin", on branch "main"), one the checkout under test with `git remote add origin
<path-to-origin>` wiring them together -- mirroring `tests/conftest.py`'s `fake_tool_repo`
convention of a real second local repo as a fake remote, just with plain `git` instead of
`uv`. Shared here (rather than duplicated) because both `test_rebase.py` (`RebaseStep`'s
own orchestration tests) and `test_gitutils.py` (direct tests of the shared git-subprocess
plumbing in `steps/gitutils.py`) build the identical topology.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


def _run_git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


def _init_repo(path: Path) -> None:
    path.mkdir()
    _run_git(["init", "-q"], path)
    _run_git(["config", "user.email", "test@example.com"], path)
    _run_git(["config", "user.name", "Test"], path)


def commit_file(repo: Path, filename: str, content: str, message: str) -> str:
    """Write `content` to `filename` in `repo`, commit it, and return the new commit SHA."""

    (repo / filename).write_text(content)
    _run_git(["add", filename], repo)
    _run_git(["commit", "-q", "-m", message], repo)
    return _run_git(["rev-parse", "HEAD"], repo).stdout.strip()


@pytest.fixture
def origin_and_checkout(tmp_path: Path) -> tuple[Path, Path]:
    """Build `origin` (a plain repo standing in for the remote, on branch "main" with one
    commit) and `checkout` (a second repo with `origin` wired in via `git remote add`,
    checked out onto its own branch "feature" at the point the two diverge). Individual
    tests advance one or both branches from here to build their scenario.
    """

    origin = tmp_path / "origin"
    _init_repo(origin)
    _run_git(["checkout", "-q", "-b", "main"], origin)
    commit_file(origin, "a.txt", "line-a\n", "initial")

    checkout = tmp_path / "checkout"
    _init_repo(checkout)
    _run_git(["remote", "add", "origin", str(origin)], checkout)
    _run_git(["fetch", "-q", "origin"], checkout)
    _run_git(["checkout", "-q", "-b", "feature", "origin/main"], checkout)

    return origin, checkout
