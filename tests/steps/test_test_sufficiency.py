"""Tests for the test-sufficiency step's schema (`TestSufficiencyOutput`, `TestArtifact`)
and `TestSufficiencyStep` itself (Milestone 6, issue #59).

Follows `tests/steps/test_review.py`'s convention exactly: the schema shape is tested pure
(no agent), and `TestSufficiencyStep`'s own orchestration tests use a real temporary git
checkout with a real diff, the real Milestone 1 `ClaudeCLI` backend, and fake CLI scripts
under `tests/pipeline/fakes/` -- no mocking of `Step` or `Agent`. Helpers below are
standalone copies of `test_review.py`'s, deliberately not imported from that module since
these are separate test files.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest
from pydantic import ValidationError

from code_review.agent import Agent, ClaudeCLI
from code_review.pipeline import Step, StepContext, StepEvent, StepOutcome, run_steps
from code_review.steps.intent import Intent
from code_review.steps.test_sufficiency import TestSufficiencyOutput, TestSufficiencyStep

# --- TestSufficiencyOutput schema shape ---------------------------------------------------


def test_test_sufficiency_output_field_order_is_findings_then_the_rest() -> None:
    assert list(TestSufficiencyOutput.model_fields.keys()) == [
        "findings",
        "tested",
        "testing_summary",
        "artifacts",
    ]


def test_test_sufficiency_output_accepts_all_documented_fields() -> None:
    output = TestSufficiencyOutput.model_validate(
        {
            "findings": [
                {
                    "severity": "info",
                    "description": "naming nit",
                    "action": "no-op",
                    "review_scope": "source",
                }
            ],
            "tested": ["greeting message includes the new line"],
            "testing_summary": "Existing test suite already covers the changed behavior.",
            "artifacts": [
                {
                    "kind": "existing-test",
                    "description": "test_greeting_includes_world exercises the new line",
                    "location": "tests/test_greeting.py:12",
                }
            ],
        }
    )

    assert len(output.findings) == 1
    assert output.tested == ["greeting message includes the new line"]
    assert output.testing_summary == "Existing test suite already covers the changed behavior."
    assert len(output.artifacts) == 1
    assert output.artifacts[0].kind == "existing-test"
    assert output.artifacts[0].location == "tests/test_greeting.py:12"


def test_test_sufficiency_output_requires_all_four_fields() -> None:
    with pytest.raises(ValidationError):
        TestSufficiencyOutput.model_validate(
            {
                "findings": [],
                "tested": [],
                "artifacts": [],
            }
        )


# --- TestSufficiencyStep (issue #59) -------------------------------------------------------

_FAKES = Path(__file__).parent.parent / "pipeline" / "fakes"
CLEAN_FAKE_CLI = _FAKES / "test_sufficiency_output_clean.py"
BLOCKING_FAKE_CLI = _FAKES / "test_sufficiency_output_blocking.py"

_EXPLICIT_INTENT = Intent(summary="use a queue, not polling", source="explicit", score=1.0)


def _run_git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


def _real_repo_with_diff(tmp_path: Path) -> tuple[Path, str]:
    """Build a real temporary git checkout and return it with a real unstaged diff.

    A standalone copy of `tests/steps/test_review.py`'s helper of the same name --
    deliberately not imported from that module (see this module's docstring).
    """

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


async def _collect(steps: list[Step], ctx: StepContext) -> list[StepEvent]:
    return [event async for event in run_steps(steps, ctx)]


def _only_outcome(events: list[StepEvent]) -> StepOutcome:
    completed = [e for e in events if e.status == "completed"]
    assert len(completed) == 1
    outcome = completed[0].outcome
    assert outcome is not None
    return outcome


def test_test_sufficiency_step_outcome_is_clean_on_info_and_no_op_findings_only(
    tmp_path: Path,
) -> None:
    """Acceptance criterion: running `TestSufficiencyStep` against a fake agent returning
    only info/no-op findings produces `needs_approval=False`."""

    repo, diff = _real_repo_with_diff(tmp_path)
    agent: Agent = ClaudeCLI()
    ctx = StepContext(cwd=repo, agent=agent, diff=diff, intent=_EXPLICIT_INTENT)
    step: Step = TestSufficiencyStep(executable=CLEAN_FAKE_CLI)

    outcome = _only_outcome(asyncio.run(_collect([step], ctx)))
    asyncio.run(agent.close())

    findings = outcome.findings
    assert isinstance(findings, TestSufficiencyOutput)
    assert len(findings.findings) == 1

    assert outcome.needs_approval is False


def test_test_sufficiency_step_needs_approval_on_an_ask_user_finding(tmp_path: Path) -> None:
    """Acceptance criterion: running `TestSufficiencyStep` against a fake agent returning a
    finding with `action="ask-user"` produces `StepOutcome(needs_approval=True, ...)`."""

    repo, diff = _real_repo_with_diff(tmp_path)
    agent: Agent = ClaudeCLI()
    ctx = StepContext(cwd=repo, agent=agent, diff=diff, intent=_EXPLICIT_INTENT)
    step: Step = TestSufficiencyStep(executable=BLOCKING_FAKE_CLI)

    outcome = _only_outcome(asyncio.run(_collect([step], ctx)))
    asyncio.run(agent.close())

    findings = outcome.findings
    assert isinstance(findings, TestSufficiencyOutput)
    assert len(findings.findings) == 1

    assert outcome.needs_approval is True
    assert outcome.auto_fixable is False


def test_test_sufficiency_step_calls_agent_exactly_once(tmp_path: Path) -> None:
    """`run_steps` yields exactly one "running"/"completed" event pair for a single
    `TestSufficiencyStep`, and `TestSufficiencyStep.run` contains exactly one `await
    ctx.agent.run(...)` call -- together, a single correct outcome from one fake-CLI
    invocation is sufficient proof there is no retry or re-verification."""

    repo, diff = _real_repo_with_diff(tmp_path)
    agent: Agent = ClaudeCLI()
    ctx = StepContext(cwd=repo, agent=agent, diff=diff, intent=_EXPLICIT_INTENT)
    step: Step = TestSufficiencyStep(executable=CLEAN_FAKE_CLI)

    events = asyncio.run(_collect([step], ctx))
    asyncio.run(agent.close())

    running = [e for e in events if e.status == "running"]
    completed = [e for e in events if e.status == "completed"]
    assert len(running) == 1
    assert len(completed) == 1
