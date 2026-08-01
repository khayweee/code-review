"""Round-trip and fixed-order tests for the executor (Milestone 2, issues #13 and #14).

Proves the full path end to end: a real temporary git checkout with a real diff, minimal
test `Step` implementations that embed that diff in a prompt and call through the real
Milestone 1 `Agent` abstraction (`ClaudeCLI`) pointed at fake CLI scripts, run through
`executor.run_steps`, producing `StepOutcome`s this test asserts on.

No mocking of `Step` or `Agent` anywhere here -- `_ReviewStep` and `_OrderStep` are real,
structurally-typed `Step` implementations, and `ClaudeCLI` is the real Milestone 1
backend. See `tests/agent/test_process_group.py` for why this repo goes through the real
backend rather than a mocked one: a mock can't prove what actually happens when the tool
runs.
"""

from __future__ import annotations

import asyncio
import subprocess
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel

from code_review.agent import Agent, ClaudeCLI, RunOpts
from code_review.pipeline import Step, StepContext, StepOutcome, run_steps

FAKE_CLI = Path(__file__).parent / "fakes" / "review_findings.py"
ORDER_FAKE_CLI_A = Path(__file__).parent / "fakes" / "order_step_a.py"
ORDER_FAKE_CLI_B = Path(__file__).parent / "fakes" / "order_step_b.py"


class ReviewFindings(BaseModel):
    """A stand-in schema for this slice -- Milestone 4 owns the real `Finding` schema."""

    summary: str
    saw_added_line: bool


@dataclass(frozen=True, slots=True)
class _ReviewStep:
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
class _OrderStep:
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
    ctx = StepContext(cwd=repo, agent=agent, diff=diff)
    step: Step = _ReviewStep()

    outcomes = asyncio.run(run_steps([step], ctx))
    asyncio.run(agent.close())

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
    ctx = StepContext(cwd=repo, agent=agent, diff=diff)
    steps: list[Step] = [_OrderStep(ORDER_FAKE_CLI_A), _OrderStep(ORDER_FAKE_CLI_B)]

    outcomes = asyncio.run(run_steps(steps, ctx))
    asyncio.run(agent.close())

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
