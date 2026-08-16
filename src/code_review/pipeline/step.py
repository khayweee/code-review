"""Step abstract base class, StepContext, StepOutcome: the pipeline's core types.

`StepContext` is a bag of per-pipeline-run dependencies a step needs (see
`docs/GLOSSARY.md`'s "run" entry for the pipeline-run vs agent-call distinction): working
directory, branch under review, `Agent`, diff, `Intent`, plus fix-loop/approval-park state
(`on_approval_needed`, `fix_round`), plus `step_outcomes`, a read-only record of earlier
steps' already-settled `StepOutcome`s a later step may summarize (see that field's own
docstring and `pipeline/AGENTS.md` for why this doesn't reverse the "no step branches on a
sibling's outcome" invariant), plus `StepOutcome.cwd_override`, a narrower, mandatory
counterpart to `step_outcomes` that lets `WorktreeStep` redirect `ctx.cwd` for every step
after it (see that field's own docstring and `pipeline/AGENTS.md`'s WorktreeStep section).

`ActivityReporter` is a structural `Protocol` so `pipeline/`/`steps/` never import `tui/`
directly; satisfied structurally by `tui.activity.ActivityRelay`. `StepContext.
activity_reporter` carries an optional instance; `StepContext.report_activity(label)` is
the single-line call site (`async with ctx.report_activity("fetch") as activity: ...`) for a
block of work, a second event stream independent of `StepEvent`. The yielded
`ActivityHandle` lets the block's own body report failure (`activity.fail("exit 1")`) --
e.g. `run_git`/`_run_gh` marking a nonzero subprocess exit -- without raising, since neither
ever raises on an ordinary command failure. `StepContext.log(message)` is the one-shot
sibling for a single point-in-time event (e.g. one LLM tool call) that has no natural block
to open/close -- `async with ctx.report_activity(label): pass` would work but `await
ctx.log(label)` says what's actually happening.

Ambient reporting: `steps/gitutils.py`'s `run_git` has no `StepContext` to read
`activity_reporter` off of, so `current_activity_reporter` (a module-level
`contextvars.ContextVar`) carries whichever reporter is in scope for the currently running
step, read via `.get()`. `executor.run_steps` is the sole writer, `.set()`/`.reset()`-ing it
immediately around each `step.run(ctx)` call so the value never leaks across steps or
sibling tasks. `report_activity`/`log_activity` are the null-safe primitives every
block-shaped/one-shot activity report funnels through, whether the caller is `ctx`-bound or
ambient; `start_activity`/`finish_activity` are their manually-paired sibling, for a span
whose open and close happen at two different callback call sites that can't share a Python
block (see their own docstrings).

`StepOutcome.needs_approval` pauses the run for a human decision; `auto_fixable` lets a step
that opts in via `Step.supports_fix_round = True` get bounded automatic re-runs before
falling through to a park. A park's "fix" response lets a human re-run with their own
instructions, uncapped -- see `executor.py`'s module docstring for the full loop.
`StepOutcome.payload` is the closed union of every shape a step actually reports:
`list[Finding] | ReviewOutput | TestSufficiencyOutput | Intent | PullRequestOutcome`. A
generic consumer (the executor's fix-round path, the TUI's findings display) narrows it
with `isinstance` over that same closed set instead of duck-typing an `object`.

`FixRound`/`ApprovalDecision`/`ApprovalResponse`/`StepEvent` are passive plumbing types, not
defined in this file -- they live in `pipeline/schemas.py` (imported here at top level,
since `with_fix_round` constructs a `FixRound` at runtime) alongside every other
frozen/slotted dataclass `pipeline/` shares with its callers. `StepEvent` is
`executor.run_steps`'s progress unit: one per "running" and one per "completed" per
step/round. `started_at`/`duration` use `time.monotonic()`, not wall-clock time.
"""

from __future__ import annotations

import contextvars
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Mapping
from contextlib import AbstractAsyncContextManager, nullcontext
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, Protocol

from code_review.agent import Agent, Usage
from code_review.pipeline.schemas import ApprovalResponse, FixRound

