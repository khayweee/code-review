"""Round-trip and fixed-order tests for the executor (Milestone 2, issues #13 and #14;
extended to the `StepEvent` stream by Milestone 13's #39).

Proves the full path end to end: a real temporary git checkout with a real diff, minimal
test `Step` implementations that embed that diff in a prompt and call through the real
Milestone 1 `Agent` abstraction (`ClaudeCLI`) pointed at fake CLI scripts, run through
`executor.run_steps`, producing a `StepEvent` stream this test collects and asserts on.

No mocking of `Step` or `Agent` anywhere here -- `_ReviewStep` and `_OrderStep` are real
`Step` subclasses, and `ClaudeCLI` is the real Milestone 1 backend. See
`tests/agent/test_process_group.py` for why this repo goes through the real backend rather
than a mocked one: a mock can't prove what actually happens when the tool runs.

`test_run_steps_binds_the_ambient_activity_reporter_but_a_step_with_no_git_call_reports_nothing`
(issue #64) proves the other half of the ambient-`ActivityReporter`-binding contract
`pipeline/step.py`/`gitutils.py` own: `IntentStep` makes no `git`/agent call, so binding
`current_activity_reporter` around its `run` (which `run_steps` now does for every step,
see `executor.py`'s module docstring) must produce zero activity events -- proving a step
with nothing to report renders no empty nested rows, not that the binding itself is inert.

The approval-park tests below (issue #80) use a minimal synthetic `_ParkingStep` (no real
`git`/agent call needed to prove park/approve/skip/abort semantics) rather than the fake-CLI
`Step`s above -- `steps/rebase.py`'s own `RebaseStep` is proven separately, end to end
against a real repo, in `tests/steps/test_rebase.py` and `tests/test_cli_review.py`.

The fix-round loop tests (issue #81) use a second minimal synthetic step, `_FixableStep`,
mirroring `_ParkingStep`'s "no real git/agent call" shape but with `supports_fix_round =
True` and a caller-supplied sequence of outcomes to return, one per round -- cheaper and
more deterministic than a real fake-CLI round-trip for proving the round-loop/cap/park
wiring itself; `steps/test_review.py` separately proves a real `ReviewStep` fix round
against a real fake-CLI backend (round 1 returns an auto-fix finding, round 2 returns
clean), per this ticket's own testing decision to prefer a real end-to-end round-trip
wherever it's cheap and reserve a synthetic step for the executor-level loop mechanics that
would be needlessly expensive to prove against a real subprocess every time.
"""

from __future__ import annotations

import asyncio
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

import pytest
from pydantic import BaseModel

from code_review.agent import Agent, ClaudeCLI, RunOpts
from code_review.pipeline import (
    ApprovalNotAttachedError,
    ApprovalResponse,
    RunAbortedError,
    Step,
    StepContext,
    StepEvent,
    StepOutcome,
    run_steps,
)
from code_review.pipeline.executor import _MAX_AUTO_FIX_ROUNDS
from code_review.pipeline.findings import Finding, action_or_default, has_blocking_finding
from code_review.steps.intent import Intent, IntentStep
from code_review.tui.activity import ActivityRelay

FAKE_CLI = Path(__file__).parent / "fakes" / "review_findings.py"
ORDER_FAKE_CLI_A = Path(__file__).parent / "fakes" / "order_step_a.py"
ORDER_FAKE_CLI_B = Path(__file__).parent / "fakes" / "order_step_b.py"

# A stand-in Intent for the tests below that don't exercise IntentStep itself -- Milestone
# 3 makes `intent` a required StepContext field, so every test constructing a StepContext
# needs one regardless of whether that test's steps read it.
_STAND_IN_INTENT = Intent(summary="add retry logic", source="explicit", score=1.0)


async def _collect(steps: list[Step], ctx: StepContext) -> list[StepEvent]:
    """Drain `run_steps`'s event stream into a list -- the async-generator equivalent of
    the `list[StepOutcome]` this test file collected before #39."""

    return [event async for event in run_steps(steps, ctx)]


def _completed_outcomes(events: list[StepEvent]) -> list[StepOutcome]:
    """Pull each step's `StepOutcome` off its "completed" event, in order -- what
    `run_steps` used to return directly before it became an event stream."""

    outcomes = []
    for event in events:
        if event.status == "completed":
            assert event.outcome is not None
            outcomes.append(event.outcome)
    return outcomes


class ReviewFindings(BaseModel):
    """A stand-in schema for this slice -- Milestone 4 owns the real `Finding` schema."""

    summary: str
    saw_added_line: bool


