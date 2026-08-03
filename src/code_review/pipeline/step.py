"""Step abstract base class, StepContext, StepOutcome (Milestone 2, see docs/ROADMAP.md).

`Step` is deliberately a one-method abstract base class -- no lifecycle hooks, no
composition helpers -- since there is only one real `Step` consuming this shape so far
(the round-trip test in `tests/pipeline/`); the first production step (`IntentStep`) lands
in Milestone 3, see `src/code_review/steps/intent.py`.

`StepContext` is a bag of per-run dependencies a step needs: the working directory, the
`Agent` instance it calls through, the diff being reviewed, and the `Intent` supplied on
the command line. `Intent` is fully known before the pipeline starts -- it's a CLI flag,
not something discovered mid-run -- so `cli.py` constructs it once and every step gets it
off the same immutable `ctx`, rather than the first step handing it forward through its
`StepOutcome`. Milestone 7 extends this type with the fix-loop and approval-park state it
needs (`on_approval_needed`, `fix_round`) -- see those fields' own comments below and
`executor.py`'s module docstring for how the loop that drives them is built.

`StepContext.on_input_needed` (issue #41) carries the same interactive-input relay
`RunOpts.on_input_needed` (see `agent/base.py`) is shaped for, so a future step can pass
`ctx.on_input_needed` through to its own `RunOpts` without needing a live reference to the
TUI itself. No step consumes it yet -- none sets a non-default `permission_mode`, the only
thing that makes a backend subprocess reach for it. `cli.py` wires it to a real
`tui.input_relay.InputRelay.request_input`; tests that don't exercise it can leave it at
its default `None`.

`ActivityReporter` (issue #66) is the same kind of narrow seam, mirroring the
`on_input_needed` rule: `pipeline/`/`steps/` depend only on this structural `Protocol`,
never on `tui/` directly, "without needing a live reference to the TUI itself". It is
satisfied by `tui.activity.ActivityRelay` (Textual-import-free, see that module's
docstring) purely structurally -- nothing here imports `tui/`. `StepContext.activity_reporter`
carries an optional instance of it, and `StepContext.report_activity(label)` is the
single-line call site a step uses regardless of whether one is attached: `async with
ctx.report_activity("fetch"): ...`. It reports a second, independent event stream from
`StepEvent`'s own "running"/"completed" pair -- `StepEvent`/`run_steps` are unchanged by
this. Issue #65 (`ReviewStep`'s agent call) consumes it explicitly, exactly this way.
`cli.py` wires it to a real `tui.activity.ActivityRelay` instance.

**Ambient reporting (issue #64)**: `steps/gitutils.py`'s `run_git` also needs to report
through an `ActivityReporter`, but it takes only `args`/`cwd` -- no `StepContext`, and
`steps/rebase.py`'s existing call sites (`run_git(["fetch", ...], ctx.cwd)`) must not
change to thread one through. `current_activity_reporter` below is a module-level
`contextvars.ContextVar` that carries whichever reporter is in scope for the *currently
running step*, read directly via `.get()` by any caller that has no `StepContext` of its
own -- `run_git` is the first, and as of #64 the only, such caller. `executor.run_steps` is
the sole writer: it `.set()`s this from `ctx.activity_reporter` immediately before each
`step.run(ctx)` call and `.reset()`s it immediately after, so the ambient value is scoped
exactly to that one step's execution and never leaks into the next step or a sibling
`asyncio.Task` (contextvars already copy-on-task-creation; see `tui/activity.py`'s own
`_current_activity_id` for the analogous nesting mechanism, one layer further in). No
leading underscore, unlike that one -- this needs to be read from a different package
(`steps/`), not just within this module. `activity_or_nullcontext` factors out the single
nullcontext-when-absent branch both `StepContext.report_activity` (explicit reporter) and
`run_git` (ambient reporter) need, so that branch has exactly one implementation.

`StepOutcome` carries `needs_approval`/`auto_fixable`. Milestone 7's ticket 1 (issue #80) is
what first acts on `needs_approval`: `executor.run_steps` stops the run right after yielding
a step's "completed" `StepEvent` when that step's `StepOutcome.needs_approval` is True,
calling `StepContext.on_approval_needed` (see that field's own comment above) and blocking
until it resolves to an `ApprovalResponse`. Milestone 7's ticket 2 (issue #81) is what first
acts on `auto_fixable`: a step that opts in via `Step.supports_fix_round = True` gets a
bounded number of automatic re-runs (a fixed round cap, `executor.py`'s own module-level
constant) with an evolving `StepContext.fix_round` before the run ever parks on that
outcome, and a park's fourth response ("fix", alongside issue #80's "approve"/"skip"/
"abort") lets a human re-run the step with their own typed instructions, uncapped -- see
`executor.py`'s own module docstring for the full round-loop shape. `Step` implementations
themselves are otherwise unaffected: nothing about park/fix-round/approve/skip/abort belongs
inside a `Step.run` method beyond reading `ctx.fix_round` to build a fix-mode prompt (see
`steps/review.py`'s `ReviewStep.run`) -- all of this is layered on top of the same fixed
`step.run(ctx)` call shape, called once per round. `findings` is typed as `object` rather
than the Milestone 5 `Finding`/`Findings` schema (`pipeline/findings.py`) -- a step's own
code narrows it back to whatever schema that step validated its agent call against.

`StepEvent` (Milestone 13, issue #39) is `executor.run_steps`'s progress unit: one per
"running" and one per "completed" per step. It gets `step_name` by calling `step.get_name()`
-- a concrete method `Step` provides by default (`type(self).__name__`), overridable by a
step that needs a name distinct from its class (e.g. multiple instances of the same step
class in one run). `Step` is now a nominal `ABC`, not a structural `Protocol`, so every real
implementation (`IntentStep`, and the test fakes in `tests/pipeline/test_executor.py`)
explicitly subclasses it. `started_at`/`duration` use `time.monotonic()`, not wall-clock
time, since nothing here needs to correlate against an external clock -- only measure
elapsed time within this process.
"""

