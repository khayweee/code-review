"""Direct tests for `steps/gitutils.py`'s shared git-subprocess plumbing.

Real-git-repo convention throughout, matching `tests/pipeline/test_executor.py`'s
docstring: no mocked `git` subprocess call anywhere. Reuses `tests/steps/conftest.py`'s
`origin_and_checkout` fixture (shared with `test_rebase.py`, which builds the identical
two-repo topology for its own `RebaseStep.run` orchestration tests) rather than
duplicating it here.

Before this module existed, `run_git`/`rebase_in_progress`/`ref_sha`/`is_ancestor`/
`conflicted_files` had no tests that exercised them directly in isolation -- every scenario
was covered only indirectly, through `RebaseStep.run` in `test_rebase.py`. This file adds
that direct coverage now that the functions are shared, reusable plumbing rather than
rebase-step-private; `test_rebase.py` keeps its existing integration-style coverage of the
same functions as exercised through `RebaseStep.run`.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from code_review.steps.gitutils import (
    conflicted_files,
    is_ancestor,
    rebase_in_progress,
    ref_sha,
    run_git,
)
from tests.steps.conftest import commit_file

# --- run_git -----------------------------------------------------------------------------


def test_run_git_returns_completed_process_without_raising_on_nonzero_exit(
    origin_and_checkout: tuple[Path, Path],
) -> None:
    _origin, checkout = origin_and_checkout

    result = run_git(["not-a-real-git-subcommand"], checkout)

    assert isinstance(result, subprocess.CompletedProcess)
    assert result.returncode != 0


def test_run_git_captures_stdout_as_text_on_success(
    origin_and_checkout: tuple[Path, Path],
) -> None:
    _origin, checkout = origin_and_checkout

    result = run_git(["rev-parse", "--abbrev-ref", "HEAD"], checkout)

    assert result.returncode == 0
    assert result.stdout.strip() == "feature"


# --- rebase_in_progress --------------------------------------------------------------------


def test_rebase_in_progress_is_false_for_an_ordinary_clean_checkout(
    origin_and_checkout: tuple[Path, Path],
) -> None:
    _origin, checkout = origin_and_checkout

    assert rebase_in_progress(checkout) is False


def test_rebase_in_progress_is_true_once_a_real_rebase_pauses_on_conflict(
    origin_and_checkout: tuple[Path, Path],
) -> None:
    origin, checkout = origin_and_checkout

    commit_file(origin, "a.txt", "line-a-from-origin\n", "origin changes a.txt")
    commit_file(checkout, "a.txt", "line-a-from-feature\n", "feature changes a.txt")
    run_git(["fetch", "-q", "origin"], checkout)

    rebase = run_git(["rebase", "origin/main"], checkout)
    assert rebase.returncode != 0

    assert rebase_in_progress(checkout) is True

    # Clean up so the repo isn't left mid-rebase for any test run after this one.
    run_git(["rebase", "--abort"], checkout)
    assert rebase_in_progress(checkout) is False


# --- ref_sha -------------------------------------------------------------------------------


def test_ref_sha_resolves_an_existing_ref_to_its_sha(
    origin_and_checkout: tuple[Path, Path],
) -> None:
    _origin, checkout = origin_and_checkout

    expected = run_git(["rev-parse", "HEAD"], checkout).stdout.strip()

    assert ref_sha("HEAD", checkout) == expected


def test_ref_sha_returns_none_for_a_ref_that_does_not_exist(
    origin_and_checkout: tuple[Path, Path],
) -> None:
    _origin, checkout = origin_and_checkout

    assert ref_sha("refs/heads/main", checkout) is None


# --- is_ancestor ---------------------------------------------------------------------------


def test_is_ancestor_is_true_for_a_genuine_ancestor_and_for_equal_refs(
    origin_and_checkout: tuple[Path, Path],
) -> None:
    origin, checkout = origin_and_checkout

    origin_sha = commit_file(origin, "origin_only.txt", "from origin\n", "origin advances")
    run_git(["fetch", "-q", "origin"], checkout)

    assert is_ancestor(origin_sha, "origin/main", checkout) is True
    assert is_ancestor("origin/main", "origin/main", checkout) is True


def test_is_ancestor_is_false_for_diverged_refs(
    origin_and_checkout: tuple[Path, Path],
) -> None:
    origin, checkout = origin_and_checkout

    commit_file(origin, "origin_only.txt", "from origin\n", "origin advances")
    run_git(["fetch", "-q", "origin"], checkout)
    commit_file(checkout, "feature_only.txt", "from feature\n", "feature advances")

    assert is_ancestor("origin/main", "HEAD", checkout) is False


# --- conflicted_files ----------------------------------------------------------------------


def test_conflicted_files_lists_unresolved_paths_sorted_during_a_real_conflict(
    origin_and_checkout: tuple[Path, Path],
) -> None:
    origin, checkout = origin_and_checkout

    commit_file(origin, "a.txt", "line-a-from-origin\n", "origin changes a.txt")
    commit_file(checkout, "a.txt", "line-a-from-feature\n", "feature changes a.txt")
    run_git(["fetch", "-q", "origin"], checkout)

    rebase = run_git(["rebase", "origin/main"], checkout)
    assert rebase.returncode != 0
    assert rebase_in_progress(checkout) is True

    assert conflicted_files(checkout) == ["a.txt"]

    run_git(["rebase", "--abort"], checkout)


def test_conflicted_files_is_empty_outside_a_conflict(
    origin_and_checkout: tuple[Path, Path],
) -> None:
    _origin, checkout = origin_and_checkout

    assert conflicted_files(checkout) == []