@dataclass(frozen=True, slots=True)
class _ReviewStep(Step):
    """Minimal real `Step`: sends `ctx.diff` to the agent, wraps the answer as findings."""

    async def run(self, ctx: StepContext) -> StepOutcome:
        result = await ctx.agent.run(
            RunOpts(
                prompt=f"Review this diff:\n{ctx.diff}",
                cwd=ctx.cwd,
                output_schema=ReviewFindings,
                executable=FAKE_CLI,
            )
        )
        return StepOutcome(
            needs_approval=False,
            auto_fixable=False,
            payload=result.output,
        )


class OrderProbe(BaseModel):
    """Reports which step produced it and whether the *other* step's on-disk marker was
    already visible -- i.e. whether that other step had already run."""

    step: str
    saw_other: bool


@dataclass(frozen=True, slots=True)
class _OrderStep(Step):
    """Minimal real `Step` whose fake CLI leaves a marker in the shared checkout and
    reports whether the other ordering step's marker is already there."""

    executable: Path

    async def run(self, ctx: StepContext) -> StepOutcome:
        result = await ctx.agent.run(
            RunOpts(
                prompt=f"Review this diff:\n{ctx.diff}",
                cwd=ctx.cwd,
                output_schema=OrderProbe,
                executable=self.executable,
            )
        )
        return StepOutcome(
            needs_approval=False,
            auto_fixable=False,
            payload=result.output,
        )


def _run_git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


def _real_repo_with_diff(tmp_path: Path) -> tuple[Path, str]:
    """Build a real temporary git checkout and return it with a real unstaged diff."""

    repo = tmp_path / "repo"
    repo.mkdir()
    _run_git(["init", "-q"], repo)
    _run_git(["config", "user.email", "test@example.com"], repo)
    _run_git(["config", "user.name", "Test"], repo)

    greeting = repo / "greeting.txt"
    greeting.write_text("hello\n")
    _run_git(["add", "greeting.txt"], repo)
    _run_git(["commit", "-q", "-m", "initial"], repo)

    greeting.write_text("hello\nworld\n")
    diff = _run_git(["diff"], repo).stdout

    return repo, diff


def test_step_round_trips_through_executor_against_real_diff(tmp_path: Path) -> None:
    repo, diff = _real_repo_with_diff(tmp_path)
    assert "+world" in diff  # sanity: the diff we built is the one the step should see

    agent: Agent = ClaudeCLI()
    ctx = StepContext(cwd=repo, agent=agent, diff=diff, intent=_STAND_IN_INTENT)
    step: Step = _ReviewStep()

    events = asyncio.run(_collect([step], ctx))
    asyncio.run(agent.close())

    outcomes = _completed_outcomes(events)
    assert len(outcomes) == 1
    outcome = outcomes[0]
    assert isinstance(outcome, StepOutcome)
    assert outcome.needs_approval is False
    assert outcome.auto_fixable is False
    findings = outcome.payload
    assert isinstance(findings, ReviewFindings)
    assert findings.saw_added_line is True
    expected_prompt = f"Review this diff:\n{diff}"
    assert findings.summary == f"reviewed a {len(expected_prompt)}-character prompt"


def test_executor_runs_steps_in_fixed_list_order_against_real_diff(tmp_path: Path) -> None:
    """Two real steps whose fake-CLI outcomes only make sense if run in list order: step
    "a" must run before its marker is visible to step "b", never the reverse. A reordered
    or dropped step flips or omits an assertion below rather than merely being missing
    from an unordered set."""

    repo, diff = _real_repo_with_diff(tmp_path)

    agent: Agent = ClaudeCLI()
    ctx = StepContext(cwd=repo, agent=agent, diff=diff, intent=_STAND_IN_INTENT)
    steps: list[Step] = [_OrderStep(ORDER_FAKE_CLI_A), _OrderStep(ORDER_FAKE_CLI_B)]

    events = asyncio.run(_collect(steps, ctx))
    asyncio.run(agent.close())

    outcomes = _completed_outcomes(events)
    assert len(outcomes) == 2
    first, second = outcomes
    assert isinstance(first.payload, OrderProbe)
    assert isinstance(second.payload, OrderProbe)

    # Step "a" ran first: it could not yet see step "b"'s marker on disk.
    assert first.payload.step == "a"
    assert first.payload.saw_other is False
    # Step "b" ran second: step "a"'s marker was already there for it to see.
    assert second.payload.step == "b"
    assert second.payload.saw_other is True


@dataclass(frozen=True, slots=True)
class _IntentReadingStep(Step):
    """Minimal real `Step` standing in for a later Milestone 3-7 step: reads
    `ctx.intent.summary` directly off the shared `StepContext`, not through `IntentStep`'s
    `StepOutcome`, and surfaces it as its own findings -- makes no agent call, since
    nothing about reading `ctx.intent` needs one."""

    async def run(self, ctx: StepContext) -> StepOutcome:
        return StepOutcome(
            needs_approval=False,
            auto_fixable=False,
            payload=ctx.intent.summary,
        )


