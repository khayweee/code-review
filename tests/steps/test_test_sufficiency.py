"""Tests for the test-sufficiency step's schema (`TestSufficiencyOutput`, `TestArtifact`)
and `TestSufficiencyStep` itself (Milestone 6, issue #59).

Follows `tests/steps/test_review.py`'s convention exactly: the schema shape is tested pure
(no agent), and `TestSufficiencyStep`'s own orchestration tests use a real temporary git
checkout with a real diff, the real Milestone 1 `ClaudeCLI` backend, and fake CLI scripts
under `tests/pipeline/fakes/` -- no mocking of `Step` or `Agent`. Helpers below are
standalone copies of `test_review.py`'s, deliberately not imported from that module since
these are separate test files.

The "Fix mode (issue #82)" section below mirrors `test_review.py`'s own "Fix mode (issue
#81)" section: `TestSufficiencyStep.supports_fix_round is True`, a real automatic fix round
driven through `run_steps`/`pipeline/executor.py` whose fix-mode prompt makes a genuine
on-disk edit and returns a fresh `TestSufficiencyOutput`, and the fail-safe-default
regression (an unset-`action` finding never reaches the automatic path) proven against a
real `TestSufficiencyStep` rather than `tests/pipeline/test_executor.py`'s synthetic step.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest
from pydantic import ValidationError

from code_review.agent import Agent, ClaudeCLI
from code_review.pipeline import (
    ApprovalResponse,
    Step,
    StepContext,
    StepEvent,
    StepOutcome,
    run_steps,
)
from code_review.steps.intent import Intent
from code_review.steps.test_sufficiency import TestSufficiencyOutput, TestSufficiencyStep
from code_review.tui.activity import ActivityEvent, ActivityRelay

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
AUTO_FIX_ROUND_FAKE_CLI = _FAKES / "test_sufficiency_output_auto_fix_round.py"
UNSET_ACTION_FAKE_CLI = _FAKES / "test_sufficiency_output_unset_action.py"
STREAMS_A_TOOL_CALL_FAKE_CLI = _FAKES / "test_sufficiency_streams_a_tool_call.py"

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


async def _approve(step_name: str, outcome: StepOutcome) -> ApprovalResponse:
    """A stub `on_approval_needed` (issue #80, updated for issue #81's `ApprovalResponse`)
    that always answers "approve" -- attached only by the one test below whose
    `TestSufficiencyStep` outcome parks (`needs_approval=True`), so `run_steps` doesn't
    fail closed (`executor.ApprovalNotAttachedError`) before this file's shared
    `_collect`/`_only_outcome` helpers can inspect that outcome. This file's own tests are
    about `TestSufficiencyStep`'s outcome construction, not about the park/approve/skip/
    fix/abort flow itself -- that is `tests/pipeline/test_executor.py`'s job (see that
    file's "The fix-round loop" section, in particular the `supports_fix_round=False`
    regression test, for the proof that `TestSufficiencyStep`-shaped outcomes are never
    bounced through an auto-fix round or parked on `auto_fixable` alone, issue #81's own
    acceptance criterion for this step)."""

    return ApprovalResponse(decision="approve")


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

    findings = outcome.payload
    assert isinstance(findings, TestSufficiencyOutput)
    assert len(findings.findings) == 1

    assert outcome.needs_approval is False


def test_test_sufficiency_step_needs_approval_on_an_ask_user_finding(tmp_path: Path) -> None:
    """Acceptance criterion: running `TestSufficiencyStep` against a fake agent returning a
    finding with `action="ask-user"` produces `StepOutcome(needs_approval=True, ...)`."""

    repo, diff = _real_repo_with_diff(tmp_path)
    agent: Agent = ClaudeCLI()
    ctx = StepContext(
        cwd=repo, agent=agent, diff=diff, intent=_EXPLICIT_INTENT, on_approval_needed=_approve
    )
    step: Step = TestSufficiencyStep(executable=BLOCKING_FAKE_CLI)

    outcome = _only_outcome(asyncio.run(_collect([step], ctx)))
    asyncio.run(agent.close())

    findings = outcome.payload
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


# --- Fix mode (issue #82) ------------------------------------------------------------------


def test_test_sufficiency_step_supports_fix_round_is_true() -> None:
    assert TestSufficiencyStep.supports_fix_round is True


def test_test_sufficiency_step_automatic_fix_round_writes_a_test_and_returns_a_fresh_output(
    tmp_path: Path,
) -> None:
    """End-to-end proof of issue #82's headline behavior, driven through the real
    `run_steps`/`pipeline/executor.py` round loop against a real `TestSufficiencyStep`: an
    outcome with one auto-fix finding and no ask-user finding triggers exactly one automatic
    re-run before any park (mirroring `tests/steps/test_review.py`'s equivalent proof for
    `ReviewStep`, issue #81), the fake agent's own new test file really lands on disk
    (proving `TestSufficiencyStep`'s fix-mode prompt, `build_test_sufficiency_fix_prompt`,
    grants real edit access rather than just re-requesting the same schema), the fix
    round's prompt actually carried the auto-fix finding's own description
    (`FixRound.instructions`, via `pipeline/findings.py`'s `describe_auto_fix_findings`),
    and the returned `TestSufficiencyOutput` from that round is a fresh verdict -- new
    findings/tested/testing_summary/artifacts -- not an echo of the finding that triggered
    it."""

    repo, diff = _real_repo_with_diff(tmp_path)
    written_test_file = repo / "test_greeting_written_by_fix_round.py"
    assert not written_test_file.exists()

    agent: Agent = ClaudeCLI()
    ctx = StepContext(cwd=repo, agent=agent, diff=diff, intent=_EXPLICIT_INTENT)
    step: Step = TestSufficiencyStep(executable=AUTO_FIX_ROUND_FAKE_CLI)

    events = asyncio.run(_collect([step], ctx))
    asyncio.run(agent.close())

    completed = [e for e in events if e.status == "completed"]
    assert len(completed) == 2  # exactly one automatic re-run, then the run reaches "clean"

    first_outcome, second_outcome = completed[0].outcome, completed[1].outcome
    assert first_outcome is not None
    assert second_outcome is not None

    assert first_outcome.auto_fixable is True
    assert first_outcome.needs_approval is False
    first_output = first_outcome.payload
    assert isinstance(first_output, TestSufficiencyOutput)
    assert len(first_output.findings) == 1
    assert first_output.testing_summary == "initial pass: no test found for the new greeting line"

    assert second_outcome.auto_fixable is False
    assert second_outcome.needs_approval is False
    second_output = second_outcome.payload
    assert isinstance(second_output, TestSufficiencyOutput)
    # A fresh verdict, not an echo of round 1's finding.
    assert second_output.findings == []
    assert second_output.tested == ["greeting message includes the new line"]
    assert second_output.testing_summary != first_output.testing_summary
    assert second_output.testing_summary.startswith("fix round: wrote missing test")
    # The fix-round prompt actually carried round 1's finding description forward.
    assert "saw_instructions=True" in second_output.testing_summary
    assert len(second_output.artifacts) == 1
    assert second_output.artifacts[0].kind == "written-test"

    # The fake agent's own new test file really landed on the working tree.
    assert written_test_file.exists()


def test_test_sufficiency_step_never_auto_fixes_a_finding_with_unset_action(
    tmp_path: Path,
) -> None:
    """Regression pinning the fail-safe default (`pipeline/findings.py`'s
    `action_or_default`) through the full executor loop for `TestSufficiencyStep`
    specifically (issue #82's own acceptance criterion), mirroring
    `tests/pipeline/test_executor.py`'s equivalent synthetic-step regression: a real
    `TestSufficiencyStep` whose fake CLI answer has a finding with no `action` set must
    never resolve to "auto-fix" -- it can only ever reach the park path, never the
    automatic fix-round path, even though this step has opted into fix rounds
    (`supports_fix_round=True`)."""

    repo, diff = _real_repo_with_diff(tmp_path)

    approved: list[StepOutcome] = []

    async def approve(step_name: str, outcome: StepOutcome) -> ApprovalResponse:
        approved.append(outcome)
        return ApprovalResponse(decision="approve")

    agent: Agent = ClaudeCLI()
    ctx = StepContext(
        cwd=repo, agent=agent, diff=diff, intent=_EXPLICIT_INTENT, on_approval_needed=approve
    )
    step: Step = TestSufficiencyStep(executable=UNSET_ACTION_FAKE_CLI)

    events = asyncio.run(_collect([step], ctx))
    asyncio.run(agent.close())

    completed = [e for e in events if e.status == "completed"]
    # Exactly one round: an unset-action finding resolves to "ask-user" (the fail-safe
    # default), never "auto-fix", so this parks on the very first round rather than
    # bouncing through any automatic re-run first.
    assert len(completed) == 1

    outcome = completed[0].outcome
    assert outcome is not None
    findings = outcome.payload
    assert isinstance(findings, TestSufficiencyOutput)
    assert len(findings.findings) == 1
    assert findings.findings[0].action is None

    assert outcome.needs_approval is True
    assert outcome.auto_fixable is False
    # The park path was actually reached -- proving this outcome never took the automatic
    # fix-round branch (which would never call `on_approval_needed` at all).
    assert len(approved) == 1
    assert approved[0] is outcome


# --- Activity reporting (shared tool_stream_relay) -----------------------------------------


async def _drain_activity_events(relay: ActivityRelay, count: int) -> list[ActivityEvent]:
    """Collect exactly `count` events off `relay`. A standalone copy of `tests/steps/
    test_review.py`'s helper of the same name -- deliberately not imported from that module
    (see this module's docstring)."""

    return [await relay.next_event() for _ in range(count)]


def test_test_sufficiency_step_streams_each_tool_call_as_a_nested_activity(
    tmp_path: Path,
) -> None:
    """`TestSufficiencyStep` gains the same tool-call visibility `ReviewStep` already has
    via the shared `tool_stream_relay` (`steps/tool_activity.py`), not a copy -- proven
    against `STREAMS_A_TOOL_CALL_FAKE_CLI`'s real stream-json transcript (one tool call,
    non-error result), mirroring `tests/steps/test_review.py`'s equivalent proof exactly.
    `run_steps` yields exactly one round here (a clean answer, no park), so this fixture's
    4 activity events -- Agent started, Tool started, Tool finished, Agent finished -- are
    the whole stream."""

    repo, diff = _real_repo_with_diff(tmp_path)
    agent: Agent = ClaudeCLI()
    relay = ActivityRelay()
    ctx = StepContext(
        cwd=repo, agent=agent, diff=diff, intent=_EXPLICIT_INTENT, activity_reporter=relay
    )
    step: Step = TestSufficiencyStep(executable=STREAMS_A_TOOL_CALL_FAKE_CLI)

    async def scenario() -> list[ActivityEvent]:
        drain_task = asyncio.ensure_future(_drain_activity_events(relay, 4))
        async for _event in run_steps([step], ctx):
            pass
        return await drain_task

    agent_started, tool_started, tool_finished, agent_finished = asyncio.run(scenario())
    asyncio.run(agent.close())

    assert agent_started.status == "started"
    assert agent_started.label == "Agent: assessing test sufficiency via claude"
    assert agent_started.parent_id is None

    assert tool_started.status == "started"
    assert tool_started.label == "Tool: Read(/fake/path.txt)"
    assert tool_started.parent_id == agent_started.activity_id

    assert tool_finished.status == "finished"
    assert tool_finished.activity_id == tool_started.activity_id

    assert agent_finished.status == "finished"
    assert agent_finished.activity_id == agent_started.activity_id
