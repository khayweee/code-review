"""Executor: runs a fixed-order list of Steps against a StepContext, yielding a StepEvent
stream.

`run_steps` is the sole entry point: an async generator that calls `step.run(ctx)` for each
`Step` in `steps`, in list order, yielding a "running" `StepEvent` before the call and a
"completed" one (with `StepOutcome` and timing) after -- so a live progress view can render
events as they happen rather than waiting for the whole run. Events aren't held anywhere
beyond the caller's own iteration -- no database, no resume-after-crash path (deliberate,
see `docs/GATE-MODEL.md`).

The outer loop is a plain `for` over `steps`: step order is a property of the caller's
list, never of a prior step's `StepOutcome`. An inner `while True` loop can re-run the same
step multiple times (fix rounds) with an evolving `StepContext` before the outer `for`
moves on.

**The approval park**: after a step's "completed" event is yielded, this loop checks
`outcome.needs_approval` (or a still-`auto_fixable` outcome whose automatic round cap is
exhausted). No park: nothing more happens. A park: awaits `ctx.on_approval_needed(step_name,
outcome)` and blocks for an `ApprovalResponse`. "approve"/"skip" both just continue to the
next step -- the caller (`tui/app.py`) decides how to render each afterward, not this
module. "abort" raises `RunAbortedError`, unwinding the generator with no further steps or
events. "fix" re-runs the step with a `FixRound` from the human's own instructions,
uncapped. `ctx.on_approval_needed is None` raises `ApprovalNotAttachedError` (fail closed,
not hang or silently approve).

**The fix-round loop**: gated entirely on `step.supports_fix_round` -- a step leaving this
`False` sees pre-fix-round behavior unchanged: only `needs_approval` parks, `auto_fixable`
is never consulted. This gate must not be `outcome.auto_fixable` alone, since a step that
computes a genuine `auto_fixable=True` but doesn't yet build a fix-mode prompt would
otherwise get bounced through capped re-runs that blindly resubmit the same prompt.

For an opted-in step, after each round: if `outcome.auto_fixable` and the automatic round
count is below `_MAX_AUTO_FIX_ROUNDS`, this calls `round_ctx.with_fix_round(...)` with
`describe_auto_fix_findings(outcome.payload)` to get a new `StepContext` carrying that
`FixRound` (the caller's `ctx` is never mutated), and re-runs the step with a fresh
"running"/"completed" pair, no park. Once the cap is reached, a still-`auto_fixable`
outcome falls through to the park instead of looping forever -- `needs_approval` and
cap-exhausted-`auto_fixable` are the only two park conditions, mutually exclusive by
construction. A human's "fix" response at that park is never counted against the cap.

**Activity reporting**: `steps/gitutils.py`'s `run_git` has no `StepContext` parameter, so
this executor -- the one place that calls `step.run(ctx)` uniformly -- binds
`pipeline.step.current_activity_reporter` from `ctx.activity_reporter` around each call
(`.set()`/`.reset()` in a `try`/`finally`, so a raising step still unbinds it).

**`step_outcomes` threading**: once a step's slot fully settles -- the inner `while True`
loop has broken, whether because no park was needed or because a park resolved to
"approve"/"skip" -- this folds `{step_name: outcome}` into the *outer* `ctx.step_outcomes`
via `dataclasses.replace` and reassigns `ctx` for the next outer-loop iteration to see,
exactly once per step slot (never once per fix round, and never onto `round_ctx`, which is
discarded and rebuilt from the updated `ctx` at the top of the next step's slot anyway).
This is a read-only reporting channel a later step may use to summarize an earlier step's
already-computed output (e.g. `PRStep` rendering `ReviewStep`'s risk verdict) -- it changes
no step's execution or the pipeline's step order, so it does not undermine this module's
"nothing here inspects a prior step's `StepOutcome` to decide whether or how to run the
next one" invariant; see `pipeline/AGENTS.md` for the full rationale.

**`cwd_override`**: at that exact same fold point, a settled outcome's `cwd_override` (only
`WorktreeStep` ever sets it) also replaces `ctx.cwd` for every step after it -- every step
still runs, unconditionally, in the same fixed order; only the resource path they operate
against changes. See `pipeline/AGENTS.md`'s WorktreeStep section for why this is an honestly
different, narrower kind of cross-step effect than `step_outcomes` (mandatory shared
infrastructure state every later step's git calls depend on, not optional data a step may
choose to read for its own output).
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from dataclasses import replace

from code_review.pipeline.findings import describe_auto_fix_findings
from code_review.pipeline.schemas import StepEvent
from code_review.pipeline.step import Step, StepContext, current_activity_reporter

# Cap on automatic fix-round re-runs before a still-auto_fixable outcome falls through to a
# park instead of looping forever. Not a config field or CLI flag. 2 lets a step recover
# from more than one round without burning agent calls silently; a human's own uncapped
# "fix" response is the escape hatch for anything that needs more.
_MAX_AUTO_FIX_ROUNDS = 2


class ApprovalNotAttachedError(RuntimeError):
    """A step parked but ``ctx.on_approval_needed`` is ``None`` -- fails closed rather than
    hanging or silently approving.
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

    Propagates out of ``run_steps`` directly, so no further step runs or event is yielded.
    """

    def __init__(self, step_name: str) -> None:
        super().__init__(
            f"run aborted: {step_name!r} needed approval and the user chose to abort. "
            "No further steps ran."
        )
        self.step_name = step_name


async def run_steps(steps: list[Step], ctx: StepContext) -> AsyncIterator[StepEvent]:
    """Run every step in ``steps`` against ``ctx``, in list order.

    Yields a "running" event before each ``step.run(round_ctx)`` call and a "completed"
    event (``StepOutcome`` plus timing) after, once per round, for every step in order. A
    step can run more than once per slot: the inner ``while True`` loop re-runs it against
    an evolving ``round_ctx`` (via ``round_ctx.with_fix_round(...)``; the caller's ``ctx``
    is never mutated) for as long as an auto-fix or human-fix round keeps firing -- see the module
    docstring's "fix-round loop" section. Each round gets its own event pair.

    Binds ``round_ctx.activity_reporter`` as the ambient ``current_activity_reporter`` for
    the duration of each ``step.run(round_ctx)`` call.

    Once a round needs a park, awaits ``round_ctx.on_approval_needed(step_name, outcome)``
    and blocks for an ``ApprovalResponse`` -- see the module docstring's "approval park"
    section.
    """

    for step in steps:
        step_name = step.get_name()
        round_ctx = ctx
        # Automatic fix rounds used so far; reset per step, never incremented by a human's
        # uncapped "fix" response at a park.
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

            # Automatic fix-round path: only for a step that opts in via supports_fix_round.
            auto_fix_cap_exhausted = auto_fix_rounds >= _MAX_AUTO_FIX_ROUNDS
            if step.supports_fix_round and outcome.auto_fixable and not auto_fix_cap_exhausted:
                auto_fix_rounds += 1
                round_ctx = round_ctx.with_fix_round(describe_auto_fix_findings(outcome.payload))
                continue

            # needs_approval, or (for a fix-round-eligible step) a still-auto_fixable
            # outcome whose automatic cap is exhausted. The two are mutually exclusive by
            # construction, so this `or` only ever has one side True in practice.
            needs_park = outcome.needs_approval or (
                step.supports_fix_round and outcome.auto_fixable and auto_fix_cap_exhausted
            )
            if not needs_park:
                break

            if round_ctx.on_approval_needed is None:
                raise ApprovalNotAttachedError(step_name)

            # Blocks until the human answers the park. "approve"/"skip" both just continue to the
            response = await round_ctx.on_approval_needed(step_name, outcome)
            if response.decision == "abort":
                raise RunAbortedError(step_name)
            if response.decision == "fix":
                # Human's own fix round: never counted toward _MAX_AUTO_FIX_ROUNDS.
                round_ctx = round_ctx.with_fix_round(response.instructions or "")
                continue
            # "approve"/"skip" both just move on; the caller (tui/app.py) decides how to
            # render the distinction.
            break

        # This step's slot has fully settled (fix rounds, if any, are done) -- fold its
        # final outcome into the outer ctx's step_outcomes exactly once per step, never once
        # per round, so a later step can read it. round_ctx (about to be rebuilt for the
        # next step) is never the target here; only the outer ctx carries this forward.
        new_step_outcomes = {**ctx.step_outcomes, step_name: outcome}
        # Same fold point, for the one other field a settled outcome can carry forward:
        # WorktreeStep's cwd_override. See pipeline/AGENTS.md's WorktreeStep section for why
        # this is a different, mandatory-infrastructure kind of cross-step effect than
        # step_outcomes, not a second precedent for one.
        if outcome.cwd_override is not None:
            ctx = replace(ctx, step_outcomes=new_step_outcomes, cwd=outcome.cwd_override)
        else:
            ctx = replace(ctx, step_outcomes=new_step_outcomes)