def test_intent_step_runs_first_and_a_later_step_reads_the_same_intent_via_ctx(
    tmp_path: Path,
) -> None:
    """End-to-end proof for issue #19: a StepContext built the way `cli.py` builds it
    (`Intent(summary=..., source="explicit", score=1.0)` from the `--intent` flag) drives
    `run_steps([IntentStep(), <later step>], ctx)`, and the later step gets the same
    intent text off `ctx.intent` -- not through `IntentStep`'s `StepOutcome` -- proving
    intent is threaded through the shared context, not passed hand-to-hand between
    steps."""

    repo, diff = _real_repo_with_diff(tmp_path)
    intent_text = "add retry logic with exponential backoff"
    intent = Intent(summary=intent_text, source="explicit", score=1.0)

    agent: Agent = ClaudeCLI()
    ctx = StepContext(cwd=repo, agent=agent, diff=diff, intent=intent)
    steps: list[Step] = [IntentStep(), _IntentReadingStep()]

    events = asyncio.run(_collect(steps, ctx))
    asyncio.run(agent.close())

    outcomes = _completed_outcomes(events)
    assert len(outcomes) == 2
    intent_outcome, later_outcome = outcomes

    assert intent_outcome.needs_approval is False
    assert intent_outcome.auto_fixable is False
    assert intent_outcome.payload is intent

    # The later step never saw `intent_outcome` -- it read `ctx.intent.summary` directly.
    assert later_outcome.payload == intent_text


def test_run_steps_yields_a_running_and_completed_event_per_step_in_order(
    tmp_path: Path,
) -> None:
    """Issue #39: for N steps, `run_steps` yields exactly 2N events, alternating
    running/completed one pair per step, each completed event naming the step that
    preceded it, carrying a non-negative duration, and the right `StepOutcome`."""

    repo, diff = _real_repo_with_diff(tmp_path)

    agent: Agent = ClaudeCLI()
    ctx = StepContext(cwd=repo, agent=agent, diff=diff, intent=_STAND_IN_INTENT)
    steps: list[Step] = [_OrderStep(ORDER_FAKE_CLI_A), _OrderStep(ORDER_FAKE_CLI_B)]

    events = asyncio.run(_collect(steps, ctx))
    asyncio.run(agent.close())

    assert len(events) == 2 * len(steps)
    assert [event.status for event in events] == [
        "running",
        "completed",
        "running",
        "completed",
    ]

    for running_event, completed_event in zip(events[0::2], events[1::2], strict=True):
        assert running_event.outcome is None
        assert running_event.duration is None
        assert completed_event.step_name == running_event.step_name
        assert completed_event.started_at == running_event.started_at
        assert completed_event.outcome is not None
        assert completed_event.duration is not None
        assert completed_event.duration >= 0

    assert events[0].step_name == "_OrderStep"
    first_outcome, second_outcome = events[1].outcome, events[3].outcome
    assert isinstance(first_outcome, StepOutcome)
    assert isinstance(second_outcome, StepOutcome)
    assert isinstance(first_outcome.payload, OrderProbe)
    assert isinstance(second_outcome.payload, OrderProbe)
    assert first_outcome.payload.step == "a"
    assert second_outcome.payload.step == "b"


def test_run_steps_binds_the_ambient_activity_reporter_but_a_step_with_no_git_call_reports_nothing(
    tmp_path: Path,
) -> None:
    """Issue #64's other acceptance criterion: `run_steps` now binds `ctx.activity_reporter`
    ambiently around every `step.run(ctx)` call (see this module's own docstring's final
    paragraph), including `IntentStep`, which makes no `git`/agent call at all. A real
    `ActivityRelay` attached to `ctx` must end the run with nothing queued -- the binding
    itself does not manufacture activity out of a step that never reports one, so the TUI
    renders `IntentStep` with no nested rows, exactly as before this seam existed.
    """

    repo, diff = _real_repo_with_diff(tmp_path)
    intent = Intent(summary="add retry logic", source="explicit", score=1.0)
    relay = ActivityRelay()

    agent: Agent = ClaudeCLI()
    ctx = StepContext(cwd=repo, agent=agent, diff=diff, intent=intent, activity_reporter=relay)
    steps: list[Step] = [IntentStep()]

    events = asyncio.run(_collect(steps, ctx))
    asyncio.run(agent.close())

    outcomes = _completed_outcomes(events)
    assert len(outcomes) == 1
    assert outcomes[0].payload is intent

    # Nothing was ever reported: `relay.next_event()` would hang forever with nothing
    # queued, so bound it with a short timeout instead of asserting on a private queue.
    async def _next_event_or_none() -> object | None:
        try:
            return await asyncio.wait_for(relay.next_event(), timeout=0.05)
        except TimeoutError:
            return None

    assert asyncio.run(_next_event_or_none()) is None


