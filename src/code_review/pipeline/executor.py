"""Executor: runs a fixed-order list of Steps against a StepContext (Milestone 2, extended
for a `StepEvent` stream by Milestone 13's #39).

`run_steps` is the executor's sole entry point: it calls `step.run(ctx)` for each `Step`
in `steps`, strictly in list order, yielding a "running" `StepEvent` immediately before
that call and a "completed" `StepEvent` (carrying that step's `StepOutcome` and timing)
immediately after -- for every step, in that same order. This is an async generator, not
a coroutine returning a list: events reach the caller as each step starts and finishes,
so a live progress view (Milestone 13's TUI, #40) can render them as they happen rather
than waiting for the whole run. Events are not held anywhere beyond the caller's own
iteration -- no database, no disk-backed log, no resume-after-crash path. That is a
deliberate consequence of this project's foreground-blocking-CLI trigger, not a gap; see
#12's Problem Statement and `docs/GATE-MODEL.md` for why the Go tool's SQLite-backed
run-state machine exists to serve its daemon and has nothing to serve here.

The loop below is a plain `for` over `steps`: nothing here inspects a prior step's
`StepOutcome` to decide whether or how to run the next one. Step order is a property of
the list the caller passes, not of anything a step returns -- see
`tests/pipeline/test_executor.py` for a test that would fail if that stopped being true.
Auto-fix and the approval park are Milestone 7's job, layered on top of this same call
shape; head continuity is Milestone 9's.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator

from code_review.pipeline.step import Step, StepContext, StepEvent


async def run_steps(steps: list[Step], ctx: StepContext) -> AsyncIterator[StepEvent]:
    """Run every step in ``steps`` against ``ctx``, in list order, unconditionally.

    Yields a "running" ``StepEvent`` immediately before calling ``step.run(ctx)``, then a
    "completed" ``StepEvent`` (that step's ``StepOutcome`` plus timing) immediately after
    it finishes -- for every step in ``steps``, strictly in list order.
    """

    for step in steps:
        step_name = step.get_name()
        started_at = time.monotonic()
        yield StepEvent(
            step_name=step_name,
            status="running",
            outcome=None,
            started_at=started_at,
            duration=None,
        )
        outcome = await step.run(ctx)
        yield StepEvent(
            step_name=step_name,
            status="completed",
            outcome=outcome,
            started_at=started_at,
            duration=time.monotonic() - started_at,
        )
