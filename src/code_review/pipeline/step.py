"""Step abstract base class, StepContext, StepOutcome: the pipeline's core types.

`StepContext` is a bag of per-pipeline-run dependencies a step needs (see
`docs/GLOSSARY.md`'s "run" entry for the pipeline-run vs agent-call distinction): working
directory, `Agent`, diff, `Intent`, plus fix-loop/approval-park state (`on_approval_needed`,
`fix_round`), plus `step_outcomes`, a read-only record of earlier steps' already-settled
`StepOutcome`s a later step may summarize (see that field's own docstring and
`pipeline/AGENTS.md` for why this doesn't reverse the "no step branches on a sibling's
outcome" invariant).

`ActivityReporter` is a structural `Protocol` so `pipeline/`/`steps/` never import `tui/`
directly; satisfied structurally by `tui.activity.ActivityRelay`. `StepContext.
activity_reporter` carries an optional instance; `StepContext.report_activity(label)` is
the single-line call site (`async with ctx.report_activity("fetch"): ...`), a second event
stream independent of `StepEvent`.

Ambient reporting: `steps/gitutils.py`'s `run_git` has no `StepContext` to read
`activity_reporter` off of, so `current_activity_reporter` (a module-level
`contextvars.ContextVar`) carries whichever reporter is in scope for the currently running
step, read via `.get()`. `executor.run_steps` is the sole writer, `.set()`/`.reset()`-ing it
immediately around each `step.run(ctx)` call so the value never leaks across steps or
sibling tasks. `activity_or_nullcontext` factors out the shared nullcontext-when-absent
branch.

`StepOutcome.needs_approval` pauses the run for a human decision; `auto_fixable` lets a step
that opts in via `Step.supports_fix_round = True` get bounded automatic re-runs before
falling through to a park. A park's "fix" response lets a human re-run with their own
instructions, uncapped -- see `executor.py`'s module docstring for the full loop.
`StepOutcome.payload` is the closed union of every shape a step actually reports:
`list[Finding] | ReviewOutput | TestSufficiencyOutput | Intent`. A generic consumer (the
executor's fix-round path, the TUI's findings display) narrows it with `isinstance` over
that same closed set instead of duck-typing an `object`.

`StepEvent` is `executor.run_steps`'s progress unit: one per "running" and one per
"completed" per step/round. `started_at`/`duration` use `time.monotonic()`, not wall-clock
time.
"""

from __future__ import annotations

import contextvars
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Mapping
from contextlib import AbstractAsyncContextManager, nullcontext
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, Literal, Protocol

from code_review.agent import Agent

if TYPE_CHECKING:
    # steps/ depends on pipeline/, never the reverse; a top-level import would be circular.
    # Lazy annotation (from __future__ import annotations) makes a type-checking-only
    # import sufficient. findings.py imports this module at top level (see its own
    # docstring), so Finding must stay TYPE_CHECKING-only here too or the cycle inverts.
    from code_review.pipeline.findings import Finding
    from code_review.steps.intent import Intent
    from code_review.steps.review import ReviewOutput
    from code_review.steps.test_sufficiency import TestSufficiencyOutput


class ActivityReporter(Protocol):
    """Structural, one-method contract for reporting nested sub-step activity.

    Satisfied structurally by `tui.activity.ActivityRelay`; nothing here imports `tui/`.
    A step never calls this directly, only via `StepContext.report_activity`.
    """

    def activity(self, label: str) -> AbstractAsyncContextManager[None]:
        """Report one nested unit of work named `label`, open for the context manager's body."""
        ...


# ActivityReporter currently in scope for the running step. executor.run_steps is the sole
# writer; steps/gitutils.py's run_git is the sole external reader (no StepContext to read
# activity_reporter off of).
current_activity_reporter: contextvars.ContextVar[ActivityReporter | None] = contextvars.ContextVar(
    "current_activity_reporter", default=None
)


def activity_or_nullcontext(
    reporter: ActivityReporter | None, label: str
) -> AbstractAsyncContextManager[None]:
    """`reporter.activity(label)` if `reporter` is set, else a no-op nullcontext."""

    if reporter is None:
        return nullcontext()
    return reporter.activity(label)


@dataclass(frozen=True, slots=True)
class FixRound:
    """What to fix in a fix-mode re-run, as free text.

    Carries either the auto-fix findings' rendered description
    (`pipeline/findings.py`'s `describe_auto_fix_findings`) or a human's typed "fix"
    instructions, collapsed to one `instructions: str` so a step's prompt only ever
    branches on `ctx.fix_round is not None`.
    """

    instructions: str


# Standalone alias (not inlined) so tui/approval_relay.py and tui/screens.py can import the
# same type instead of each defining an overlapping Literal.
ApprovalDecision = Literal["approve", "skip", "abort", "fix"]