# --- The approval park (issue #80) ------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _ParkingStep(Step):
    """Minimal real `Step` that returns a caller-supplied `StepOutcome` unconditionally --
    no `git`/agent call, so these tests can drive `needs_approval` directly rather than
    depending on a real producer like `steps/rebase.py`'s `RebaseStep` (proven separately,
    end to end, in `tests/steps/test_rebase.py`/`tests/test_cli_review.py`)."""

    outcome: StepOutcome

    async def run(self, ctx: StepContext) -> StepOutcome:
        return self.outcome


@dataclass(frozen=True, slots=True)
class _MarkerStep(Step):
    """A second real `Step`, distinct from `_ParkingStep`, whose own outcome proves the
    loop actually reached it -- i.e. that the run continued past a park."""

    async def run(self, ctx: StepContext) -> StepOutcome:
        return StepOutcome(needs_approval=False, auto_fixable=False, payload="ran")


_PARKING_OUTCOME = StepOutcome(needs_approval=True, auto_fixable=False, payload=["a finding"])


def _fixed_response(decision: str, instructions: str | None = None) -> object:
    """An `on_approval_needed`-shaped callable that always answers
    `ApprovalResponse(decision, instructions)`, and records every `(step_name, outcome)`
    call it received for the caller to assert on."""

    calls: list[tuple[str, StepOutcome]] = []

    async def _answer(step_name: str, outcome: StepOutcome) -> ApprovalResponse:
        calls.append((step_name, outcome))
        return ApprovalResponse(decision=decision, instructions=instructions)  # type: ignore[arg-type]

    _answer.calls = calls  # type: ignore[attr-defined]
    return _answer


def test_run_steps_continues_to_the_next_step_when_the_decision_is_approve(
    tmp_path: Path,
) -> None:
    repo, diff = _real_repo_with_diff(tmp_path)
    answer = _fixed_response("approve")

    agent: Agent = ClaudeCLI()
    ctx = StepContext(
        cwd=repo,
        agent=agent,
        diff=diff,
        intent=_STAND_IN_INTENT,
        on_approval_needed=answer,  # type: ignore[arg-type]
    )
    steps: list[Step] = [_ParkingStep(_PARKING_OUTCOME), _MarkerStep()]

    events = asyncio.run(_collect(steps, ctx))
    asyncio.run(agent.close())

    outcomes = _completed_outcomes(events)
    assert len(outcomes) == 2
    assert outcomes[0] is _PARKING_OUTCOME
    assert outcomes[1].payload == "ran"
    assert answer.calls == [("_ParkingStep", _PARKING_OUTCOME)]  # type: ignore[attr-defined]


def test_run_steps_continues_to_the_next_step_when_the_decision_is_skip(tmp_path: Path) -> None:
    """Skip is recorded by the caller (`tui/app.py`), not `run_steps` itself -- from this
    loop's own perspective, skip and approve both simply let it continue (see `executor.py`'s
    module docstring's "The approval park" section)."""

    repo, diff = _real_repo_with_diff(tmp_path)
    answer = _fixed_response("skip")

    agent: Agent = ClaudeCLI()
    ctx = StepContext(
        cwd=repo,
        agent=agent,
        diff=diff,
        intent=_STAND_IN_INTENT,
        on_approval_needed=answer,  # type: ignore[arg-type]
    )
    steps: list[Step] = [_ParkingStep(_PARKING_OUTCOME), _MarkerStep()]

    events = asyncio.run(_collect(steps, ctx))
    asyncio.run(agent.close())

    outcomes = _completed_outcomes(events)
    assert len(outcomes) == 2
    assert outcomes[1].payload == "ran"


def test_run_steps_raises_run_aborted_error_and_runs_no_further_step_on_abort(
    tmp_path: Path,
) -> None:
    repo, diff = _real_repo_with_diff(tmp_path)
    answer = _fixed_response("abort")

    agent: Agent = ClaudeCLI()
    ctx = StepContext(
        cwd=repo,
        agent=agent,
        diff=diff,
        intent=_STAND_IN_INTENT,
        on_approval_needed=answer,  # type: ignore[arg-type]
    )
    steps: list[Step] = [_ParkingStep(_PARKING_OUTCOME), _MarkerStep()]

    async def _collect_or_raise() -> list[StepEvent]:
        return [event async for event in run_steps(steps, ctx)]

    with pytest.raises(RunAbortedError, match="_ParkingStep") as exc_info:
        asyncio.run(_collect_or_raise())
    asyncio.run(agent.close())

    assert exc_info.value.step_name == "_ParkingStep"


