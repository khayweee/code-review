"""Dev-only TUI preview: launches `ReviewApp` against a hand-written fake event stream, with
no real git repo, `--intent`, or `claude` subprocess involved anywhere -- `branch` below is a
hardcoded display string, not read from an actual checkout, so the Pipeline box's border
subtitle has something to show while eyeballing styling.

Exists so a styling/layout change to `tui/widgets.py`/`app.py`/`*.tcss` can be eyeballed in
one command instead of a real `code-review review BRANCH --intent ...` run, which needs a
real branch and shells out to the `claude` CLI for every step. This mirrors exactly how
`tests/tui/test_app.py` drives `ReviewApp` -- a hand-built `AsyncIterator[StepEvent]`, per
`app.py`'s own injection-seam docstring -- just left running on a timer instead of driven
through `Pilot` and asserted against.

ReviewStep's outcome has `needs_approval=True`, and a real `ApprovalRelay` (issue #80/#81)
is wired into `ReviewApp` exactly the way `cli.py` wires one for a real run -- so this
preview also drives the parked `FindingsBox`'s inline decision selector (issue #87,
superseding the old `ApprovalPromptScreen` modal): `_fake_events` awaits
`approval_relay.request_approval(...)` after ReviewStep's "completed" event, the same call
`pipeline.executor.run_steps` makes, and reacts to whatever the human answers (approve/skip/
fix/abort) the same way that executor does -- "fix" re-runs the step for one simulated round
before moving on, "abort" raises `RunAbortedError`, matching real behavior.

Not installed as a package entry point and not covered by `tests/` -- it is a manual dev
tool, not a shipped code path.

Usage:
    uv run python scripts/preview_tui.py          # everything succeeds
    uv run python scripts/preview_tui.py --fail    # ReviewStep raises, preview the failure path
"""

from __future__ import annotations

import argparse
import asyncio
import os
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path

from code_review.agent import Usage
from code_review.pipeline.executor import RunAbortedError
from code_review.pipeline.findings import Finding
from code_review.pipeline.schemas import StepEvent
from code_review.pipeline.step import StepOutcome
from code_review.steps.intent import Intent
from code_review.steps.pr import PullRequestOutcome
from code_review.steps.registry import STEP_DISPLAY_NAMES, STEP_REGISTRY
from code_review.steps.review import ReviewOutput
from code_review.steps.test_sufficiency import TestArtifact, TestSufficiencyOutput
from code_review.steps.worktree import sanitize_branch_name_for_path, worktrees_root
from code_review.tui.activity import ActivityRelay
from code_review.tui.app import ReviewApp
from code_review.tui.approval_relay import ApprovalRelay

# Long enough that each step's "running" state is visible on screen, short enough that the
# whole preview run finishes in a few seconds.
_STEP_DELAY = 3.6

# No-findings outcome, generic across the steps whose payload is a plain list[Finding]
# (RebaseStep, PRStep) -- see StepOutcome.payload's closed-union docstring.
_NO_FINDINGS = StepOutcome(needs_approval=False, auto_fixable=False, payload=[])

_INTENT_OUTCOME = StepOutcome(
    needs_approval=False,
    auto_fixable=False,
    payload=Intent(
        summary="Preview the TUI's styling without a real git repo or `claude` subprocess.",
        source="explicit",
        score=1.0,
    ),
)

