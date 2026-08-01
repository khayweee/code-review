"""Executor: runs one real Step against a StepContext (Milestone 2, slice 1 of 2).

`run_step` is the public entry point this slice proves: it calls `step.run(ctx)` and
hands back the `StepOutcome` produced, held in memory only for this process's lifetime --
no database, no disk-backed log, no resume-after-crash path. That is a deliberate
consequence of this project's foreground-blocking-CLI trigger, not a gap; see #12's
Problem Statement and `docs/GATE-MODEL.md` for why the Go tool's SQLite-backed run-state
machine exists to serve its daemon and has nothing to serve here.

Slice 2 (issue #14) generalizes this to a fixed, hard-coded `list[Step]` order. Nothing
here commits to that signature yet, and nothing here branches on `needs_approval` or
`auto_fixable` -- Milestone 6 adds the fix/park state machine on top of this same call
shape, Milestone 8 the head-continuity guard.
"""

from __future__ import annotations

from code_review.pipeline.step import Step, StepContext, StepOutcome


async def run_step(step: Step, ctx: StepContext) -> StepOutcome:
    """Run ``step`` against ``ctx`` and return the ``StepOutcome`` it produced."""

    return await step.run(ctx)