def test_run_steps_only_yields_the_parked_steps_own_running_and_completed_events_on_abort(
    tmp_path: Path,
) -> None:
    """Proves the event stream itself stops exactly where the design nuance says it must:
    the parked step's "running"/"completed" pair is yielded (unconditionally, before the
    park is even checked), and nothing about `_MarkerStep` -- not even a "running" event --
    ever reaches the caller once the human aborts."""

    repo, diff = _real_repo_with_diff(tmp_path)
    answer = _fixed_response("abort")

    agent: Agent = ClaudeCLI()
    ctx = StepContext(
        cwd=repo,
        agent=agent,
        diff=diff,
        intent=_STAND_IN_INTENT,
        on_approval_needed=answer,  # type: ignore[arg-type]
    )
    steps: list[Step] = [_ParkingStep(_PARKING_OUTCOME), _MarkerStep()]

    seen: list[StepEvent] = []

    async def _collect_until_raise() -> None:
        async for event in run_steps(steps, ctx):
            seen.append(event)

    with pytest.raises(RunAbortedError):
        asyncio.run(_collect_until_raise())
    asyncio.run(agent.close())

    assert [event.status for event in seen] == ["running", "completed"]
    assert seen[0].step_name == "_ParkingStep"
    assert seen[1].step_name == "_ParkingStep"


def test_run_steps_fails_closed_when_a_step_parks_with_no_approval_relay_attached(
    tmp_path: Path,
) -> None:
    """The fail-closed rule (`StepContext.on_approval_needed`'s own field comment) --
    mirrors `agent/errors.py`'s `StdinBlockedError`/`on_input_needed`'s identical rule.
    `StepContext`'s default (no `on_approval_needed` passed at all) proves the "every
    existing test that constructs `StepContext` directly keeps passing unchanged"
    acceptance criterion is compatible with this failing closed rather than hanging."""

    repo, diff = _real_repo_with_diff(tmp_path)

    agent: Agent = ClaudeCLI()
    ctx = StepContext(cwd=repo, agent=agent, diff=diff, intent=_STAND_IN_INTENT)
    steps: list[Step] = [_ParkingStep(_PARKING_OUTCOME), _MarkerStep()]

    async def _collect_or_raise() -> list[StepEvent]:
        return [event async for event in run_steps(steps, ctx)]

    with pytest.raises(ApprovalNotAttachedError, match="_ParkingStep") as exc_info:
        asyncio.run(_collect_or_raise())
    asyncio.run(agent.close())

    assert exc_info.value.step_name == "_ParkingStep"


def test_run_steps_does_not_park_and_never_calls_on_approval_needed_when_needs_approval_is_false(
    tmp_path: Path,
) -> None:
    repo, diff = _real_repo_with_diff(tmp_path)
    answer = _fixed_response("abort")  # would blow up the run if this were ever called

    agent: Agent = ClaudeCLI()
    ctx = StepContext(
        cwd=repo,
        agent=agent,
        diff=diff,
        intent=_STAND_IN_INTENT,
        on_approval_needed=answer,  # type: ignore[arg-type]
    )
    steps: list[Step] = [_MarkerStep(), _MarkerStep()]

    events = asyncio.run(_collect(steps, ctx))
    asyncio.run(agent.close())

    outcomes = _completed_outcomes(events)
    assert len(outcomes) == 2
    assert answer.calls == []  # type: ignore[attr-defined]


# --- The fix-round loop (issue #81) ------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _FixableStep(Step):
    """Minimal real `Step` opting into the fix-round loop via `supports_fix_round = True`
    (mirroring `steps/review.py`'s `ReviewStep`), returning outcomes from a caller-supplied
    sequence -- one outcome per round, with the final entry repeated for any round beyond
    the sequence's length (so a test proving the round cap can supply a single
    always-auto-fixable outcome without predicting the exact round count itself). Records
    every `ctx.fix_round` it was called with, in call order, so a test can assert the
    fix-round instructions actually carried across each re-run."""

    outcomes: list[StepOutcome]
    seen_fix_rounds: list[object] = field(default_factory=list)

    supports_fix_round: ClassVar[bool] = True

    async def run(self, ctx: StepContext) -> StepOutcome:
        self.seen_fix_rounds.append(ctx.fix_round)
        index = min(len(self.seen_fix_rounds) - 1, len(self.outcomes) - 1)
        return self.outcomes[index]


