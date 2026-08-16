"""Tests for `WorktreeStep` (worktree isolation) and its underlying git-subprocess helpers.

Real-git-repo convention throughout, matching `tests/steps/test_rebase.py`. `WorktreeStep`
never fetches or pushes -- unlike `RebaseStep`, it has no need for `tests/steps/conftest.py`'s
two-repo `origin_and_checkout` fixture (a real "origin" standing in for a remote); a single
local repo with two branches is enough to exercise every scenario here, so this file builds
its own `repo_with_feature_branch` fixture instead.

`WorktreeStep` makes no agent call, so `_SpyAgent` below -- mirroring `test_rebase.py`'s own
`_SpyAgent` -- fails loudly if it's ever invoked.

The "--- Activity reporting" section drives `WorktreeStep` through `executor.run_steps`
rather than calling `WorktreeStep.run` directly, as every scenario above it does: the
ambient `ActivityReporter` `gitutils.run_git` reads is only bound there (see
`pipeline/step.py`'s "Ambient reporting" section), so calling `WorktreeStep.run` directly
would never bind it and nothing would be reported.
"""

from __future__ import annotations

import asyncio
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from code_review.agent import RunOpts
from code_review.agent.base import OutputT, Result
from code_review.install_state import STATE_DIR_ENV_VAR, state_dir
from code_review.pipeline.executor import run_steps
from code_review.pipeline.step import StepContext, StepOutcome
from code_review.steps.intent import Intent
from code_review.steps.worktree import (
    BranchAlreadyCheckedOutError,
    WorktreeStep,
    create_worktree,
    remove_worktree,
    resolve_branch_head_short_sha,
    sanitize_branch_name_for_path,
    worktree_path_for_branch,
    worktrees_root,
)
from code_review.tui.activity import ActivityRelay
from code_review.tui.schemas import ActivityEvent

_STAND_IN_INTENT = Intent(summary="add retry logic", source="explicit", score=1.0)


@dataclass
class _SpyAgent:
    """Records whether `run` was ever invoked and fails loudly if it was -- `WorktreeStep`
    must never call through the agent it is given (see its module docstring: no agent/LLM
    call anywhere in `run`)."""

    run_called: bool = False

    async def run(self, opts: RunOpts[OutputT]) -> Result[OutputT]:
        self.run_called = True
        raise AssertionError("WorktreeStep must not call Agent.run")

    async def close(self) -> None:
        pass


def _run_git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


def _init_repo(path: Path) -> None:
    path.mkdir()
    _run_git(["init", "-q"], path)
    _run_git(["config", "user.email", "test@example.com"], path)
    _run_git(["config", "user.name", "Test"], path)


@pytest.fixture
def repo_with_feature_branch(tmp_path: Path) -> tuple[Path, str]:
    """A real repo on "main", with a "feature/change" branch one commit ahead, left checked
    out on "main" -- so "feature/change" is free for `create_worktree`/`WorktreeStep` to
    check out."""

    repo = tmp_path / "repo"
    _init_repo(repo)
    _run_git(["checkout", "-q", "-b", "main"], repo)
    (repo / "a.txt").write_text("line-a\n")
    _run_git(["add", "-A"], repo)
    _run_git(["commit", "-q", "-m", "initial"], repo)

    _run_git(["checkout", "-q", "-b", "feature/change"], repo)
    (repo / "a.txt").write_text("line-a\nline-b\n")
    _run_git(["add", "-A"], repo)
    _run_git(["commit", "-q", "-m", "add line-b"], repo)
    _run_git(["checkout", "-q", "main"], repo)

    return repo, "feature/change"


@pytest.fixture(autouse=True)
def _isolated_state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(STATE_DIR_ENV_VAR, str(tmp_path / "state"))


def _ctx(repo: Path, branch: str, agent: _SpyAgent) -> StepContext:
    return StepContext(cwd=repo, branch=branch, agent=agent, diff="", intent=_STAND_IN_INTENT)


async def _collect(ctx: StepContext) -> list[StepOutcome]:
    return [
        event.outcome
        async for event in run_steps([WorktreeStep()], ctx)
        if event.status == "completed"
    ]  # type: ignore[misc]


# --- sanitize_branch_name_for_path -------------------------------------------------------