if TYPE_CHECKING:
    # steps/ depends on pipeline/, never the reverse; a top-level import would be circular.
    # Lazy annotation (from __future__ import annotations) makes a type-checking-only
    # import sufficient. findings.py imports this module at top level (see its own
    # docstring), so Finding must stay TYPE_CHECKING-only here too or the cycle inverts.
    from code_review.pipeline.findings import Finding
    from code_review.steps.intent import Intent
    from code_review.steps.pr import PullRequestOutcome
    from code_review.steps.review import ReviewOutput
    from code_review.steps.test_sufficiency import TestSufficiencyOutput


@dataclass(slots=True)
class ActivityHandle:
    """Mutable handle yielded by an open `activity()` block, letting the block's own body
    report that the work it wraps failed (e.g. `run_git` marking a nonzero git exit).
    `error` stays `None` for the common/successful case.
    """

    error: str | None = None

    def fail(self, detail: str) -> None:
        self.error = detail


class ActivityReporter(Protocol):
    """Structural, four-method contract for reporting nested sub-step activity.

    Satisfied structurally by `tui.activity.ActivityRelay`; nothing here imports `tui/`.
    A step never calls this directly, only via `StepContext.report_activity`/
    `StepContext.log` (and the ambient `start_activity`/`finish_activity` module-level
    wrappers below, for a span whose open/close can't share a Python block).
    """

    def activity(self, label: str) -> AbstractAsyncContextManager[ActivityHandle]:
        """Report one nested unit of work named `label`, open for the context manager's
        body. The yielded `ActivityHandle` lets the body report failure via `.fail(detail)`.
        """
        ...

    async def log(self, label: str) -> None:
        """Report one already-finished, near-zero-duration activity named `label` -- a
        moment in time (e.g. one LLM tool call) rather than a block of work. Semantically
        equivalent to `async with self.activity(label): pass`, but without requiring a
        caller that only has a point-in-time callback (not a block) to fabricate one.
        """
        ...

    async def start(self, label: str) -> int:
        """Open a span named `label`, closed later by a separate `finish(activity_id, ...)`
        call rather than exiting an `async with` block -- for a caller whose open/close
        happen at two different callback call sites (e.g. `tool_stream_relay`'s
        `TOOL_USE`/`TOOL_RESULT` pair). Returns the new `activity_id`.
        """
        ...

    async def finish(self, activity_id: int, label: str, *, error: str | None = None) -> None:
        """Close the span opened by the matching `start(...)` call. `error` mirrors
        `ActivityHandle.fail(detail)`'s effect on the "finished" event.
        """
        ...


# ActivityReporter currently in scope for the running step. executor.run_steps is the sole
# writer; steps/gitutils.py's run_git is the sole external reader (no StepContext to read
# activity_reporter off of).
current_activity_reporter: contextvars.ContextVar[ActivityReporter | None] = contextvars.ContextVar(
    "current_activity_reporter", default=None
)


def report_activity(
    reporter: ActivityReporter | None, label: str
) -> AbstractAsyncContextManager[ActivityHandle]:
    """`reporter.activity(label)` if `reporter` is set, else a no-op nullcontext yielding a
    fresh, unread `ActivityHandle` -- the null-safe primitive every block-shaped activity
    report funnels through, whether the caller has a `StepContext` (`StepContext.
    report_activity` is a thin `self`-bound wrapper around this) or only the ambient
    `current_activity_reporter` (`steps/gitutils.py`, `scm/github.py`). Returning a real
    handle even in the no-reporter case means `activity.fail(...)` is always safely callable
    with no branching at call sites. See `log_activity` for the one-shot sibling.
    """

    if reporter is None:
        return nullcontext(ActivityHandle())
    return reporter.activity(label)


async def log_activity(reporter: ActivityReporter | None, label: str) -> None:
    """`reporter.log(label)` if `reporter` is set, else a no-op -- the null-safe, one-shot
    sibling of `report_activity`, for a point-in-time event with no block to wrap.
    `StepContext.log` is a thin `self`-bound wrapper around this.
    """

    if reporter is not None:
        await reporter.log(label)