@dataclass(frozen=True, slots=True)
class _NonFixableStep(Step):
    """Minimal real `Step` standing in for `steps/test_sufficiency.py`'s
    `TestSufficiencyStep` -- issue #82's own territory, not touched by this ticket -- which
    already computes a genuine `auto_fixable=True` the same way `ReviewStep` does but
    leaves `supports_fix_round` at `Step`'s own `False` default. Proves `executor.run_steps`
    never bounces a step like this through an automatic re-run or parks it on
    `auto_fixable` alone."""

    outcome: StepOutcome

    async def run(self, ctx: StepContext) -> StepOutcome:
        return self.outcome


def test_run_steps_auto_fix_round_re_runs_exactly_once_with_fix_round_context_before_park(
    tmp_path: Path,
) -> None:
    """Acceptance criterion: a `ReviewStep`-shaped outcome (here, `_FixableStep`) with at
    least one auto-fix finding and no ask-user finding triggers exactly one automatic
    re-run before any park is offered, and the re-run's `StepContext` actually carries
    `fix_round` with instructions describing the auto-fix finding."""

    repo, diff = _real_repo_with_diff(tmp_path)
    auto_fix_finding = Finding(
        severity="warning",
        description="extract a helper function",
        action="auto-fix",
        review_scope="source",
    )
    round_one = StepOutcome(needs_approval=False, auto_fixable=True, payload=[auto_fix_finding])
    round_two = StepOutcome(needs_approval=False, auto_fixable=False, payload=[])
    step = _FixableStep(outcomes=[round_one, round_two])
    answer = _fixed_response("abort")  # would blow up the run if this were ever called

    agent: Agent = ClaudeCLI()
    ctx = StepContext(
        cwd=repo,
        agent=agent,
        diff=diff,
        intent=_STAND_IN_INTENT,
        on_approval_needed=answer,  # type: ignore[arg-type]
    )

    events = asyncio.run(_collect([step], ctx))
    asyncio.run(agent.close())

    outcomes = _completed_outcomes(events)
    assert len(outcomes) == 2  # exactly one automatic re-run, no more
    assert outcomes[0] is round_one
    assert outcomes[1] is round_two
    assert answer.calls == []  # type: ignore[attr-defined]  -- never parked

    assert step.seen_fix_rounds[0] is None  # the first round is a normal run
    second_round_fix = step.seen_fix_rounds[1]
    assert second_round_fix is not None
    assert "extract a helper function" in second_round_fix.instructions  # type: ignore[attr-defined]
    assert "warning" in second_round_fix.instructions  # type: ignore[attr-defined]


def test_run_steps_stops_automatic_fix_rounds_once_the_cap_is_exhausted_and_parks(
    tmp_path: Path,
) -> None:
    """Acceptance criterion: once the round cap is exhausted, a still-`auto_fixable`
    outcome falls through to Ticket 1's park path rather than looping forever."""

    repo, diff = _real_repo_with_diff(tmp_path)
    auto_fix_finding = Finding(
        severity="info", description="keep fixing forever", action="auto-fix", review_scope="source"
    )
    always_auto_fixable = StepOutcome(
        needs_approval=False, auto_fixable=True, payload=[auto_fix_finding]
    )
    # More outcomes than the cap could ever consume -- proves the loop actually stops
    # rather than merely running out of a short sequence.
    step = _FixableStep(outcomes=[always_auto_fixable] * (_MAX_AUTO_FIX_ROUNDS + 5))
    answer = _fixed_response("approve")

    agent: Agent = ClaudeCLI()
    ctx = StepContext(
        cwd=repo,
        agent=agent,
        diff=diff,
        intent=_STAND_IN_INTENT,
        on_approval_needed=answer,  # type: ignore[arg-type]
    )

    events = asyncio.run(_collect([step], ctx))
    asyncio.run(agent.close())

    outcomes = _completed_outcomes(events)
    # One initial round plus `_MAX_AUTO_FIX_ROUNDS` automatic re-runs, then it parks.
    assert len(outcomes) == _MAX_AUTO_FIX_ROUNDS + 1
    assert all(outcome is always_auto_fixable for outcome in outcomes)
    assert len(answer.calls) == 1  # type: ignore[attr-defined]  -- parked exactly once