@dataclass(frozen=True, slots=True)
class ApprovalResponse:
    """A human's answer to a parked step's approval request.

    `instructions` is set only when `decision == "fix"` (the human's typed text), `None`
    otherwise. `executor.run_steps` turns a "fix" response into a `FixRound` and re-runs
    the step; "approve"/"skip" continue to the next step; "abort" raises
    `executor.RunAbortedError`.
    """

    decision: ApprovalDecision
    instructions: str | None = None


@dataclass(frozen=True, slots=True)
class StepContext:
    """Per-pipeline-run dependencies and state a Step needs (see `docs/GLOSSARY.md`'s "run"
    entry for the pipeline-run vs agent-call distinction; contrast `agent/base.py`'s
    `RunOpts`, scoped to one agent call).
    """

    cwd: Path
    agent: Agent
    diff: str
    intent: Intent
    # Relay for interactive-input prompts, passed through to a step's own RunOpts. Reserved:
    # no step consumes it yet. cli.py wires it to tui.input_relay.InputRelay.request_input.
    on_input_needed: Callable[[str], Awaitable[str]] | None = None
    # Reports nested sub-step activity as a second event stream, independent of StepEvent.
    # None means no reporter attached; a step always calls self.report_activity(label)
    # regardless (see that method). steps/gitutils.py's run_git has no StepContext and
    # instead reads the ambient current_activity_reporter bound from this field. cli.py
    # wires this to a real tui.activity.ActivityRelay for interactive runs.
    activity_reporter: ActivityReporter | None = None
    # Approval-park seam: executor.run_steps awaits this with (step_name, outcome) whenever
    # a step parks, blocking until an ApprovalResponse comes back. Takes the full step name
    # + outcome rather than a prompt string because formatting StepOutcome.payload for
    # display is the relay/modal's job, not pipeline/'s. None means run_steps fails closed
    # with ApprovalNotAttachedError rather than hanging or silently approving. cli.py wires
    # this to tui.approval_relay.ApprovalRelay.request_approval.
    on_approval_needed: Callable[[str, StepOutcome], Awaitable[ApprovalResponse]] | None = None
    # Round-state for a fix-mode re-run of the step currently executing; see FixRound. None
    # means a normal run, not a fix round. executor.run_steps is the sole writer, via
    # dataclasses.replace (ctx itself is never mutated).
    fix_round: FixRound | None = None
    # Read-only reporting channel: outcomes already produced by earlier steps in this
    # pipeline run, keyed by step.get_name(). executor.run_steps is the sole writer, via
    # dataclasses.replace, once each step's slot fully settles (after its fix-round loop,
    # if any, resolves to a final outcome) -- never once per round. A later step may read
    # this to summarize a sibling's already-computed output (e.g. PRStep rendering
    # ReviewStep's risk verdict); it is never a signal a step uses to decide whether or how
    # to run itself or a sibling -- that would reverse the "step order/execution never
    # branches on a prior StepOutcome" invariant this field is deliberately scoped not to
    # touch. See pipeline/AGENTS.md for the full rationale.
    step_outcomes: Mapping[str, StepOutcome] = field(default_factory=dict)

    def report_activity(self, label: str) -> AbstractAsyncContextManager[None]:
        """Report one nested unit of work: `async with ctx.report_activity("fetch"): ...`."""

        return activity_or_nullcontext(self.activity_reporter, label)

    def with_fix_round(self, instructions: str) -> StepContext:
        """Return a copy of this StepContext for a fix-mode re-run of the same step, with
        `fix_round` set to `FixRound(instructions)`. Every other field carries over
        unchanged; this StepContext itself is never mutated (frozen) -- see `executor.py`'s
        fix-round loop, the sole caller.
        """

        return replace(self, fix_round=FixRound(instructions=instructions))


@dataclass(frozen=True, slots=True)
class StepOutcome:
    """A Step's report back to the executor."""

    needs_approval: bool
    auto_fixable: bool
    # The closed set of shapes a step actually reports -- see module docstring. A step
    # narrows this back to whichever member it produced, typically via isinstance.
    payload: list[Finding] | ReviewOutput | TestSufficiencyOutput | Intent


class Step(ABC):
    """One unit of pipeline work: given a StepContext, produce a StepOutcome."""

    # Without this, subclassing Step would give every @dataclass(slots=True) implementation
    # a __dict__ back, defeating slots=True's memory-layout guarantee.
    __slots__ = ()

    # Per-step opt-in to executor.run_steps's fix-round loop. False by default so a step's
    # auto_fixable outcome is inert unless the step actually knows how to consume
    # StepContext.fix_round. Only ReviewStep overrides this to True.
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