_REVIEW_FINDINGS = StepOutcome(
    needs_approval=True,
    auto_fixable=False,
    payload=ReviewOutput(
        findings=[
            Finding(
                severity="error",
                description="`_run_pipeline` swallows the `to_thread` exception silently.",
                action="ask-user",
                review_scope="source",
                location="src/code_review/cli.py:249",
                suggestions=[
                    "Log the exception before re-raising.",
                    "Propagate it to the caller instead of swallowing it.",
                ],
            ),
            Finding(
                severity="warning",
                description="Docstring drifted from the current behavior.",
                action="auto-fix",
                review_scope="source",
                location="src/code_review/tui/app.py:120",
                suggestions=["Update the docstring to match the current behavior."],
            ),
            Finding(
                severity="info",
                description="Consider a shorter variable name here.",
                action="no-op",
                review_scope="source",
            ),
        ],
        risk_level="medium",
        risk_rationale="One unhandled-exception path found; nothing else stood out.",
    ),
    # Standing in for ReviewStep.run's real `result.usage` (see pipeline/AGENTS.md's "Run
    # report" section) -- gives the Status box's token-usage block real numbers to render.
    usage=Usage(input_tokens=8400, output_tokens=1250, total_cost_usd=0.0623),
)

# The fix round's own settle-outcome (see the "fix" branch below) -- a distinct Usage from
# _REVIEW_FINDINGS's, so the Status box's ReviewStep row visibly sums two rounds together,
# previewing build_run_report's own multi-round-summing behavior.
_REVIEW_FIX_ROUND_OUTCOME = StepOutcome(
    needs_approval=False,
    auto_fixable=False,
    payload=[],
    usage=Usage(input_tokens=2100, output_tokens=340, total_cost_usd=0.0158),
)

_TEST_SUFFICIENCY_OUTCOME = StepOutcome(
    needs_approval=False,
    auto_fixable=False,
    payload=TestSufficiencyOutput(
        findings=[],
        tested=["ReviewApp renders a completed step's findings box."],
        testing_summary="Existing TUI tests cover the happy path end to end.",
        artifacts=[
            TestArtifact(
                kind="existing-test",
                description="Pilot-driven test asserts the findings box renders.",
                location="tests/tui/test_app.py:42",
            )
        ],
    ),
    usage=Usage(input_tokens=5100, output_tokens=780, total_cost_usd=0.0349),
)

# `created=True` -- no existing PR for "fix/nil-check" here (mirrors PRStep.run's
# find_pull_request_for_branch returning None, taking the create_pull_request branch), so
# the Pipeline box's "Pull Request" row renders "→ opened <url>" (see tui/state.py's
# _detail_for_completed_payload).
_PR_OUTCOME = StepOutcome(
    needs_approval=False,
    auto_fixable=False,
    payload=PullRequestOutcome(
        url="https://github.com/example-org/code-review/pull/42",
        number=42,
        created=True,
    ),
)


async def _simulate_worktree_activity(activity_relay: ActivityRelay) -> None:
    """`WorktreeStep`'s real single `git worktree add` call, reported the same way every
    other step's `steps/gitutils.py`-backed git call is -- target path (relative to `cwd`,
    matching `create_worktree`'s own `os.path.relpath` -- see `steps/worktree.py`) and
    branch included, not just the bare subcommand."""

    branch_segment = sanitize_branch_name_for_path("fix/nil-check")
    worktree_path = worktrees_root() / f"code_review_{branch_segment}_a1b2c3d"
    relative_worktree_path = os.path.relpath(worktree_path, Path.cwd())
    async with activity_relay.activity(f"git worktree add {relative_worktree_path} fix/nil-check"):
        await asyncio.sleep(0.4)


async def _simulate_git_activity(activity_relay: ActivityRelay) -> None:
    """`RebaseStep`'s real individual `git fetch`/`git rebase` calls, each its own
    `activity()` span reported ambiently by `steps/gitutils.py`'s `run_git` -- reported here
    the same way, through `activity_relay.activity(label)`.
    """

    async with activity_relay.activity("git fetch origin"):
        await asyncio.sleep(0.5)
    async with activity_relay.activity("git rebase origin/main"):
        await asyncio.sleep(0.5)