def test_run_steps_never_auto_fixes_a_finding_with_unset_action_even_on_a_fix_round_step(
    tmp_path: Path,
) -> None:
    """Regression pinning the fail-safe default (`pipeline/findings.py`'s
    `action_or_default`) through the full executor loop, not just the pure-function tests
    in `tests/pipeline/test_findings.py`: a finding whose `action` is unset must never
    resolve to "auto-fix", so it can only ever reach the park path -- never the automatic
    fix-round path -- even on a step that has opted into fix rounds
    (`supports_fix_round=True`)."""

    repo, diff = _real_repo_with_diff(tmp_path)
    unset_action_finding = Finding(
        severity="warning", description="no action set", review_scope="source"
    )
    assert unset_action_finding.action is None

    # Computed exactly the way `steps/review.py`'s `ReviewStep.run` computes its own
    # `StepOutcome` from a list of findings -- see that step's own comment on the
    # "needs_approval xor auto_fixable" invariant.
    blocking = has_blocking_finding([unset_action_finding])
    has_auto_fix = any(action_or_default(f.action) == "auto-fix" for f in [unset_action_finding])
    outcome = StepOutcome(
        needs_approval=blocking,
        auto_fixable=has_auto_fix and not blocking,
        payload=[unset_action_finding],
    )
    # The fail-safe default: unset action resolves to "ask-user", never "auto-fix".
    assert outcome.needs_approval is True
    assert outcome.auto_fixable is False

    step = _FixableStep(outcomes=[outcome])
    answer = _fixed_response("approve")

    agent: Agent = ClaudeCLI()
    ctx = StepContext(
        cwd=repo,
        agent=agent,
        diff=diff,
        intent=_STAND_IN_INTENT,
        on_approval_needed=answer,  # type: ignore[arg-type]
    )

    events = asyncio.run(_collect([step], ctx))
    asyncio.run(agent.close())

    outcomes = _completed_outcomes(events)
    assert len(outcomes) == 1  # no automatic re-run ever happened
    assert step.seen_fix_rounds == [None]  # the step never entered fix-round mode
    assert answer.calls == [("_FixableStep", outcome)]  # type: ignore[attr-defined]  -- parked


def test_run_steps_does_not_round_or_park_a_step_that_does_not_support_fix_rounds(
    tmp_path: Path,
) -> None:
    """Acceptance criterion: `TestSufficiencyStep` (mirrored here by `_NonFixableStep`, a
    synthetic `supports_fix_round=False` step with a genuine `auto_fixable=True` outcome,
    exactly as `TestSufficiencyStep.run` already computes today) is NOT bounced through any
    auto-fix round and does NOT park on `auto_fixable` alone -- only `needs_approval` parks
    it, exactly as before this ticket. This is the regression this ticket's design brief
    most wanted pinned: gating the round loop off `outcome.auto_fixable` alone (instead of
    `step.supports_fix_round`) would have made this test fail."""

    repo, diff = _real_repo_with_diff(tmp_path)
    always_auto_fixable_no_approval = StepOutcome(
        needs_approval=False, auto_fixable=True, payload=["would be auto-fixable"]
    )
    step = _NonFixableStep(always_auto_fixable_no_approval)
    assert step.supports_fix_round is False
    answer = _fixed_response("abort")  # would blow up the run if this were ever called

    agent: Agent = ClaudeCLI()
    ctx = StepContext(
        cwd=repo,
        agent=agent,
        diff=diff,
        intent=_STAND_IN_INTENT,
        on_approval_needed=answer,  # type: ignore[arg-type]
    )
    steps: list[Step] = [step, _MarkerStep()]

    events = asyncio.run(_collect(steps, ctx))
    asyncio.run(agent.close())

    outcomes = _completed_outcomes(events)
    # Exactly one round for the auto-fixable-but-non-opted-in step, then the next step ran
    # -- no automatic re-run, no park.
    assert len(outcomes) == 2
    assert outcomes[0] is always_auto_fixable_no_approval
    assert outcomes[1].payload == "ran"
    assert answer.calls == []  # type: ignore[attr-defined]


# --- step_outcomes threading (issue #119) -------------------------------------------------


@dataclass(frozen=True, slots=True)
class _ReportingStep(Step):
    """Minimal real `Step` that records `ctx.step_outcomes` exactly as seen at call time,
    then returns a fixed outcome -- proves a later step can read an earlier step's already
    -settled `StepOutcome` via `StepContext.step_outcomes` (issue #119)."""

    outcome: StepOutcome
    seen_step_outcomes: list[object] = field(default_factory=list)

    async def run(self, ctx: StepContext) -> StepOutcome:
        self.seen_step_outcomes.append(ctx.step_outcomes)
        return self.outcome


def test_run_steps_threads_an_earlier_steps_settled_outcome_into_a_later_steps_ctx(
    tmp_path: Path,
) -> None:
    """Acceptance criterion: a later step's `StepContext.step_outcomes` carries the exact
    `StepOutcome` an earlier step in the same run produced, keyed by `get_name()`."""

    repo, diff = _real_repo_with_diff(tmp_path)
    reporting_step = _ReportingStep(
        outcome=StepOutcome(needs_approval=False, auto_fixable=False, payload=[])
    )

    agent: Agent = ClaudeCLI()
    ctx = StepContext(cwd=repo, agent=agent, diff=diff, intent=_STAND_IN_INTENT)
    steps: list[Step] = [_MarkerStep(), reporting_step]

    asyncio.run(_collect(steps, ctx))
    asyncio.run(agent.close())

    assert len(reporting_step.seen_step_outcomes) == 1
    seen = reporting_step.seen_step_outcomes[0]
    assert seen == {
        "_MarkerStep": StepOutcome(needs_approval=False, auto_fixable=False, payload="ran")
    }


