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
"""

from __future__ import annotations

import asyncio
import subprocess
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel

from code_review.agent import Agent, ClaudeCLI, RunOpts
from code_review.pipeline import Step, StepContext, StepEvent, StepOutcome, run_steps
from code_review.steps.intent import Intent, IntentStep

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
            findings=result.output,
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
            findings=result.output,
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
    findings = outcome.findings
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
    assert isinstance(first.findings, OrderProbe)
    assert isinstance(second.findings, OrderProbe)

    # Step "a" ran first: it could not yet see step "b"'s marker on disk.
    assert first.findings.step == "a"
    assert first.findings.saw_other is False
    # Step "b" ran second: step "a"'s marker was already there for it to see.
    assert second.findings.step == "b"
    assert second.findings.saw_other is True


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
            findings=ctx.intent.summary,
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
    assert intent_outcome.findings is intent

    # The later step never saw `intent_outcome` -- it read `ctx.intent.summary` directly.
    assert later_outcome.findings == intent_text


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
    assert isinstance(first_outcome.findings, OrderProbe)
    assert isinstance(second_outcome.findings, OrderProbe)
    assert first_outcome.findings.step == "a"
    assert second_outcome.findings.step == "b"
