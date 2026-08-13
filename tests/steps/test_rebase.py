"""Tests for `RebaseStep`'s own orchestration/guard behavior (Milestone 4, issue #23).

Real-git-repo convention throughout, matching `tests/pipeline/test_executor.py`'s
docstring: no mocked `git` subprocess call anywhere. `origin_and_checkout` (shared via
`tests/steps/conftest.py` with `test_gitutils.py`) builds two real local repos -- one
standing in for the remote ("origin", on branch "main"), one the checkout under test with
`git remote add origin <path-to-origin>` wiring them together -- mirroring
`tests/conftest.py`'s `fake_tool_repo` convention of a real second local repo as a fake
remote (used there for `uv tool install git+file://...`), just with plain `git` instead of
`uv`.

Direct unit tests of the shared git-subprocess plumbing itself (`run_git`,
`rebase_in_progress`, `ref_sha`, `is_ancestor`, `conflicted_files`, now in
`steps/gitutils.py`) live in `test_gitutils.py`; this file only exercises them indirectly,
through `RebaseStep.run`'s own guard/orchestration decisions.

`RebaseStep` makes no agent call (see its module docstring), so `_SpyAgent` below -- a
genuine hand-written `Agent`-protocol implementation, not a mock library stand-in, matching
`tests/steps/test_intent.py`'s `_SpyAgent` -- fails loudly if `RebaseStep` ever calls it.

The "--- Activity reporting" section near the bottom (issue #64) drives `RebaseStep`
through `executor.run_steps` rather than calling `RebaseStep.run` directly, as every
scenario above it does: the ambient `ActivityReporter` `gitutils.run_git` reads is only
bound there (around each `step.run(ctx)` call -- see `pipeline/step.py`'s "Ambient
reporting" section), so calling `RebaseStep.run` directly would never bind it and nothing
would be reported.
"""

from __future__ import annotations

import asyncio
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from code_review.agent import RunOpts
from code_review.agent.base import OutputT, Result
from code_review.pipeline.executor import run_steps
from code_review.pipeline.findings import Finding
from code_review.pipeline.step import ApprovalResponse, StepContext, StepOutcome
from code_review.steps.intent import Intent
from code_review.steps.rebase import RebaseStep
from code_review.tui.activity import ActivityEvent, ActivityRelay
from tests.steps.conftest import commit_file

_STAND_IN_INTENT = Intent(summary="add retry logic", source="explicit", score=1.0)


@dataclass
class _SpyAgent:
    """Records whether `run` was ever invoked and fails loudly if it was -- `RebaseStep`
    must never call through the agent it is given (see its module docstring: no
    agent/LLM call anywhere in `run`)."""

    run_called: bool = False

    async def run(self, opts: RunOpts[OutputT]) -> Result[OutputT]:
        self.run_called = True
        raise AssertionError("RebaseStep must not call Agent.run")

    async def close(self) -> None:
        pass


def _run_git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


