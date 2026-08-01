"""Round-trip test for Step/StepContext/StepOutcome through the executor.

Proves the full path end to end: a real temporary git checkout with a real diff, a
minimal test `Step` that embeds that diff in a prompt and calls through the real
Milestone 1 `Agent` abstraction (`ClaudeCLI`) pointed at a fake CLI script, run through
`executor.run_step`, producing a `StepOutcome` this test asserts on.

No mocking of `Step` or `Agent` anywhere here -- `_ReviewStep` is a real, structurally-
typed `Step` implementation, and `ClaudeCLI` is the real Milestone 1 backend. See
`tests/agent/test_process_group.py` for why this repo goes through the real backend
rather than a mocked one: a mock can't prove what actually happens when the tool runs.
"""

from __future__ import annotations

import asyncio
import subprocess
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel

from code_review.agent import Agent, ClaudeCLI, RunOpts
from code_review.pipeline import Step, StepContext, StepOutcome, run_step

FAKE_CLI = Path(__file__).parent / "fakes" / "review_findings.py"


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

    outcome = asyncio.run(run_step(step, ctx))
    asyncio.run(agent.close())

    assert isinstance(outcome, StepOutcome)
    assert outcome.needs_approval is False
    assert outcome.auto_fixable is False
    findings = outcome.findings
    assert isinstance(findings, ReviewFindings)
    assert findings.saw_added_line is True
    expected_prompt = f"Review this diff:\n{diff}"
    assert findings.summary == f"reviewed a {len(expected_prompt)}-character prompt"
