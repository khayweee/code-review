"""Executor: runs a fixed-order list of Steps against a StepContext (Milestone 2).

`run_steps` is the executor's sole entry point: it calls `step.run(ctx)` for each `Step`
in `steps`, strictly in list order, and returns every `StepOutcome` produced, in that same
order. Outcomes are held in memory only for this process's lifetime -- no database, no
disk-backed log, no resume-after-crash path. That is a deliberate consequence of this
project's foreground-blocking-CLI trigger, not a gap; see #12's Problem Statement and
`docs/GATE-MODEL.md` for why the Go tool's SQLite-backed run-state machine exists to serve
its daemon and has nothing to serve here.

The loop below is a plain `for` over `steps`: nothing here inspects a prior step's
`StepOutcome` to decide whether or how to run the next one. Step order is a property of
the list the caller passes, not of anything a step returns -- see
`tests/pipeline/test_executor.py` for a test that would fail if that stopped being true.
Auto-fix and the approval park are Milestone 7's job, layered on top of this same call
shape; head continuity is Milestone 9's.
"""

from __future__ import annotations

from code_review.pipeline.step import Step, StepContext, StepOutcome


async def run_steps(steps: list[Step], ctx: StepContext) -> list[StepOutcome]:
    """Run every step in ``steps`` against ``ctx``, in list order, unconditionally.

    Returns each step's ``StepOutcome`` in the order the steps actually ran.
    """

    return [await step.run(ctx) for step in steps]