def _run_git_unchecked(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


def _init_repo(path: Path) -> None:
    path.mkdir()
    _run_git(["init", "-q"], path)
    _run_git(["config", "user.email", "test@example.com"], path)
    _run_git(["config", "user.name", "Test"], path)


def _ctx(checkout: Path, agent: _SpyAgent) -> StepContext:
    return StepContext(cwd=checkout, agent=agent, diff="", intent=_STAND_IN_INTENT)


def _assert_not_mid_rebase(checkout: Path) -> None:
    """The repo must never be left mid-rebase: neither state directory exists, and `git
    status --porcelain=v2 --branch` (which reports rebase state on its branch lines)
    shows an ordinary, non-rebasing branch."""

    assert not (checkout / ".git" / "rebase-merge").exists()
    assert not (checkout / ".git" / "rebase-apply").exists()

    status = _run_git(["status"], checkout).stdout
    assert "rebase in progress" not in status
    assert "working tree clean" in status or "nothing to commit" in status


# --- Scenario 1: already up to date --------------------------------------------------


def test_rebase_step_completes_with_no_findings_when_already_up_to_date(
    origin_and_checkout: tuple[Path, Path],
) -> None:
    _origin, checkout = origin_and_checkout
    agent = _SpyAgent()

    outcome = asyncio.run(RebaseStep().run(_ctx(checkout, agent)))

    assert agent.run_called is False
    assert isinstance(outcome, StepOutcome)
    assert outcome.needs_approval is False
    assert outcome.auto_fixable is False
    assert outcome.payload == []
    _assert_not_mid_rebase(checkout)


# --- Scenario 2: clean rebase, no conflict --------------------------------------------


def test_rebase_step_rebases_cleanly_onto_new_origin_commits_with_no_conflict(
    origin_and_checkout: tuple[Path, Path],
) -> None:
    origin, checkout = origin_and_checkout

    # origin/main gains a commit touching an unrelated file...
    origin_sha = commit_file(origin, "origin_only.txt", "from origin\n", "origin advances")
    # ...and the checkout's own branch has a commit touching a different unrelated file.
    commit_file(checkout, "feature_only.txt", "from feature\n", "feature advances")

    agent = _SpyAgent()
    outcome = asyncio.run(RebaseStep().run(_ctx(checkout, agent)))

    assert agent.run_called is False
    assert isinstance(outcome, StepOutcome)
    assert outcome.needs_approval is False
    assert outcome.auto_fixable is False
    assert outcome.payload == []
    _assert_not_mid_rebase(checkout)

    # Proves the rebase actually happened: origin's new commit is now an ancestor of the
    # checkout's HEAD.
    is_ancestor = _run_git_unchecked(["merge-base", "--is-ancestor", origin_sha, "HEAD"], checkout)
    assert is_ancestor.returncode == 0

    # The checkout's own commit is still present (rebased on top, not lost or squashed).
    log = _run_git(["log", "--oneline"], checkout).stdout
    assert "feature advances" in log


# --- Scenario 3: real conflict ----------------------------------------------------------


def test_rebase_step_aborts_and_reports_a_finding_per_conflicted_file_on_real_conflict(
    origin_and_checkout: tuple[Path, Path],
) -> None:
    origin, checkout = origin_and_checkout

    # origin/main changes the same line...
    commit_file(origin, "a.txt", "line-a-from-origin\n", "origin changes a.txt")
    # ...that the checkout's branch changes differently.
    commit_file(checkout, "a.txt", "line-a-from-feature\n", "feature changes a.txt")

    agent = _SpyAgent()
    outcome = asyncio.run(RebaseStep().run(_ctx(checkout, agent)))

    assert agent.run_called is False
    assert isinstance(outcome, StepOutcome)
    assert outcome.needs_approval is True
    assert outcome.auto_fixable is False

    findings = outcome.payload
    assert isinstance(findings, list)
    assert len(findings) == 1
    finding = findings[0]
    assert isinstance(finding, Finding)
    assert finding.action == "ask-user"
    assert "a.txt" in finding.description

    # Non-negotiable: `git rebase --abort` ran, the repo is back to a clean, non-mid-rebase
    # state, and the checkout is still on its own branch with its own commit intact.
    _assert_not_mid_rebase(checkout)
    branch = _run_git(["branch", "--show-current"], checkout).stdout.strip()
    assert branch == "feature"
    log = _run_git(["log", "--oneline", "-1"], checkout).stdout
    assert "feature changes a.txt" in log


# --- Scenario 4: a non-conflict rebase failure must not be misclassified --------------


def test_rebase_step_raises_rather_than_misclassify_a_dirty_working_tree_as_a_conflict(
    origin_and_checkout: tuple[Path, Path],
) -> None:
    """A dirty working tree makes `git rebase` refuse to even start -- exit nonzero with
    no `rebase-merge`/`rebase-apply` state to abort. `RebaseStep` must not treat this as
    a conflict finding; it has no classification for it and re-raises instead (see the
    module docstring's "Conflict detection" section)."""

    origin, checkout = origin_and_checkout
    commit_file(origin, "a.txt", "line-a-from-origin\n", "origin changes a.txt")

    # Uncommitted, unstaged change to a tracked file -- git refuses to start a rebase.
    (checkout / "a.txt").write_text("locally dirtied, never committed\n")

    agent = _SpyAgent()
    with pytest.raises(RuntimeError, match="without entering a conflict state"):
        asyncio.run(RebaseStep().run(_ctx(checkout, agent)))

    assert agent.run_called is False
    # Nothing to abort: no rebase state was ever entered.
    assert not (checkout / ".git" / "rebase-merge").exists()
    assert not (checkout / ".git" / "rebase-apply").exists()
    # The uncommitted change is untouched -- RebaseStep did not attempt to clean it up.
    assert (checkout / "a.txt").read_text() == "locally dirtied, never committed\n"


# --- Scenario 5: issue #24 guard fires --------------------------------------------------


def test_rebase_step_blocks_when_local_default_branch_carries_unpushed_commits_into_head(
    origin_and_checkout: tuple[Path, Path],
) -> None:
    """Guard fires: local `main` (distinct from `origin/main`) gains a commit never pushed
    anywhere, and the checkout's `feature` branch (HEAD) already incorporates that commit
    by merging local `main` into itself -- so local `main`'s tip is both strictly ahead of
    `origin/main` and an ancestor of HEAD, both conditions the guard requires. No rebase
    may be attempted at all."""

    origin, checkout = origin_and_checkout

    # Local `main`, branched from origin/main, gains a commit the developer never pushed.
    _run_git(["branch", "main", "origin/main"], checkout)
    _run_git(["checkout", "-q", "main"], checkout)
    local_only_sha = commit_file(
        checkout, "local_main_only.txt", "unpushed\n", "add local-main-only file"
    )

    # `feature` (the branch under review) already carries that unpushed tip -- e.g. the
    # developer merged their own local main into their feature branch.
    _run_git(["checkout", "-q", "feature"], checkout)
    _run_git(["merge", "-q", "--no-ff", "main", "-m", "merge local main into feature"], checkout)

    head_before = _run_git(["rev-parse", "HEAD"], checkout).stdout.strip()

    agent = _SpyAgent()
    outcome = asyncio.run(RebaseStep().run(_ctx(checkout, agent)))

    assert agent.run_called is False
    assert isinstance(outcome, StepOutcome)
    assert outcome.needs_approval is True
    assert outcome.auto_fixable is False

    findings = outcome.payload
    assert isinstance(findings, list)
    assert len(findings) == 1
    finding = findings[0]
    assert isinstance(finding, Finding)
    assert finding.action == "ask-user"
    # Names the offending commit (short SHA + subject) and the file it touches.
    assert local_only_sha[:7] in finding.description
    assert "add local-main-only file" in finding.description
    assert "local_main_only.txt" in finding.description

    # No rebase was attempted at all: HEAD is exactly where the merge left it (git rebase
    # would have moved it, even on a clean fast-forward), and there is no paused-rebase
    # state -- proof the step returned before ever calling `git rebase`.
    head_after = _run_git(["rev-parse", "HEAD"], checkout).stdout.strip()
    assert head_after == head_before
    _assert_not_mid_rebase(checkout)


# --- Scenario 6: guard does not fire, no local default branch exists --------------------


def test_rebase_step_does_not_block_and_rebases_normally_when_no_local_default_branch_exists(
    origin_and_checkout: tuple[Path, Path],
) -> None:
    """Regression test: `origin_and_checkout`'s own fixture shape (no local `main` branch
    in `checkout` at all, only the remote-tracking `origin/main`) is the ordinary case the
    guard must stay silent on. Mirrors Scenario 2's real clean-rebase setup so this proves
    both "the guard is a no-op" and "the ordinary rebase behavior it falls through to is
    unaffected by the new code path" in one test."""

    origin, checkout = origin_and_checkout
    assert (
        _run_git_unchecked(
            ["rev-parse", "--verify", "--quiet", "refs/heads/main"], checkout
        ).returncode
        != 0
    )

    origin_sha = commit_file(origin, "origin_only.txt", "from origin\n", "origin advances")
    commit_file(checkout, "feature_only.txt", "from feature\n", "feature advances")

    agent = _SpyAgent()
    outcome = asyncio.run(RebaseStep().run(_ctx(checkout, agent)))

    assert agent.run_called is False
    assert outcome.needs_approval is False
    assert outcome.auto_fixable is False
    assert outcome.payload == []
    _assert_not_mid_rebase(checkout)

    is_ancestor = _run_git_unchecked(["merge-base", "--is-ancestor", origin_sha, "HEAD"], checkout)
    assert is_ancestor.returncode == 0


# --- Scenario 7: guard does not fire, local default branch already equals origin --------


def test_rebase_step_does_not_block_when_local_default_branch_already_equals_origin(
    origin_and_checkout: tuple[Path, Path],
) -> None:
    """Regression test pinning down condition 1 in isolation: a local `main` branch
    exists, but its tip already equals `origin/main`'s -- not genuinely ahead. Local
    `main`'s tip is trivially an ancestor of HEAD (condition 2 holds, since `feature` was
    itself cut from that same commit), but equality is exactly what condition 1's
    strictness check excludes, so the guard must not fire on condition 2 alone."""

    _origin, checkout = origin_and_checkout
    _run_git(["branch", "main", "origin/main"], checkout)

    agent = _SpyAgent()
    outcome = asyncio.run(RebaseStep().run(_ctx(checkout, agent)))

    assert agent.run_called is False
    assert outcome.needs_approval is False
    assert outcome.auto_fixable is False
    assert outcome.payload == []
    _assert_not_mid_rebase(checkout)


# --- Scenario 8: guard does not fire, local default branch ahead but not incorporated ---


def test_rebase_step_does_not_block_when_local_default_branch_is_ahead_but_not_incorporated(
    origin_and_checkout: tuple[Path, Path],
) -> None:
    """Regression test pinning down condition 2 in isolation: local `main` gains a commit
    never pushed to `origin/main` (condition 1 holds -- it is genuinely ahead), but
    `feature` (HEAD) never merged it in, so local `main`'s tip is not an ancestor of HEAD.
    The guard must not fire on condition 1 alone; `feature` still equals `origin/main`, so
    this falls through to the ordinary already-up-to-date rebase outcome."""

    origin, checkout = origin_and_checkout
    _run_git(["branch", "main", "origin/main"], checkout)
    _run_git(["checkout", "-q", "main"], checkout)
    commit_file(checkout, "local_main_only.txt", "unpushed\n", "add local-main-only file")
    _run_git(["checkout", "-q", "feature"], checkout)

    agent = _SpyAgent()
    outcome = asyncio.run(RebaseStep().run(_ctx(checkout, agent)))

    assert agent.run_called is False
    assert outcome.needs_approval is False
    assert outcome.auto_fixable is False
    assert outcome.payload == []
    _assert_not_mid_rebase(checkout)


# --- default_branch override -----------------------------------------------------------


def test_rebase_step_default_branch_is_overridable_for_a_non_main_default(
    tmp_path: Path,
) -> None:
    """`default_branch` defaults to "main" (see `RebaseStep`'s field comment); a fixture
    or caller with a differently named default branch overrides it via the constructor,
    the same pattern as `ReviewStep(executable=...)`."""

    origin = tmp_path / "origin"
    _init_repo(origin)
    _run_git(["checkout", "-q", "-b", "trunk"], origin)
    commit_file(origin, "a.txt", "line-a\n", "initial")

    checkout = tmp_path / "checkout"
    _init_repo(checkout)
    _run_git(["remote", "add", "origin", str(origin)], checkout)
    _run_git(["fetch", "-q", "origin"], checkout)
    _run_git(["checkout", "-q", "-b", "feature", "origin/trunk"], checkout)

    agent = _SpyAgent()
    outcome = asyncio.run(RebaseStep(default_branch="trunk").run(_ctx(checkout, agent)))

    assert outcome.needs_approval is False
    assert outcome.payload == []


# --- Activity reporting (issue #64) -----------------------------------------------------


async def _approve(step_name: str, outcome: StepOutcome) -> ApprovalResponse:
    """A stub `on_approval_needed` (issue #80, updated for issue #81's `ApprovalResponse`)
    that always answers "approve" -- attached to every `_ctx_with_relay` context below so a
    scenario whose `RebaseStep` outcome parks (a real conflict, or the issue #24 guard
    firing) doesn't make `run_steps` fail closed (`executor.ApprovalNotAttachedError`)
    before this section's own assertions (about activity reporting, not the
    park/approve/skip/fix/abort flow itself -- that is `tests/pipeline/test_executor.py`'s
    job) get to run. Harmless for the one test in this section that calls `RebaseStep.run`
    directly instead of going through `run_steps` -- nothing ever reads this field on that
    path."""

    return ApprovalResponse(decision="approve")


def _ctx_with_relay(checkout: Path, agent: _SpyAgent, relay: ActivityRelay) -> StepContext:
    return StepContext(
        cwd=checkout,
        agent=agent,
        diff="",
        intent=_STAND_IN_INTENT,
        activity_reporter=relay,
        on_approval_needed=_approve,
    )


async def _drain_activity_events(relay: ActivityRelay) -> list[ActivityEvent]:
    """Drain every `ActivityEvent` already queued on `relay`. Called only after `run_steps`
    has fully returned, so every event `RebaseStep`'s own `git` calls reported is already
    queued -- a short timeout per `next_event()` call tells "nothing left" apart from
    "genuinely still coming", without hanging forever on the empty end.
    """

    events: list[ActivityEvent] = []
    while True:
        try:
            events.append(await asyncio.wait_for(relay.next_event(), timeout=0.05))
        except TimeoutError:
            return events


def test_rebase_step_reports_fetch_guard_rebase_conflict_read_and_abort_as_activities_in_order(
    origin_and_checkout: tuple[Path, Path],
) -> None:
    """Issue #64's own end-to-end proof, mirroring Scenario 3 (real conflict) above but
    driven through `executor.run_steps` with a real `ActivityRelay` attached: every
    underlying `git` call `RebaseStep.run` makes (fetch, the guard's own no-op check,
    the rebase, the conflict read, the abort) is reported as its own started/finished
    activity pair, in the exact order the calls actually happen -- with no changes at any
    `steps/rebase.py` call site."""

    origin, checkout = origin_and_checkout

    # origin/main changes the same line...
    commit_file(origin, "a.txt", "line-a-from-origin\n", "origin changes a.txt")
    # ...that the checkout's branch changes differently -- forces a real conflict.
    commit_file(checkout, "a.txt", "line-a-from-feature\n", "feature changes a.txt")

    agent = _SpyAgent()
    relay = ActivityRelay()
    ctx = _ctx_with_relay(checkout, agent, relay)

    async def scenario() -> tuple[StepOutcome, list[ActivityEvent]]:
        events = [event async for event in run_steps([RebaseStep()], ctx)]
        activity_events = await _drain_activity_events(relay)
        completed = events[-1]
        assert completed.status == "completed"
        assert completed.outcome is not None
        return completed.outcome, activity_events

    outcome, activity_events = asyncio.run(scenario())

    # Sanity check: still the same conflict outcome Scenario 3 pins down -- activity
    # reporting must not change RebaseStep's own conflict-detection behavior.
    assert outcome.needs_approval is True
    assert len(outcome.payload) == 1  # type: ignore[arg-type]

    started_labels = [event.label for event in activity_events if event.status == "started"]
    assert started_labels == [
        "git fetch origin main",
        "git rev-parse --verify --quiet refs/heads/main",  # the guard's no-op local-`main` check
        "git rebase origin/main",
        "git diff --name-only --diff-filter=U",  # conflicted_files, read before the abort
        "git rebase --abort",
    ]

    # Every started activity has a matching finished one, same id, in the same relative
    # order -- each is reported for the call's whole duration, not a bare point-in-time
    # marker.
    finished = [event for event in activity_events if event.status == "finished"]
    started = [event for event in activity_events if event.status == "started"]
    assert [event.activity_id for event in finished] == [event.activity_id for event in started]

    # RebaseStep's own git calls are flat siblings, never nested inside one another.
    assert all(event.parent_id is None for event in activity_events)


def test_rebase_step_reports_the_unpushed_local_default_guards_own_calls_when_it_fires(
    origin_and_checkout: tuple[Path, Path],
) -> None:
    """Mirrors Scenario 5 above (the issue #24 guard actually firing), proving the guard's
    own `ref_sha`/`is_ancestor`/`log`/`diff` calls -- not just the ordinary no-op check --
    are reported too, in the order they run, and that no `rebase` activity appears since
    the step returns before ever attempting one."""

    origin, checkout = origin_and_checkout

    _run_git(["branch", "main", "origin/main"], checkout)
    _run_git(["checkout", "-q", "main"], checkout)
    commit_file(checkout, "local_main_only.txt", "unpushed\n", "add local-main-only file")
    _run_git(["checkout", "-q", "feature"], checkout)
    _run_git(["merge", "-q", "--no-ff", "main", "-m", "merge local main into feature"], checkout)

    agent = _SpyAgent()
    relay = ActivityRelay()
    ctx = _ctx_with_relay(checkout, agent, relay)

    async def scenario() -> tuple[StepOutcome, list[ActivityEvent]]:
        events = [event async for event in run_steps([RebaseStep()], ctx)]
        activity_events = await _drain_activity_events(relay)
        completed = events[-1]
        assert completed.status == "completed"
        assert completed.outcome is not None
        return completed.outcome, activity_events

    outcome, activity_events = asyncio.run(scenario())

    assert outcome.needs_approval is True  # guard fired, same as Scenario 5

    # `merge-base --is-ancestor`/`log --oneline`/`diff --name-only` carry dynamic SHAs and
    # commit ranges computed at test-run time, so these are prefix comparisons rather than
    # exact matches; the rest of the command is fully static and asserted verbatim.
    started_labels = [event.label for event in activity_events if event.status == "started"]
    expected_prefixes = [
        "git fetch origin main",
        "git rev-parse --verify --quiet refs/heads/main",  # local `main`'s tip
        "git rev-parse --verify --quiet refs/remotes/origin/main",  # origin/main's tip
        "git merge-base --is-ancestor ",  # condition 1
        "git merge-base --is-ancestor ",  # condition 2
        "git log --oneline ",
        "git diff --name-only ",
    ]
    assert len(started_labels) == len(expected_prefixes)
    for label, prefix in zip(started_labels, expected_prefixes, strict=True):
        assert label.startswith(prefix), (label, prefix)
    assert all(event.parent_id is None for event in activity_events)


def test_rebase_step_reports_no_activity_when_run_directly_without_going_through_executor(
    origin_and_checkout: tuple[Path, Path],
) -> None:
    """Regression pin for the ambient-binding design itself: `RebaseStep.run` called
    directly (as every scenario above the "Activity reporting" section does) never binds
    `current_activity_reporter`, so a reporter attached to `ctx` is silently never used --
    proving the binding really lives in `executor.run_steps`, not in `RebaseStep`/`gitutils`
    themselves."""

    _origin, checkout = origin_and_checkout
    agent = _SpyAgent()
    relay = ActivityRelay()
    ctx = _ctx_with_relay(checkout, agent, relay)

    outcome = asyncio.run(RebaseStep().run(ctx))

    assert outcome.needs_approval is False
    activity_events = asyncio.run(_drain_activity_events(relay))
    assert activity_events == []