def test_sanitize_branch_name_for_path_replaces_slashes() -> None:
    assert sanitize_branch_name_for_path("feature/change") == "feature-change"


def test_sanitize_branch_name_for_path_replaces_other_unsafe_characters() -> None:
    assert sanitize_branch_name_for_path("feat/a b~c") == "feat-a-b-c"


def test_sanitize_branch_name_for_path_leaves_a_plain_name_untouched() -> None:
    assert sanitize_branch_name_for_path("main") == "main"


def test_worktrees_root_lives_under_the_state_dir() -> None:
    assert worktrees_root() == state_dir() / "worktrees"


# --- resolve_branch_head_short_sha / worktree_path_for_branch (async plumbing) -----------


def test_resolve_branch_head_short_sha_matches_git_rev_parse_short(
    repo_with_feature_branch: tuple[Path, str],
) -> None:
    repo, branch = repo_with_feature_branch

    short_sha = asyncio.run(resolve_branch_head_short_sha(branch, repo))

    expected = _run_git(["rev-parse", "--short", branch], repo).stdout.strip()
    assert short_sha == expected
    assert short_sha != ""


def test_resolve_branch_head_short_sha_raises_for_an_unknown_branch(
    repo_with_feature_branch: tuple[Path, str],
) -> None:
    repo, _branch = repo_with_feature_branch

    with pytest.raises(RuntimeError, match="does-not-exist"):
        asyncio.run(resolve_branch_head_short_sha("does-not-exist", repo))


def test_worktree_path_for_branch_names_it_with_the_sanitized_branch_and_short_sha(
    repo_with_feature_branch: tuple[Path, str],
) -> None:
    repo, branch = repo_with_feature_branch

    path = asyncio.run(worktree_path_for_branch(branch, repo))

    short_sha = _run_git(["rev-parse", "--short", branch], repo).stdout.strip()
    assert path == worktrees_root() / f"code_review_feature-change_{short_sha}"
    assert path.parent == worktrees_root()


# --- create_worktree (async plumbing) -----------------------------------------------------


def test_create_worktree_checks_out_the_branch_for_real_not_detached(
    repo_with_feature_branch: tuple[Path, str],
) -> None:
    repo, branch = repo_with_feature_branch

    async def scenario() -> Path:
        worktree_path = await worktree_path_for_branch(branch, repo)
        await create_worktree(repo, worktree_path, branch)
        return worktree_path

    worktree_path = asyncio.run(scenario())

    assert worktree_path.is_dir()
    assert (worktree_path / "a.txt").read_text() == "line-a\nline-b\n"
    checked_out = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], worktree_path).stdout.strip()
    assert checked_out == branch


def test_create_worktree_raises_a_clear_error_when_the_branch_is_already_checked_out(
    repo_with_feature_branch: tuple[Path, str],
) -> None:
    repo, _branch = repo_with_feature_branch
    # "main" is already checked out in `repo` itself (the fixture's own main working copy).

    async def scenario() -> Path:
        worktree_path = await worktree_path_for_branch("main", repo)
        await create_worktree(repo, worktree_path, "main")
        return worktree_path

    with pytest.raises(BranchAlreadyCheckedOutError, match="already checked out"):
        asyncio.run(scenario())


def test_create_worktree_raises_a_plain_runtime_error_for_an_unknown_branch(
    repo_with_feature_branch: tuple[Path, str],
) -> None:
    repo, _branch = repo_with_feature_branch
    worktree_path = worktrees_root() / "code_review_does-not-exist_0000000"

    with pytest.raises(RuntimeError) as exc_info:
        asyncio.run(create_worktree(repo, worktree_path, "does-not-exist"))
    assert not isinstance(exc_info.value, BranchAlreadyCheckedOutError)


# --- remove_worktree (sync, cli.py's post-pipeline cleanup) -------------------------------


def test_remove_worktree_removes_a_clean_worktree(
    repo_with_feature_branch: tuple[Path, str],
) -> None:
    repo, branch = repo_with_feature_branch
    worktree_path = asyncio.run(worktree_path_for_branch(branch, repo))
    asyncio.run(create_worktree(repo, worktree_path, branch))

    remove_worktree("git", repo, worktree_path)

    assert not worktree_path.exists()
    # The branch is free again once its worktree is gone.
    _run_git(["worktree", "add", str(worktree_path), branch], repo)