from __future__ import annotations

import contextvars
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager, nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, Literal, Protocol

from code_review.agent import Agent

if TYPE_CHECKING:
    # Import-direction note: `steps/` depends on `pipeline/`, never the reverse. A
    # top-level import of `Intent` here would be circular, since `steps/intent.py` needs
    # `StepContext`/`StepOutcome` at module level to construct real instances of them.
    # This module already has `from __future__ import annotations`, so the `intent:
    # Intent` field annotation below is a lazy string dataclass never evaluates at
    # runtime -- a type-checking-only import is sufficient and avoids the cycle.
    from code_review.steps.intent import Intent


class ActivityReporter(Protocol):
    """Structural, one-method contract for reporting nested sub-step activity (issue #66).

    Satisfied by `tui.activity.ActivityRelay` purely structurally -- nothing here imports
    `tui/` (mirrors `on_input_needed`'s own rule; see this module's docstring). A step
    never calls this directly; it goes through `StepContext.report_activity`, which
    delegates here when a reporter is attached.
    """

    def activity(self, label: str) -> AbstractAsyncContextManager[None]:
        """Report one nested unit of work named `label`, open for as long as the returned
        async context manager's body runs."""
        ...


# Ambient carrier for the `ActivityReporter` in scope for the currently running step (issue
# #64) -- see the module docstring's "Ambient reporting (issue #64)" section for the full
# rationale. `executor.run_steps` is the sole writer; `steps/gitutils.py`'s `run_git` is the
# sole reader outside this module, via `.get()` directly (it has no `StepContext` to read
# `activity_reporter` off of).
current_activity_reporter: contextvars.ContextVar[ActivityReporter | None] = contextvars.ContextVar(
    "current_activity_reporter", default=None
)


def activity_or_nullcontext(
    reporter: ActivityReporter | None, label: str
) -> AbstractAsyncContextManager[None]:
    """The one nullcontext-when-absent branch shared by every call site that reports an
    activity through a possibly-absent reporter: `StepContext.report_activity` (explicit,
    per-step reporter) and `steps/gitutils.py`'s `run_git` (ambient, via the ContextVar
    above). `reporter.activity(label)` when `reporter` is given, else
    `contextlib.nullcontext()` -- factored here once so neither call site duplicates the
    branch.
    """

    if reporter is None:
        return nullcontext()
    return reporter.activity(label)


