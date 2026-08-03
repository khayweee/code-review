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

`run_git`/`ref_sha`/`is_ancestor`/`conflicted_files` are `async def` (issue #62) -- every
scenario below runs its awaits inside a small `async def scenario()` closure driven by
`asyncio.run(...)`, matching this repo's existing convention for testing async code
(`tests/steps/test_rebase.py`'s `asyncio.run(RebaseStep().run(...))`) rather than pulling
in `pytest-asyncio`.

The "--- Ambient activity reporting" section near the bottom pins `run_git`'s own contract
in isolation (issue #64): bind/unbind `pipeline.step.current_activity_reporter` directly
(rather than through a full `run_steps([RebaseStep()], ctx)` call) to prove `run_git`
reports itself and derives its label correctly, independent of `RebaseStep`'s own
orchestration. `test_rebase.py` proves the full, real end-to-end call sequence and
ordering through `RebaseStep` itself.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path

import pytest

from code_review.pipeline.step import current_activity_reporter
from code_review.steps.gitutils import (
    conflicted_files,
    is_ancestor,
    rebase_in_progress,
    ref_sha,
    run_git,
)
from code_review.tui.activity import ActivityEvent, ActivityRelay
from tests.steps.conftest import commit_file

# --- run_git -----------------------------------------------------------------------------


def test_run_git_returns_completed_process_without_raising_on_nonzero_exit(
    origin_and_checkout: tuple[Path, Path],
) -> None:
    _origin, checkout = origin_and_checkout

    result = asyncio.run(run_git(["not-a-real-git-subcommand"], checkout))

    assert isinstance(result, subprocess.CompletedProcess)
    assert result.returncode != 0


def test_run_git_captures_stdout_as_text_on_success(
    origin_and_checkout: tuple[Path, Path],
) -> None:
    _origin, checkout = origin_and_checkout

    result = asyncio.run(run_git(["rev-parse", "--abbrev-ref", "HEAD"], checkout))

    assert result.returncode == 0
    assert result.stdout.strip() == "feature"


def test_run_git_does_not_block_the_event_loop_while_a_slow_git_subprocess_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #62's own acceptance criterion: a `run_git` call that runs longer than one
    tick interval must not freeze the rest of the asyncio event loop. Proven the same way
    `tests/test_cli_review.py`'s diff-fetch test proves its own non-blocking fix -- a
    concurrent ticker task counts how many times it gets scheduled while a call is in
    flight -- except here the slow part is a genuine subprocess (a fake `git` on `PATH`
    that just sleeps), not a monkeypatched Python function, since that's what actually
    proves `asyncio.create_subprocess_exec` replaced the old blocking `subprocess.run`. A
    blocking implementation would starve the ticker to ~0 ticks in the sleep window; the
    fixed implementation keeps it ticking throughout.
    """

    bin_dir = tmp_path / "slow_git_bin"
    bin_dir.mkdir()
    fake_git = bin_dir / "git"
    fake_git.write_text("#!/bin/sh\nsleep 1\nexit 0\n")
    fake_git.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")

    async def scenario() -> int:
        ticks = 0

        async def tick() -> None:
            nonlocal ticks
            while True:
                ticks += 1
                await asyncio.sleep(0.02)

        ticker = asyncio.create_task(tick())
        await run_git(["status"], tmp_path)
        ticker.cancel()
        return ticks

    tick_count = asyncio.run(scenario())

    assert tick_count > 10


# --- rebase_in_progress --------------------------------------------------------------------
# `rebase_in_progress` itself stays sync (no subprocess call -- pure filesystem check), but
# the `run_git` calls that set up/tear down each scenario are async and wrapped in
# `asyncio.run(...)` individually, matching `test_rebase.py`'s
# `asyncio.run(RebaseStep().run(...))` convention.


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
    asyncio.run(run_git(["fetch", "-q", "origin"], checkout))

    rebase = asyncio.run(run_git(["rebase", "origin/main"], checkout))
    assert rebase.returncode != 0

    assert rebase_in_progress(checkout) is True

    # Clean up so the repo isn't left mid-rebase for any test run after this one.
    asyncio.run(run_git(["rebase", "--abort"], checkout))
    assert rebase_in_progress(checkout) is False


# --- ref_sha -------------------------------------------------------------------------------


def test_ref_sha_resolves_an_existing_ref_to_its_sha(
    origin_and_checkout: tuple[Path, Path],
) -> None:
    _origin, checkout = origin_and_checkout

    expected = asyncio.run(run_git(["rev-parse", "HEAD"], checkout)).stdout.strip()

    assert asyncio.run(ref_sha("HEAD", checkout)) == expected


def test_ref_sha_returns_none_for_a_ref_that_does_not_exist(
    origin_and_checkout: tuple[Path, Path],
) -> None:
    _origin, checkout = origin_and_checkout

    assert asyncio.run(ref_sha("refs/heads/main", checkout)) is None


# --- is_ancestor ---------------------------------------------------------------------------


def test_is_ancestor_is_true_for_a_genuine_ancestor_and_for_equal_refs(
    origin_and_checkout: tuple[Path, Path],
) -> None:
    origin, checkout = origin_and_checkout

    origin_sha = commit_file(origin, "origin_only.txt", "from origin\n", "origin advances")
    asyncio.run(run_git(["fetch", "-q", "origin"], checkout))

    assert asyncio.run(is_ancestor(origin_sha, "origin/main", checkout)) is True
    assert asyncio.run(is_ancestor("origin/main", "origin/main", checkout)) is True


def test_is_ancestor_is_false_for_diverged_refs(
    origin_and_checkout: tuple[Path, Path],
) -> None:
    origin, checkout = origin_and_checkout

    commit_file(origin, "origin_only.txt", "from origin\n", "origin advances")
    asyncio.run(run_git(["fetch", "-q", "origin"], checkout))
    commit_file(checkout, "feature_only.txt", "from feature\n", "feature advances")

    assert asyncio.run(is_ancestor("origin/main", "HEAD", checkout)) is False


# --- conflicted_files ----------------------------------------------------------------------


def test_conflicted_files_lists_unresolved_paths_sorted_during_a_real_conflict(
    origin_and_checkout: tuple[Path, Path],
) -> None:
    origin, checkout = origin_and_checkout

    commit_file(origin, "a.txt", "line-a-from-origin\n", "origin changes a.txt")
    commit_file(checkout, "a.txt", "line-a-from-feature\n", "feature changes a.txt")
    asyncio.run(run_git(["fetch", "-q", "origin"], checkout))

    rebase = asyncio.run(run_git(["rebase", "origin/main"], checkout))
    assert rebase.returncode != 0
    assert rebase_in_progress(checkout) is True

    assert asyncio.run(conflicted_files(checkout)) == ["a.txt"]

    asyncio.run(run_git(["rebase", "--abort"], checkout))


def test_conflicted_files_is_empty_outside_a_conflict(
    origin_and_checkout: tuple[Path, Path],
) -> None:
    _origin, checkout = origin_and_checkout

    assert asyncio.run(conflicted_files(checkout)) == []


# --- Ambient activity reporting (issue #64) -------------------------------------------------
# `run_git` reads `pipeline.step.current_activity_reporter` directly rather than through a
# `StepContext` (it has none) -- bind/unbind it here the same way `executor.run_steps`
# does, to pin `run_git`'s own reporting contract down in isolation from `RebaseStep`.


def test_run_git_reports_a_started_and_finished_activity_when_a_reporter_is_bound(
    origin_and_checkout: tuple[Path, Path],
) -> None:
    _origin, checkout = origin_and_checkout
    relay = ActivityRelay()

    async def scenario() -> list[ActivityEvent]:
        token = current_activity_reporter.set(relay)
        try:
            await run_git(["status"], checkout)
        finally:
            current_activity_reporter.reset(token)
        return [await relay.next_event(), await relay.next_event()]

    started, finished = asyncio.run(scenario())

    assert started.status == "started"
    assert started.label == "git status"
    assert finished.status == "finished"
    assert finished.label == "git status"
    # Same activity span, not two unrelated ones.
    assert finished.activity_id == started.activity_id
    # `run_git` calls are flat, never nested inside one another.
    assert started.parent_id is None


def test_run_git_derives_the_label_from_subcommand_and_main_argument(
    origin_and_checkout: tuple[Path, Path],
) -> None:
    _origin, checkout = origin_and_checkout
    relay = ActivityRelay()

    async def scenario() -> str:
        token = current_activity_reporter.set(relay)
        try:
            await run_git(["fetch", "origin", "main"], checkout)
        finally:
            current_activity_reporter.reset(token)
        return (await relay.next_event()).label

    assert asyncio.run(scenario()) == "git fetch origin"


def test_run_git_reports_an_activity_even_when_the_call_fails(
    origin_and_checkout: tuple[Path, Path],
) -> None:
    """A nonzero-exit `git` call (an ordinary, non-exceptional outcome for `run_git`, per
    its own docstring) still gets a matching started/finished pair -- reporting is tied to
    the subprocess call's lifetime, not to whether it succeeded."""

    _origin, checkout = origin_and_checkout
    relay = ActivityRelay()

    async def scenario() -> tuple[ActivityEvent, ActivityEvent]:
        token = current_activity_reporter.set(relay)
        try:
            result = await run_git(["not-a-real-git-subcommand"], checkout)
        finally:
            current_activity_reporter.reset(token)
        assert result.returncode != 0
        return await relay.next_event(), await relay.next_event()

    started, finished = asyncio.run(scenario())

    assert started.status == "started"
    assert finished.status == "finished"
    assert finished.activity_id == started.activity_id
