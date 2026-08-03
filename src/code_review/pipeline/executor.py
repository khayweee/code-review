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

**Why this module now touches activity reporting (issue #64)**: `steps/gitutils.py`'s
`run_git` needs to reach the running step's `ActivityReporter` but has no `StepContext`
parameter (and `steps/rebase.py`'s existing call sites must not gain one -- see
`pipeline/step.py`'s module docstring, "Ambient reporting (issue #64)"). This executor is
the one place that already calls `step.run(ctx)` uniformly for every step, so it is the
natural, and only non-invasive, place to bind `pipeline.step.current_activity_reporter`
(a `contextvars.ContextVar`) from `ctx.activity_reporter` for the duration of that one
call: `.set()` immediately before `await step.run(ctx)`, `.reset()` immediately after, in
a `try`/`finally` so a step that raises still unbinds it before the exception propagates.
This is pure plumbing between two already-existing seams (`StepContext.activity_reporter`
and the ContextVar `run_git` reads) -- it does not change which steps run, what they
produce, or `StepEvent`'s own shape.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator

from code_review.pipeline.step import (
    Step,
    StepContext,
    StepEvent,
    current_activity_reporter,
)


async def run_steps(steps: list[Step], ctx: StepContext) -> AsyncIterator[StepEvent]:
    """Run every step in ``steps`` against ``ctx``, in list order, unconditionally.

    Yields a "running" ``StepEvent`` immediately before calling ``step.run(ctx)``, then a
    "completed" ``StepEvent`` (that step's ``StepOutcome`` plus timing) immediately after
    it finishes -- for every step in ``steps``, strictly in list order.

    Also binds ``ctx.activity_reporter`` as the ambient ``current_activity_reporter``
    (``pipeline/step.py``) for exactly the duration of each ``step.run(ctx)`` call -- see
    this module's docstring's "Why this module now touches activity reporting" section.
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
        token = current_activity_reporter.set(ctx.activity_reporter)
        try:
            outcome = await step.run(ctx)
        finally:
            current_activity_reporter.reset(token)
        yield StepEvent(
            step_name=step_name,
            status="completed",
            outcome=outcome,
            started_at=started_at,
            duration=time.monotonic() - started_at,
        )
