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
import time
from collections.abc import AsyncIterator, Awaitable, Callable

from code_review.pipeline.executor import RunAbortedError
from code_review.pipeline.findings import Finding
from code_review.pipeline.step import StepEvent, StepOutcome
from code_review.steps.intent import Intent
from code_review.steps.pr import PullRequestOutcome
from code_review.steps.registry import STEP_DISPLAY_NAMES, STEP_REGISTRY
from code_review.steps.review import ReviewOutput
from code_review.steps.test_sufficiency import TestArtifact, TestSufficiencyOutput
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
    activity_relay: ActivityRelay, label: str, tool_calls: list[str]
) -> None:
    """`ReviewStep`/`TestSufficiencyStep`'s real shape: one coarse `ctx.report_activity(label)`
    span for the whole agent call, containing zero or more one-shot `ctx.log(...)` events --
    `steps/tool_activity.py`'s `tool_stream_relay` reporting each streamed `TOOL_USE` this way
    for a real `ClaudeCLI` call -- reported here identically via `activity_relay.log(...)` so
    the activity pane renders exactly as it would live tool-call streaming.
    """

    async with activity_relay.activity(label):
        await asyncio.sleep(0.3)
        for tool_call in tool_calls:
            await activity_relay.log(tool_call)
            await asyncio.sleep(0.4)


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
    """Steps in `STEP_REGISTRY` order, all five with a class today (see `registry.py`), so
    none render as a pending placeholder.

    Each step's `simulate_activity` callable stands in for the nested sub-step activity
    issues #64/#65 report for real (`RebaseStep`'s individual `git fetch`/`git rebase` spans
    via `_simulate_git_activity`; `ReviewStep`/`TestSufficiencyStep`'s one coarse agent-call
    span plus nested one-shot tool-call events via `_simulate_agent_call`; `PRStep`'s `git
    diff`/`gh pr view`/`gh pr create` spans via `_simulate_pr_activity`) -- reported here
    the same way, through `activity_relay.activity(label)`/`activity_relay.log(label)`, so
    `ReviewApp`'s activity worker (`app.py`'s `_consume_activities`) and `state.py`'s
    `backfill_activities` render them exactly as they would a real run's. `IntentStep`
    reports none, matching reality (no subprocess). `PRStep`'s outcome also carries a real
    `PullRequestOutcome` payload (`_PR_OUTCOME`), so the Pipeline box's "Pull Request" row
    renders its "→ opened <url>" detail text too (`tui/state.py`'s
    `_detail_for_completed_payload`).

    ReviewStep's outcome carries `needs_approval=True`, so once its "completed" event is
    yielded this awaits `approval_relay.request_approval(...)` -- exactly the call
    `pipeline.executor.run_steps` makes at a real park -- and blocks until `ReviewApp`'s
    approval-relay worker resolves it from a human answering the parked `FindingsBox`'s
    inline decision selector (issue #87). This mirrors that executor's own approve/skip/
    fix/abort handling: "fix" re-runs the step for
    one simulated round (settling on `_NO_FINDINGS`, standing in for a fix that resolved the
    findings) before moving on; "abort" raises `RunAbortedError`, matching the real failure
    path this preview's `--fail` flag also exercises; "approve"/"skip" both simply continue
    to the next step, the same "presentational only" distinction the real executor draws.
    """

    steps: list[tuple[str, StepOutcome, Callable[[ActivityRelay], Awaitable[None]] | None]] = [
        ("IntentStep", _INTENT_OUTCOME, None),
        ("RebaseStep", _NO_FINDINGS, _simulate_git_activity),
        (
            "ReviewStep",
            _REVIEW_FINDINGS,
            lambda relay: _simulate_agent_call(
                relay,
                "Agent: reviewing diff via claude",
                [
                    "Tool: Read(src/code_review/cli.py)",
                    "Tool: Bash(git diff --stat)",
                ],
            ),
        ),
        (
            "TestSufficiencyStep",
            _TEST_SUFFICIENCY_OUTCOME,
            lambda relay: _simulate_agent_call(
                relay,
                "Agent: assessing test sufficiency via claude",
                ["Tool: Read(tests/tui/test_app.py)"],
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
                    outcome=_NO_FINDINGS,
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