async def _simulate_agent_call(
    activity_relay: ActivityRelay, label: str, tool_calls: list[tuple[str, float]]
) -> None:
    """`ReviewStep`/`TestSufficiencyStep`'s real shape: one coarse `ctx.report_activity(label)`
    span for the whole agent call, containing zero or more nested tool-call spans --
    `steps/tool_activity.py`'s `tool_stream_relay` opens each one on the streamed `TOOL_USE`
    and closes it on the matching `TOOL_RESULT`, so its reported duration is the real elapsed
    time of that call, not an instant one-shot log. Reported here identically via
    `activity_relay.start(...)`/`.finish(...)`, with each `(label, seconds)` pair in
    `tool_calls` sleeping a different amount so the activity pane visibly shows distinct,
    real per-row durations -- exactly what a real long-running `Bash` call next to a fast
    `Read` call looks like live.
    """

    async with activity_relay.activity(label):
        await asyncio.sleep(0.3)
        for tool_call, seconds in tool_calls:
            activity_id = await activity_relay.start(tool_call)
            await asyncio.sleep(seconds)
            await activity_relay.finish(activity_id, tool_call)


async def _simulate_pr_activity(activity_relay: ActivityRelay) -> None:
    """`PRStep`'s real shape, in call order: `_build_body`'s `git diff --name-status` call
    (`steps/gitutils.py`'s `run_git`, ambient span), `find_pull_request_for_branch`'s `gh pr
    view` call, and (no existing PR found here, so the create branch runs)
    `create_pull_request`'s `gh pr create` call -- each its own ambient span via
    `scm/github.py`'s `_run_gh`/`gitutils.py`'s `run_git`, reported here the same way.
    """

    async with activity_relay.activity("git diff --name-status origin/main...fix/nil-check"):
        await asyncio.sleep(0.3)
    async with activity_relay.activity("gh pr view"):
        await asyncio.sleep(0.4)
    async with activity_relay.activity("gh pr create"):
        await asyncio.sleep(0.5)


