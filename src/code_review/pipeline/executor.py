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
Within one step, though, an inner `while True` loop (see "The fix-round loop" below) can
re-run that same step multiple times, with an evolving `StepContext`, before the outer
`for` moves on. Head continuity is Milestone 9's.

**The approval park (issue #80, Milestone 7's first ticket)**: after a step's "completed"
`StepEvent` has already been yielded (see the note above and below -- that shape is
unchanged by this), this loop checks that same `outcome.needs_approval` (extended by issue
#81 to also cover a still-`auto_fixable` outcome whose automatic round cap is exhausted --
see below). No park means nothing more happens, exactly as before #80/#81. A park means
this function awaits `ctx.on_approval_needed(step_name, outcome)` (see `pipeline/step.py`'s
`StepContext.on_approval_needed` field comment) and blocks until it resolves to an
`ApprovalResponse`. "approve" and "skip" are identical from this loop's own perspective --
both simply let the `for` continue to the next step; the two only differ in how the
*caller* (`tui/app.py`'s `ReviewApp`, via its own approval-relay worker) chooses to render
that step afterward, which this module has no opinion on and never will (see
`tui/AGENTS.md`'s "'Failed' is derived, never reported" section for the identical
"presentation state lives on the caller side, not here" rule already established for
`failed_step`). "abort" raises `RunAbortedError` right here, which propagates out of this
generator on its next `__anext__()` -- no further step runs, and no further `StepEvent` is
yielded. "fix" (issue #81) re-runs the same step with a `FixRound` built from the human's
own typed instructions -- see below; this branch is never capped, unlike the automatic
path. `ctx.on_approval_needed is None` (no relay attached, e.g. any test constructing
`StepContext` without exercising this field) raises `ApprovalNotAttachedError` instead of
hanging or silently treating the park as approved, the same "fail closed" rule
`agent/claude_cli.py`'s own `_run_with_stdin_relay` already applies to `on_input_needed`.

**The fix-round loop (issue #81, Milestone 7's second ticket)**: gated entirely on
`step.supports_fix_round` (`pipeline/step.py`) -- a step that leaves this `False` (every
step except `steps/review.py`'s `ReviewStep` today) sees exactly the pre-#81 behavior: only
`outcome.needs_approval` parks, `outcome.auto_fixable` is never consulted, no automatic
re-run ever happens. This is the one design point this ticket most needed to get right
(see `pipeline/AGENTS.md`'s own note): gating the loop off `outcome.auto_fixable` alone
would have bounced `steps/test_sufficiency.py`'s `TestSufficiencyStep` through the same
capped re-runs too, even though it doesn't yet know how to consume `StepContext.fix_round`
(that's issue #82) -- each "round" would blindly resubmit the identical prompt and get the
identical outcome back, a real behavior change `TestSufficiencyStep` must not see from this
ticket.

For a step that opts in, after each round's `outcome`: if `outcome.auto_fixable` is True
and this step's automatic round count hasn't reached `_MAX_AUTO_FIX_ROUNDS` (a plain
module-level constant, not a config field or CLI flag -- see that constant's own comment),
this function builds `FixRound(instructions=describe_auto_fix_findings(outcome.findings))`
(`pipeline/findings.py`), `dataclasses.replace`s the round's own `StepContext` with it (the
original `ctx` passed into `run_steps` is never mutated), and re-runs the same step --
yielding a fresh "running"/"completed" `StepEvent` pair, exactly as any other round would,
with no park and no approval call. Once that cap is reached, a still-`auto_fixable` outcome
falls through to the park logic above instead of looping forever -- this is the other half
of "`needs_approval`/`auto_fixable` are otherwise mutually exclusive by construction" (see
`steps/review.py`'s own comment on that invariant): a cap-exhausted `auto_fixable=True`
outcome is the one case this loop parks on something other than `needs_approval` alone. A
human's "fix" response at that park re-runs the step the same way, but is never subject to
`_MAX_AUTO_FIX_ROUNDS` -- a human can choose "fix" as many times in a row as they want; only
the fully-automatic path is bounded.

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

import dataclasses
import time
from collections.abc import AsyncIterator

from code_review.pipeline.findings import describe_auto_fix_findings
from code_review.pipeline.step import (
    FixRound,
    Step,
    StepContext,
    StepEvent,
    current_activity_reporter,
)

# Small, fixed cap on the number of *automatic* fix-round re-runs a single step gets before
# a still-`auto_fixable` outcome falls through to a park instead (issue #81's second
# acceptance criterion: "falls through... rather than looping forever"). A plain module-
# level constant, not a config field or CLI flag -- Milestone 10 (trust-boundary config
# partitioning) is what would make this configurable, and is explicitly out of scope here.
# 2 was picked as a small number that still lets a step recover from more than one
# consecutive round without unbounded looping burning agent calls silently in the
# background; a human's own uncapped "fix" response at the resulting park is the escape
# hatch for anything that genuinely needs more.
_MAX_AUTO_FIX_ROUNDS = 2


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

    Yields a "running" ``StepEvent`` immediately before calling ``step.run(round_ctx)``,
    then a "completed" ``StepEvent`` (that step's ``StepOutcome`` plus timing) immediately
    after it finishes -- once per *round* (see below), for every step in ``steps``,
    strictly in list order. This shape is unchanged by the approval park or the fix-round
    loop below: a step's "completed" event is always yielded first, exactly as it was
    before issue #80, regardless of what ``outcome.needs_approval``/``outcome.auto_fixable``
    say.

    A step can run more than once per its slot in ``steps``: the inner ``while True`` loop
    re-runs the same step, against an evolving ``round_ctx`` built via
    ``dataclasses.replace`` (the caller's own ``ctx`` is never mutated), for as long as an
    auto-fix or human-fix round keeps firing -- see this module's docstring's "The fix-round
    loop" section. Each round gets its own fresh "running"/"completed" ``StepEvent`` pair,
    which is what makes a fix round's fresh findings visible to a live TUI for free.

    Also binds ``round_ctx.activity_reporter`` as the ambient ``current_activity_reporter``
    (``pipeline/step.py``) for exactly the duration of each ``step.run(round_ctx)`` call --
    see this module's docstring's "Why this module now touches activity reporting" section.

    Once a round's outcome needs a park (``outcome.needs_approval``, or -- for a step that
    opts into fix rounds -- a still-``auto_fixable`` outcome whose automatic round cap is
    exhausted), this awaits ``round_ctx.on_approval_needed(step_name, outcome)`` and blocks
    until an ``ApprovalResponse`` comes back -- see this module's docstring's "The approval
    park" section for the full approve/skip/fix/abort behavior and the fail-closed rule
    when no relay is attached.
    """

    for step in steps:
        step_name = step.get_name()
        round_ctx = ctx
        # How many *automatic* fix rounds this step has used so far -- reset to 0 for every
        # new step in the outer `for`, and never incremented by a human's own "fix" response
        # at a park (that path is uncapped, see the module docstring).
        auto_fix_rounds = 0

        while True:
            started_at = time.monotonic()
            yield StepEvent(
                step_name=step_name,
                status="running",
                outcome=None,
                started_at=started_at,
                duration=None,
            )
            token = current_activity_reporter.set(round_ctx.activity_reporter)
            try:
                outcome = await step.run(round_ctx)
            finally:
                current_activity_reporter.reset(token)
            yield StepEvent(
                step_name=step_name,
                status="completed",
                outcome=outcome,
                started_at=started_at,
                duration=time.monotonic() - started_at,
            )

            # The automatic fix-round path (issue #81): only ever considered for a step
            # that has explicitly opted in -- see `Step.supports_fix_round`'s own comment
            # for why this gate must not be `outcome.auto_fixable` alone.
            auto_fix_cap_exhausted = auto_fix_rounds >= _MAX_AUTO_FIX_ROUNDS
            if step.supports_fix_round and outcome.auto_fixable and not auto_fix_cap_exhausted:
                auto_fix_rounds += 1
                round_ctx = dataclasses.replace(
                    round_ctx,
                    fix_round=FixRound(instructions=describe_auto_fix_findings(outcome.findings)),
                )
                continue

            # Whether this round's outcome needs a park -- `needs_approval` (issue #80),
            # or, for a fix-round-eligible step, a still-`auto_fixable` outcome whose
            # automatic round cap is exhausted (issue #81: "falls through to Ticket 1's
            # park path"). `needs_approval`/`auto_fixable` are otherwise mutually exclusive
            # by construction (see `steps/review.py`'s own comment on that invariant), so
            # this `or` only ever has one side True at a time in practice.
            needs_park = outcome.needs_approval or (
                step.supports_fix_round and outcome.auto_fixable and auto_fix_cap_exhausted
            )
            if not needs_park:
                break

            if round_ctx.on_approval_needed is None:
                raise ApprovalNotAttachedError(step_name)
            response = await round_ctx.on_approval_needed(step_name, outcome)
            if response.decision == "abort":
                raise RunAbortedError(step_name)
            if response.decision == "fix":
                # The human's own uncapped fix round: never checked against or counted
                # toward `_MAX_AUTO_FIX_ROUNDS` -- see the module docstring.
                round_ctx = dataclasses.replace(
                    round_ctx,
                    fix_round=FixRound(instructions=response.instructions or ""),
                )
                continue
            # "approve"/"skip" both simply let the loop move on to the next step in the
            # outer `for` -- see this module's docstring's "The approval park" section for
            # why that distinction is presentational only, drawn by the caller
            # (`tui/app.py`), not by this loop.
            break