async def start_activity(reporter: ActivityReporter | None, label: str) -> int | None:
    """`reporter.start(label)` if `reporter` is set, else `None` -- the null-safe half of
    the manually-paired open/close primitive, for a span whose open and close happen at two
    different callback call sites (e.g. `tool_stream_relay`'s `TOOL_USE`/`TOOL_RESULT`
    pair). Pass the returned id to `finish_activity`; `None` means there is nothing to
    close.
    """

    if reporter is None:
        return None
    return await reporter.start(label)


async def finish_activity(
    reporter: ActivityReporter | None, activity_id: int, label: str, *, error: str | None = None
) -> None:
    """`reporter.finish(activity_id, label, error=error)` if `reporter` is set, else a
    no-op -- the null-safe close half of `start_activity`.
    """

    if reporter is not None:
        await reporter.finish(activity_id, label, error=error)


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
    # The branch under review, verified to exist before any Step runs (cli.py's
    # _verify_branch_exists). WorktreeStep checks this out --detached (never by name, to
    # avoid colliding with ctx.cwd's own checkout of the same branch -- the common case
    # when a developer reviews the branch they're currently on) into its throwaway
    # worktree, so ctx.cwd's HEAD is detached from WorktreeStep onward. RebaseStep doesn't
    # need a name (git rebase works the same on a detached HEAD), but PRStep does -- it
    # reads this field directly rather than re-deriving "the branch under review" from
    # ctx.cwd's HEAD (see those steps' own module docstrings). Required, not defaulted,
    # matching cwd/agent/diff/intent -- see pipeline/AGENTS.md's WorktreeStep section.
    branch: str
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

    def report_activity(self, label: str) -> AbstractAsyncContextManager[ActivityHandle]:
        """Report one nested unit of work: `async with ctx.report_activity("fetch"): ...`.
        Thin `self.activity_reporter`-bound wrapper around the module-level `report_activity`.
        """

        return report_activity(self.activity_reporter, label)

    async def log(self, message: str) -> None:
        """Report one point-in-time event: `await ctx.log("wrote 3 lines")`. Thin
        `self.activity_reporter`-bound wrapper around the module-level `log_activity`.
        """

        await log_activity(self.activity_reporter, message)

    def with_fix_round(self, instructions: str) -> StepContext:
        """Return a copy of this StepContext for a fix-mode re-run of the same step, with
        `fix_round` set to `FixRound(instructions)`. Every other field carries over
        unchanged; this StepContext itself is never mutated (frozen) -- see `executor.py`'s
        fix-round loop, the sole caller.
        """

        return replace(self, fix_round=FixRound(instructions=instructions))


@dataclass(frozen=True, slots=True)
class StepOutcome:
    """A Step's report back to the executor. `usage` carries this round's agent-call token
    usage, for `pipeline.run_report` to sum across every round of a run."""

    needs_approval: bool
    auto_fixable: bool
    # The closed set of shapes a step actually reports -- see module docstring. A step
    # narrows this back to whichever member it produced, typically via isinstance.
    payload: list[Finding] | ReviewOutput | TestSufficiencyOutput | Intent | PullRequestOutcome
    # Redirects ctx.cwd for every step after this one, once this step's slot settles
    # (executor.run_steps folds it in the same place/way it folds step_outcomes). None
    # (the default) leaves ctx.cwd untouched -- true for every step but WorktreeStep, whose
    # whole job is pointing the rest of the pipeline at a freshly created worktree. See
    # pipeline/AGENTS.md's WorktreeStep section for why this is a narrower, different kind
    # of cross-step effect than step_outcomes (mandatory shared infrastructure state every
    # later step's git calls depend on, not optional data a step may choose to read).
    cwd_override: Path | None = None
    # This round's agent-call usage (Agent.run's Result.usage), or None for a step that made
    # no agent call. pipeline.run_report.build_run_report sums this across every round.
    usage: Usage | None = None


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
