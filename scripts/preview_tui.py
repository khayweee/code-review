"""Dev-only TUI preview: launches `ReviewApp` against a hand-written fake event stream, with
no git repo, branch, `--intent`, or `claude` subprocess involved anywhere.

Exists so a styling/layout change to `tui/widgets.py`/`app.py`/`*.tcss` can be eyeballed in
one command instead of a real `code-review review BRANCH --intent ...` run, which needs a
real branch and shells out to the `claude` CLI for every step. This mirrors exactly how
`tests/tui/test_app.py` drives `ReviewApp` -- a hand-built `AsyncIterator[StepEvent]`, per
`app.py`'s own injection-seam docstring -- just left running on a timer instead of driven
through `Pilot` and asserted against.

ReviewStep's outcome has `needs_approval=True`, and a real `ApprovalRelay` (issue #80/#81)
is wired into `ReviewApp` exactly the way `cli.py` wires one for a real run -- so this
preview also drives `ApprovalPromptScreen`: `_fake_events` awaits
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
from collections.abc import AsyncIterator

from code_review.pipeline.executor import RunAbortedError
from code_review.pipeline.findings import Finding
from code_review.pipeline.step import StepEvent, StepOutcome
from code_review.steps.registry import STEP_REGISTRY
from code_review.steps.review import ReviewOutput
from code_review.tui.activity import ActivityRelay
from code_review.tui.app import ReviewApp
from code_review.tui.approval_relay import ApprovalRelay

# Long enough that each step's "running" state is visible on screen, short enough that the
# whole preview run finishes in a few seconds.
_STEP_DELAY = 3.6

_NO_FINDINGS = StepOutcome(needs_approval=False, auto_fixable=False, findings=None)

_REVIEW_FINDINGS = StepOutcome(
    needs_approval=True,
    auto_fixable=False,
    findings=ReviewOutput(
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


async def _fake_events(
    fail: bool, activity_relay: ActivityRelay, approval_relay: ApprovalRelay
) -> AsyncIterator[StepEvent]:
    """Steps in `STEP_REGISTRY` order. `PRStep` has no class yet (see `registry.py`), so it
    is left out entirely and renders as a pending placeholder, matching a real run today.

    Each step's `activities` list stands in for the nested sub-step activity issues #64/#65
    report for real (`RebaseStep`'s individual `git fetch`/`git rebase` calls, `ReviewStep`'s
    one coarse agent-call span) -- reported here the same way, through
    `activity_relay.activity(label)`, so `ReviewApp`'s activity worker (`app.py`'s
    `_consume_activities`) and `state.py`'s `backfill_activities` render them exactly as
    they would a real run's. `IntentStep` reports none, matching reality (no subprocess).

    ReviewStep's outcome carries `needs_approval=True`, so once its "completed" event is
    yielded this awaits `approval_relay.request_approval(...)` -- exactly the call
    `pipeline.executor.run_steps` makes at a real park -- and blocks until `ReviewApp`'s
    approval-relay worker resolves it from a human answering `ApprovalPromptScreen`. This
    mirrors that executor's own approve/skip/fix/abort handling: "fix" re-runs the step for
    one simulated round (settling on `_NO_FINDINGS`, standing in for a fix that resolved the
    findings) before moving on; "abort" raises `RunAbortedError`, matching the real failure
    path this preview's `--fail` flag also exercises; "approve"/"skip" both simply continue
    to the next step, the same "presentational only" distinction the real executor draws.
    """

    steps: list[tuple[str, StepOutcome, list[tuple[str, float]]]] = [
        ("IntentStep", _NO_FINDINGS, []),
        (
            "RebaseStep",
            _NO_FINDINGS,
            [("git fetch origin", 0.5), ("git rebase origin/main", 0.5)],
        ),
        ("ReviewStep", _REVIEW_FINDINGS, [("claude review call", 1.0)]),
        ("TestSufficiencyStep", _NO_FINDINGS, [("claude test-sufficiency call", 0.7)]),
    ]

    for name, outcome, activities in steps:
        started = time.monotonic()
        yield StepEvent(
            step_name=name, status="running", outcome=None, started_at=started, duration=None
        )

        if activities:
            for label, delay in activities:
                async with activity_relay.activity(label):
                    await asyncio.sleep(delay)
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
    )
    app.run()


if __name__ == "__main__":
    main()