@dataclass(frozen=True, slots=True)
class FixRound:
    """Round-state for a step re-run in fix mode (issue #81): what to fix, as text.

    Carries either the auto-fix findings' descriptions (the automatic path, rendered by
    `pipeline/findings.py`'s `describe_auto_fix_findings`) or a human's own typed
    instructions (the "fix" approval response, see `ApprovalResponse` below) -- both
    collapsed to this one `instructions: str` shape so a step's fix-mode prompt only ever
    branches on one field (`ctx.fix_round is not None`), never on which of the two paths
    produced it. Attached to a new `StepContext` via `dataclasses.replace` for each re-run
    (`executor.run_steps` never mutates `ctx`) -- see that module's own docstring for the
    round loop this drives. Consumer: `steps/review.py`'s `ReviewStep.run` (issue #81);
    `steps/test_sufficiency.py` does not consume this yet (issue #82).
    """

    instructions: str


# The fourth approval-park response (issue #81), alongside issue #80's "approve"/"skip"/
# "abort". Kept as a standalone alias (not inlined into `ApprovalResponse.decision`'s
# annotation) so `tui/approval_relay.py`/`tui/screens.py` can import and re-export the
# exact same type rather than each defining their own overlapping `Literal`.
ApprovalDecision = Literal["approve", "skip", "abort", "fix"]


@dataclass(frozen=True, slots=True)
class ApprovalResponse:
    """A human's answer to a parked step's approval request (issue #81, extending issue
    #80's bare `Decision` string).

    `instructions` is set only when `decision == "fix"` -- the free-text instructions a
    human typed in response to `tui.screens.InputPromptScreen`'s follow-up prompt after
    choosing "fix" on `tui.screens.ApprovalPromptScreen` -- and is `None` for every other
    decision. `executor.run_steps` turns a "fix" response into a `FixRound(instructions=
    response.instructions)` and re-runs the parked step; "approve"/"skip" let the run
    continue to the next step exactly as issue #80 already does; "abort" raises
    `executor.RunAbortedError`, unchanged.
    """

    decision: ApprovalDecision
    instructions: str | None = None


@dataclass(frozen=True, slots=True)
class StepContext:
    """Per-run dependencies and state a Step needs in order to run."""

    cwd: Path
    agent: Agent
    diff: str
    intent: Intent
    # Reserved for a future step that pins a non-default `permission_mode` on its own
    # `RunOpts` (see `agent/base.py`'s `RunOpts.on_input_needed`) and needs to pass this
    # through so a blocked-on-stdin subprocess can relay its prompt to a human. No step
    # consumes this yet -- see the module docstring. `cli.py` wires it to
    # `tui.input_relay.InputRelay.request_input` for interactive runs.
    on_input_needed: Callable[[str], Awaitable[str]] | None = None
    # Reports nested sub-step activity (issue #66) -- e.g. one `git fetch` call, or one
    # agent call -- as a second, independent event stream from this run's `StepEvent`
    # "running"/"completed" pair. `None` (the default, and every test that doesn't
    # exercise it) means no reporter is attached; a step calls `self.report_activity(label)`
    # regardless -- see that method below -- so no call site ever needs an `if` branch on
    # whether one is present. Two consumers: issue #65's `ReviewStep` reads this field
    # directly (it has a `ctx` in hand); issue #64's `steps/gitutils.py` `run_git` cannot
    # (no `StepContext` parameter) and instead reads whatever `executor.run_steps` bound
    # ambiently from this same field -- see the module docstring's "Ambient reporting
    # (issue #64)" section. `cli.py` wires this field to a real `tui.activity.ActivityRelay`
    # instance for interactive runs.
    activity_reporter: ActivityReporter | None = None
    # The approval-park seam (issue #80, extended by issue #81): `executor.run_steps` calls
    # this, passing the parking step's own name and its full `StepOutcome`, whenever that
    # `StepOutcome.needs_approval` is True (or, once a step opts into fix rounds via `Step.
    # supports_fix_round`, whenever its `auto_fixable` outcome has exhausted the automatic
    # round cap -- see `executor.py`'s module docstring) -- and blocks until it resolves to
    # an `ApprovalResponse`. Shaped like `on_input_needed` above (a structural callable,
    # `None` by default so every existing test constructing `StepContext` directly keeps
    # passing unchanged), but takes `(step_name, outcome)` rather than a single prompt
    # string: the park logic in `pipeline/` must not itself format `StepOutcome.findings`
    # (typed as bare `object`, see below) into display text -- that is the relay/modal's
    # job, on the `tui/` side of the boundary this field exists to preserve. Unlike
    # `on_input_needed`, this field already has a real consumer as of issue #80:
    # `executor.run_steps`'s own park loop, not a step. `None` (the default, and every test
    # that doesn't park a step) means `run_steps` fails closed with `executor.
    # ApprovalNotAttachedError` rather than hanging or silently treating the park as
    # approved -- mirroring `agent/errors.py`'s `StdinBlockedError`/`RunOpts.
    # on_input_needed`'s own "`None` means fail closed" rule. `cli.py` wires this to
    # `tui.approval_relay.ApprovalRelay.request_approval` for interactive runs.
    on_approval_needed: Callable[[str, StepOutcome], Awaitable[ApprovalResponse]] | None = None
    # Round-state for a fix-mode re-run of the step currently executing (issue #81) -- see
    # `FixRound`'s own docstring above for the exact shape and why automatic and
    # human-typed fix instructions collapse to one field. `None` (the default, and every
    # existing test constructing `StepContext` directly) means "this is a normal run, not a
    # fix round" -- the same "`None` means normal run" precedent `on_input_needed`/
    # `activity_reporter`/`on_approval_needed` already set. `executor.run_steps` is the
    # sole writer: it never mutates `ctx` itself, instead building a new `StepContext` via
    # `dataclasses.replace(ctx, fix_round=FixRound(...))` for each re-run round and passing
    # that forward. Consumer: `steps/review.py`'s `ReviewStep.run` (issue #81), which
    # branches its prompt on `ctx.fix_round is not None`.
    fix_round: FixRound | None = None

    def report_activity(self, label: str) -> AbstractAsyncContextManager[None]:
        """Single-line call site for a step to report one nested unit of work named
        `label`: `async with ctx.report_activity("fetch"): ...`. Delegates to
        `activity_or_nullcontext`, so every call site is this one line with no branching
        needed regardless of whether a reporter is present.
        """

        return activity_or_nullcontext(self.activity_reporter, label)


