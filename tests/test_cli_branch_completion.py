"""Tests for `review BRANCH`'s shell completion (issue found while dogfooding Milestone 12's
install script: the argument had no completer, so shells fell back to file-path completion).

Runs `_complete_branch` against a real git repo with real branches -- no mocking `git`,
matching this project's real-subprocess testing convention (see tests/conftest.py).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from code_review.cli import _complete_branch


def _run_git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


@pytest.fixture
def repo_with_branches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _run_git(["init", "-q"], repo)
    _run_git(["config", "user.email", "test@example.com"], repo)
    _run_git(["config", "user.name", "Test"], repo)
    (repo / "README.md").write_text("hello\n")
    _run_git(["add", "-A"], repo)
    _run_git(["commit", "-q", "-m", "init"], repo)
    _run_git(["branch", "feature/one"], repo)
    _run_git(["branch", "feature/two"], repo)

    initial_branch = subprocess.run(
        ["git", "branch", "--show-current"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()

    monkeypatch.chdir(repo)
    return repo, initial_branch


def test_complete_branch_lists_all_local_branches(
    repo_with_branches: tuple[Path, str],
) -> None:
    _repo, initial_branch = repo_with_branches

    result = _complete_branch(None, [], "")

    assert set(result) == {initial_branch, "feature/one", "feature/two"}


def test_complete_branch_filters_by_prefix(repo_with_branches: tuple[Path, str]) -> None:
    result = _complete_branch(None, [], "feature/")

    assert set(result) == {"feature/one", "feature/two"}


def test_complete_branch_returns_empty_list_outside_a_git_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)

    assert _complete_branch(None, [], "") == []


def test_complete_branch_returns_empty_list_when_git_is_missing(
    repo_with_branches: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", "")

    assert _complete_branch(None, [], "") == []