def test_remove_worktree_force_removes_uncommitted_edits(
    repo_with_feature_branch: tuple[Path, str],
) -> None:
    """`remove_worktree` must still succeed (and discard) uncommitted edits left behind by
    e.g. an unfinished fix round -- `--keep-worktree` is the escape hatch for a user who
    wants those preserved instead, not this cleanup path."""

    repo, branch = repo_with_feature_branch
    worktree_path = asyncio.run(worktree_path_for_branch(branch, repo))
    asyncio.run(create_worktree(repo, worktree_path, branch))
    (worktree_path / "a.txt").write_text("uncommitted change\n")

    remove_worktree("git", repo, worktree_path)

    assert not worktree_path.exists()


def test_remove_worktree_raises_for_a_path_that_is_not_a_worktree(
    repo_with_feature_branch: tuple[Path, str], tmp_path: Path
) -> None:
    repo, _branch = repo_with_feature_branch

    with pytest.raises(RuntimeError):
        remove_worktree("git", repo, tmp_path / "not-a-worktree")


# --- WorktreeStep, driven through run_steps ------------------------------------------------


def test_worktree_step_creates_the_worktree_and_reports_it_via_cwd_override(
    repo_with_feature_branch: tuple[Path, str],
) -> None:
    repo, branch = repo_with_feature_branch
    agent = _SpyAgent()
    ctx = _ctx(repo, branch, agent)

    outcomes = asyncio.run(_collect(ctx))

    assert agent.run_called is False
    assert len(outcomes) == 1
    outcome = outcomes[0]
    assert outcome.needs_approval is False
    assert outcome.auto_fixable is False
    assert outcome.payload == []
    assert outcome.cwd_override is not None
    assert outcome.cwd_override.name.startswith("code_review_feature-change_")

    checked_out = _run_git(
        ["rev-parse", "--abbrev-ref", "HEAD"], outcome.cwd_override
    ).stdout.strip()
    assert checked_out == branch


def test_worktree_step_raises_when_branch_is_already_checked_out_elsewhere(
    repo_with_feature_branch: tuple[Path, str],
) -> None:
    """`repo`'s own HEAD is "main" (see the fixture) -- requesting a worktree for "main"
    collides with that, exactly the case a real `review BRANCH` run hits when `BRANCH` is
    already checked out in the user's main working copy. Raised straight out of
    `WorktreeStep.run`, uncaught -- `executor.run_steps` does nothing special with it, so it
    propagates out of the async generator like any other step failure."""

    repo, _feature_branch = repo_with_feature_branch
    agent = _SpyAgent()
    ctx = _ctx(repo, "main", agent)

    with pytest.raises(BranchAlreadyCheckedOutError):
        asyncio.run(_collect(ctx))


# --- Activity reporting --------------------------------------------------------------------


async def _drain_activity_events(relay: ActivityRelay) -> list[ActivityEvent]:
    """Drain every `ActivityEvent` already queued on `relay`. Called only after `run_steps`
    has fully returned, so every event `WorktreeStep`'s own `git` call reported is already
    queued -- a short timeout per `next_event()` call tells "nothing left" apart from
    "genuinely still coming", without hanging forever on the empty end."""

    events: list[ActivityEvent] = []
    while True:
        try:
            events.append(await asyncio.wait_for(relay.next_event(), timeout=0.05))
        except TimeoutError:
            return events


def test_worktree_step_reports_git_worktree_add_as_an_activity(
    repo_with_feature_branch: tuple[Path, str],
) -> None:
    repo, branch = repo_with_feature_branch
    agent = _SpyAgent()
    relay = ActivityRelay()
    ctx = StepContext(
        cwd=repo,
        branch=branch,
        agent=agent,
        diff="",
        intent=_STAND_IN_INTENT,
        activity_reporter=relay,
    )

    async def scenario() -> list[ActivityEvent]:
        async for _event in run_steps([WorktreeStep()], ctx):
            pass
        return await _drain_activity_events(relay)

    activity_events = asyncio.run(scenario())

    labels = [event.label for event in activity_events if event.status == "started"]
    assert any(label.startswith("git worktree add") for label in labels)
    assert all(event.error is None for event in activity_events if event.status == "finished")