@dataclass(frozen=True, slots=True)
class StepOutcome:
    """A Step's report back to the executor."""

    needs_approval: bool
    auto_fixable: bool
    findings: object


class Step(ABC):
    """One unit of pipeline work: given a StepContext, produce a StepOutcome."""

    # Empty, matching `abc.ABC`'s own `__slots__ = ()`: without this, subclassing `Step`
    # would give every `@dataclass(slots=True)` implementation a `__dict__` back, silently
    # defeating the memory-layout guarantee `slots=True` exists for.
    __slots__ = ()

    # Per-step opt-in to `executor.run_steps`'s fix-round loop (issue #81). `False` by
    # default -- deliberately -- so a step's `StepOutcome.auto_fixable` is inert to the
    # executor unless that step explicitly declares it knows how to consume `StepContext.
    # fix_round`. This matters concretely for `steps/test_sufficiency.py`'s
    # `TestSufficiencyStep`: it already computes a genuine `auto_fixable=True` the same way
    # `ReviewStep` does (see that step's own `run`), but does not yet build a fix-mode
    # prompt (issue #82) -- gating the round loop on `outcome.auto_fixable` alone would
    # bounce it through capped re-runs that blindly resubmit the same prompt and get the
    # same outcome back, a real behavior change this ticket must not introduce. When this
    # is `False`, `executor.run_steps` behaves exactly as it did before issue #81: only
    # `outcome.needs_approval` parks, `outcome.auto_fixable` is never consulted. Only
    # `steps/review.py`'s `ReviewStep` overrides this to `True`.
    supports_fix_round: ClassVar[bool] = False

    @abstractmethod
    async def run(self, ctx: StepContext) -> StepOutcome:
        """Do this step's work against ``ctx`` and report what happened."""

    def get_name(self) -> str:
        """Return this step's display name, used by ``executor.run_steps`` for events.

        Defaults to the concrete class's name; a step needing a different name (e.g.
        multiple instances of the same step class in one run) overrides this.
        """

        return type(self).__name__


@dataclass(frozen=True, slots=True)
class StepEvent:
    """One progress event from ``executor.run_steps``: a step entering "running", or a
    step's "completed" report (its ``StepOutcome`` plus timing)."""

    step_name: str
    status: Literal["running", "completed"]
    outcome: StepOutcome | None
    started_at: float
    duration: float | None