async def _fake_events(
    fail: bool, activity_relay: ActivityRelay, approval_relay: ApprovalRelay
) -> AsyncIterator[StepEvent]:
    """Steps in `STEP_REGISTRY` order, all six with a class today (see `registry.py`), so
    none render as a pending placeholder.

    Each step's `simulate_activity` callable stands in for the nested sub-step activity
    issues #64/#65 report for real (`WorktreeStep`'s single `git worktree add` span via
    `_simulate_worktree_activity`; `RebaseStep`'s individual `git fetch`/`git rebase` spans
    via `_simulate_git_activity`; `ReviewStep`/`TestSufficiencyStep`'s one coarse agent-call
    span plus nested per-tool-call spans via `_simulate_agent_call`; `PRStep`'s `git
    diff`/`gh pr view`/`gh pr create` spans via `_simulate_pr_activity`) -- reported here the
    same way, through `activity_relay.activity(label)`/`.start(label)`/`.finish(activity_id,
    label)`, so `ReviewApp`'s activity worker (`app.py`'s `_consume_activities`) and
    `state.py`'s `backfill_activities` render them exactly as they would a real run's,
    including each tool-call row's real (not instant) elapsed duration. `IntentStep` reports
    none, matching reality (no subprocess). `PRStep`'s outcome also carries a real
    `PullRequestOutcome` payload (`_PR_OUTCOME`), so the Pipeline box's "Pull Request" row
    renders its "→ opened <url>" detail text too (`tui/state.py`'s
    `_detail_for_completed_payload`).

    `_REVIEW_FINDINGS`/`_TEST_SUFFICIENCY_OUTCOME`/`_REVIEW_FIX_ROUND_OUTCOME` each carry a
    `Usage`, standing in for a real `Result.usage` (`pipeline/AGENTS.md`'s "Run report"
    section) -- once the run ends, the Status box's token-usage block sums them, including
    across ReviewStep's own two rounds when a "fix" is requested, previewing
    `pipeline.run_report.build_run_report`'s multi-round-summing exactly as it behaves live.

    ReviewStep's outcome carries `needs_approval=True`, so once its "completed" event is
    yielded this awaits `approval_relay.request_approval(...)` -- exactly the call
    `pipeline.executor.run_steps` makes at a real park -- and blocks until `ReviewApp`'s
    approval-relay worker resolves it from a human answering the parked `FindingsBox`'s
    inline decision selector (issue #87). This mirrors that executor's own approve/skip/
    fix/abort handling: "fix" re-runs the step for one simulated round (settling on
    `_REVIEW_FIX_ROUND_OUTCOME`, standing in for a fix that resolved the findings) before
    moving on; "abort" raises `RunAbortedError`, matching the real failure path this
    preview's `--fail` flag also exercises; "approve"/"skip" both simply continue to the
    next step, the same "presentational only" distinction the real executor draws.
    """

    steps: list[tuple[str, StepOutcome, Callable[[ActivityRelay], Awaitable[None]] | None]] = [
        ("WorktreeStep", _NO_FINDINGS, _simulate_worktree_activity),
        ("IntentStep", _INTENT_OUTCOME, None),
        ("RebaseStep", _NO_FINDINGS, _simulate_git_activity),
        (
            "ReviewStep",
            _REVIEW_FINDINGS,
            lambda relay: _simulate_agent_call(
                relay,
                "Agent: reviewing diff via claude",
                [
                    ("Tool: Read(src/code_review/cli.py)", 0.3),
                    ("Tool: Bash(timeout 300 uv run pytest tests/ -q)", 1.4),
                ],
            ),
        ),
        (
            "TestSufficiencyStep",
            _TEST_SUFFICIENCY_OUTCOME,
            lambda relay: _simulate_agent_call(
                relay,
                "Agent: assessing test sufficiency via claude",
                [("Tool: Read(tests/tui/test_app.py)", 0.5)],
            ),
        ),
        ("PRStep", _PR_OUTCOME, _simulate_pr_activity),
    ]

    for name, outcome, simulate_activity in steps:
        started = time.monotonic()
        yield StepEvent(
            step_name=name, status="running", outcome=None, started_at=started, duration=None
        )

        if simulate_activity is not None:
            await simulate_activity(activity_relay)
        else:
            await asyncio.sleep(_STEP_DELAY)

        if fail and name == "ReviewStep":
            raise RuntimeError("preview: simulated ReviewStep failure (--fail)")

        yield StepEvent(
            step_name=name,
            status="completed",
            outcome=outcome,
            started_at=started,
            duration=time.monotonic() - started,
        )

        if outcome.needs_approval:
            response = await approval_relay.request_approval(name, outcome)
            if response.decision == "abort":
                raise RunAbortedError(name)
            if response.decision == "fix":
                fix_started = time.monotonic()
                yield StepEvent(
                    step_name=name,
                    status="running",
                    outcome=None,
                    started_at=fix_started,
                    duration=None,
                )
                await asyncio.sleep(1.0)
                yield StepEvent(
                    step_name=name,
                    status="completed",
                    outcome=_REVIEW_FIX_ROUND_OUTCOME,
                    started_at=fix_started,
                    duration=time.monotonic() - fix_started,
                )
            # "approve"/"skip" leave no further bookkeeping here -- `ReviewApp`'s own
            # approval-relay worker already records "skip" into `self._skipped_steps`.


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fail",
        action="store_true",
        help="Simulate ReviewStep raising, to preview the failure path.",
    )
    args = parser.parse_args()

    activity_relay = ActivityRelay()
    approval_relay = ApprovalRelay()
    app = ReviewApp(
        STEP_REGISTRY,
        _fake_events(args.fail, activity_relay, approval_relay),
        activity_relay=activity_relay,
        approval_relay=approval_relay,
        branch="fix/nil-check",
        display_names=STEP_DISPLAY_NAMES,
    )
    app.run()


if __name__ == "__main__":
    main()
