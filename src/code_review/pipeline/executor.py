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
`StepOutcome` to decide whether or how to run the *next* step in `steps` -- step order is
still a property of the list the caller passes, not of anything a step returns (see
`tests/pipeline/test_executor.py` for a test that would fail if that stopped being true).
Auto-fix (issue #81, still unimplemented) is layered on top of this same call shape; head
continuity is Milestone 9's.

**The approval park (issue #80, Milestone 7's first ticket)**: after a step's "completed"
`StepEvent` has already been yielded (see the note above and below -- that shape is
unchanged by this), this loop checks that same `outcome.needs_approval`. False means
nothing more happens, exactly as before #80. True means the step *parked*: this function
awaits `ctx.on_approval_needed(step_name, outcome)` (see `pipeline/step.py`'s
`StepContext.on_approval_needed` field comment) and blocks until it resolves to one of
"approve"/"skip"/"abort". "approve" and "skip" are identical from this loop's own
perspective -- both simply let the `for` continue to the next step; the two only differ in
how the *caller* (`tui/app.py`'s `ReviewApp`, via its own approval-relay worker) chooses to
render that step afterward, which this module has no opinion on and never will (see
`tui/AGENTS.md`'s "'Failed' is derived, never reported" section for the identical
"presentation state lives on the caller side, not here" rule already established for
`failed_step`). "abort" raises `RunAbortedError` right here, which propagates out of this
generator on its next `__anext__()` -- no further step runs, and no further `StepEvent` is
yielded. `ctx.on_approval_needed is None` (no relay attached, e.g. any test constructing
`StepContext` without exercising this field) raises `ApprovalNotAttachedError` instead of
hanging or silently treating the park as approved, the same "fail closed" rule
`agent/claude_cli.py`'s own `_run_with_stdin_relay` already applies to `on_input_needed`.

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


class ApprovalNotAttachedError(RuntimeError):
    """A step parked (``StepOutcome.needs_approval`` is True) but ``ctx.on_approval_needed``
    is ``None`` -- no relay is attached to ask a human. Raised instead of hanging forever
    or silently treating the park as approved, mirroring ``agent/errors.py``'s
    ``StdinBlockedError`` (``RunOpts.on_input_needed``'s own "``None`` means fail closed"
    rule, applied here to the analogous approval seam).
    """

    def __init__(self, step_name: str) -> None:
        super().__init__(
            f"{step_name!r} needs approval (StepOutcome.needs_approval=True) but no "
            "on_approval_needed relay is attached to StepContext -- failing closed rather "
            "than hanging or silently approving. Wire StepContext.on_approval_needed (see "
            "tui.approval_relay.ApprovalRelay) to run this pipeline interactively."
        )
        self.step_name = step_name


class RunAbortedError(RuntimeError):
    """Raised when a human answers a parked step's approval request with "abort".

    Unwinds the whole run: raised directly out of ``run_steps``, so no further step runs
    and no further ``StepEvent`` is yielded. ``cli.py`` reports this the same way it
    already reports any other exception a step raises -- via ``ReviewApp.error`` once the
    TUI exits -- rather than needing a dedicated catch of this specific type; see
    ``cli.py``'s ``review`` command.
    """

    def __init__(self, step_name: str) -> None:
        super().__init__(
            f"run aborted: {step_name!r} needed approval and the user chose to abort. "
            "No further steps ran."
        )
        self.step_name = step_name


async def run_steps(steps: list[Step], ctx: StepContext) -> AsyncIterator[StepEvent]:
    """Run every step in ``steps`` against ``ctx``, in list order.

    Yields a "running" ``StepEvent`` immediately before calling ``step.run(ctx)``, then a
    "completed" ``StepEvent`` (that step's ``StepOutcome`` plus timing) immediately after
    it finishes -- for every step in ``steps``, strictly in list order. This shape is
    unchanged by the approval park below: a step's "completed" event is always yielded
    first, exactly as it was before issue #80, regardless of what ``outcome.needs_approval``
    says.

    Also binds ``ctx.activity_reporter`` as the ambient ``current_activity_reporter``
    (``pipeline/step.py``) for exactly the duration of each ``step.run(ctx)`` call -- see
    this module's docstring's "Why this module now touches activity reporting" section.

    Once ``outcome.needs_approval`` is True, this awaits ``ctx.on_approval_needed(step_name,
    outcome)`` and blocks until a decision comes back -- see this module's docstring's "The
    approval park" section for the full approve/skip/abort behavior and the fail-closed
    rule when no relay is attached.
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

        if not outcome.needs_approval:
            continue
        if ctx.on_approval_needed is None:
            raise ApprovalNotAttachedError(step_name)
        decision = await ctx.on_approval_needed(step_name, outcome)
        if decision == "abort":
            raise RunAbortedError(step_name)
        # "approve"/"skip" both simply let the loop continue -- see this module's
        # docstring's "The approval park" section for why that distinction is presentational
        # only, drawn by the caller (`tui/app.py`), not by this loop.