def test_run_steps_gives_the_first_step_in_a_run_an_empty_step_outcomes(tmp_path: Path) -> None:
    repo, diff = _real_repo_with_diff(tmp_path)
    reporting_step = _ReportingStep(
        outcome=StepOutcome(needs_approval=False, auto_fixable=False, payload=[])
    )

    agent: Agent = ClaudeCLI()
    ctx = StepContext(cwd=repo, agent=agent, diff=diff, intent=_STAND_IN_INTENT)

    asyncio.run(_collect([reporting_step], ctx))
    asyncio.run(agent.close())

    assert reporting_step.seen_step_outcomes == [{}]


def test_run_steps_only_records_a_fix_round_steps_final_settled_outcome_not_each_round(
    tmp_path: Path,
) -> None:
    """A step that goes through one or more automatic fix rounds before settling must land
    exactly one entry in a later step's `step_outcomes` -- the final, settled outcome, never
    a stale intermediate round's outcome."""

    repo, diff = _real_repo_with_diff(tmp_path)
    auto_fix_finding = Finding(
        severity="warning", description="extract a helper", action="auto-fix", review_scope="source"
    )
    round_one = StepOutcome(needs_approval=False, auto_fixable=True, payload=[auto_fix_finding])
    round_two = StepOutcome(needs_approval=False, auto_fixable=False, payload=[])
    fixable_step = _FixableStep(outcomes=[round_one, round_two])
    reporting_step = _ReportingStep(
        outcome=StepOutcome(needs_approval=False, auto_fixable=False, payload=[])
    )

    agent: Agent = ClaudeCLI()
    ctx = StepContext(cwd=repo, agent=agent, diff=diff, intent=_STAND_IN_INTENT)
    steps: list[Step] = [fixable_step, reporting_step]

    events = asyncio.run(_collect(steps, ctx))
    asyncio.run(agent.close())

    # Sanity: the fix round actually happened (two completed events for _FixableStep, one
    # for _ReportingStep).
    assert len(_completed_outcomes(events)) == 3

    assert len(reporting_step.seen_step_outcomes) == 1
    seen = reporting_step.seen_step_outcomes[0]
    assert seen == {"_FixableStep": round_two}


def test_run_steps_fix_approval_response_re_runs_with_instructions_and_is_never_capped(
    tmp_path: Path,
) -> None:
    """Acceptance criteria: a "fix" `ApprovalResponse` causes a re-run with a `FixRound`
    carrying the human's own typed instructions, and choosing "fix" repeatedly (here, more
    times than `_MAX_AUTO_FIX_ROUNDS`) is never capped -- only the automatic path is."""

    repo, diff = _real_repo_with_diff(tmp_path)
    parking_outcome = StepOutcome(
        needs_approval=True, auto_fixable=False, payload=["needs a human"]
    )
    # `_MAX_AUTO_FIX_ROUNDS + 2` fix rounds, well past the automatic path's own cap, to
    # prove this path is genuinely uncapped rather than merely under-tested against it.
    step = _FixableStep(outcomes=[parking_outcome] * (_MAX_AUTO_FIX_ROUNDS + 3))

    typed_instructions = [f"fix attempt {i}" for i in range(_MAX_AUTO_FIX_ROUNDS + 2)]
    responses = iter(
        [ApprovalResponse(decision="fix", instructions=text) for text in typed_instructions]
        + [ApprovalResponse(decision="approve")]
    )
    calls: list[tuple[str, StepOutcome]] = []

    async def _answer(step_name: str, outcome: StepOutcome) -> ApprovalResponse:
        calls.append((step_name, outcome))
        return next(responses)

    agent: Agent = ClaudeCLI()
    ctx = StepContext(
        cwd=repo,
        agent=agent,
        diff=diff,
        intent=_STAND_IN_INTENT,
        on_approval_needed=_answer,
    )

    events = asyncio.run(_collect([step], ctx))
    asyncio.run(agent.close())

    outcomes = _completed_outcomes(events)
    # One round per typed "fix" plus the final "approve" round.
    assert len(outcomes) == len(typed_instructions) + 1
    assert len(calls) == len(typed_instructions) + 1

    assert step.seen_fix_rounds[0] is None
    carried_instructions = [fr.instructions for fr in step.seen_fix_rounds[1:]]  # type: ignore[union-attr]
    assert carried_instructions == typed_instructions
