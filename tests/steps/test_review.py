"""Tests for the Review step's schema (`ReviewOutput`, Milestone 5, issue #26) and
`ReviewStep` itself (issue #27).

The pure `intent_conformance_clause`/prompt-assembly tests moved to
`tests/prompt/test_review.py`, and the deterministic pipeline-owned-delivery scope filter's
tests moved to `tests/pipeline/test_findings.py`, in a later structural refactor alongside
the functions themselves moving out of this module. What remains here is the schema shape
(pure, no agent) and `ReviewStep`'s own orchestration tests, which follow
`tests/pipeline/test_executor.py`'s convention: a real temporary git checkout with a real
diff, the real Milestone 1 `ClaudeCLI` backend, and fake CLI scripts under
`tests/pipeline/fakes/` -- no mocking of `Step` or `Agent`.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from code_review.agent import Agent, ClaudeCLI, ProcessExitError
from code_review.pipeline import (
    ApprovalResponse,
    Step,
    StepContext,
    StepEvent,
    StepOutcome,
    run_steps,
)
from code_review.steps.intent import Intent
from code_review.steps.review import ReviewOutput, ReviewStep
from code_review.tui.activity import ActivityEvent, ActivityRelay

# --- ReviewOutput schema shape -----------------------------------------------------------


def test_review_output_field_order_is_findings_then_risk_fields() -> None:
    # Pins the reference implementation's chain-of-thought ordering (see the schema's
    # docstring): a reordering here would silently change what an agent is asked to
    # reason about first.
    assert list(ReviewOutput.model_fields.keys()) == [
        "findings",
        "risk_level",
        "risk_rationale",
        "risk_scope",
    ]


def test_review_output_accepts_all_documented_fields() -> None:
    output = ReviewOutput.model_validate(
        {
            "findings": [
                {
                    "severity": "warning",
                    "description": "missing null check",
                    "action": "ask-user",
                    "review_scope": "source",
                }
            ],
            "risk_level": "medium",
            "risk_rationale": "touches error handling on a hot path",
            "risk_scope": "source-or-external",
        }
    )

    assert len(output.findings) == 1
    assert output.risk_level == "medium"
    assert output.risk_rationale == "touches error handling on a hot path"
    assert output.risk_scope == "source-or-external"


def test_review_output_risk_scope_is_optional_and_defaults_to_none() -> None:
    output = ReviewOutput.model_validate(
        {"findings": [], "risk_level": "low", "risk_rationale": "no issues found"}
    )

    assert output.risk_scope is None


def test_review_output_requires_risk_level_and_risk_rationale() -> None:
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ReviewOutput.model_validate({"findings": []})


# --- ReviewStep (issue #27) ---------------------------------------------------------------

_FAKES = Path(__file__).parent.parent / "pipeline" / "fakes"
CLEAN_FAKE_CLI = _FAKES / "review_output_clean.py"
BLOCKING_FAKE_CLI = _FAKES / "review_output_blocking.py"
PROMPT_PROBE_FAKE_CLI = _FAKES / "review_prompt_probe.py"
AUTO_FIX_ROUND_FAKE_CLI = _FAKES / "review_output_auto_fix_round.py"
STREAMS_A_TOOL_CALL_FAKE_CLI = _FAKES / "review_streams_a_tool_call.py"
# Reused directly from `tests/agent/` rather than copied into `pipeline/fakes/` -- it is a
# generic "start, then exit non-zero" double with no `ReviewOutput`-specific behavior, and
# `tests/agent/test_claude_cli.py`'s own `test_nonzero_exit_raises_process_exit_error_with_
# context` already proves what it does at the `ClaudeCLI` layer; this module only needs it
# to exercise `ReviewStep`'s activity-reporting exit path (issue #65).
NONZERO_EXIT_FAKE_CLI = Path(__file__).parent.parent / "agent" / "fakes" / "nonzero_exit.py"

_EXPLICIT_INTENT = Intent(summary="use a queue, not polling", source="explicit", score=1.0)
_INFERRED_INTENT = Intent(summary="use a queue, not polling", source="claude", score=0.4)


def _run_git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


def _real_repo_with_diff(tmp_path: Path) -> tuple[Path, str]:
    """Build a real temporary git checkout and return it with a real unstaged diff.

    A standalone copy of `tests/pipeline/test_executor.py`'s helper of the same name --
    deliberately not imported from that test module, since these are separate test files
    (see this module's docstring).
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
    that always answers "approve" -- attached only by the one test below whose `ReviewStep`
    outcome parks (`needs_approval=True`), so `run_steps` doesn't fail closed (`executor.
    ApprovalNotAttachedError`) before this file's shared `_collect`/`_only_outcome` helpers
    can inspect that outcome. This file's own tests are about `ReviewStep`'s outcome
    construction, not about the park/approve/skip/fix/abort flow itself -- that is `tests/
    pipeline/test_executor.py`'s job."""

    return ApprovalResponse(decision="approve")


def _only_outcome(events: list[StepEvent]) -> StepOutcome:
    completed = [e for e in events if e.status == "completed"]
    assert len(completed) == 1
    outcome = completed[0].outcome
    assert outcome is not None
    return outcome


def test_review_step_outcome_is_clean_and_auto_fixable_after_scope_filtering(
    tmp_path: Path,
) -> None:
    """End-to-end (issue #27): the fake CLI's answer has one "pipeline-owned-delivery"
    "ask-user" finding alongside "source"-scoped "no-op"/"auto-fix" findings. Without
    filtering, the "ask-user" finding would make this outcome need approval; proving
    `needs_approval` comes back `False` here proves the scope filter ran before the
    blocking-findings gate did, and `StepOutcome.payload` carries the already-filtered
    `ReviewOutput`, not the raw agent answer.

    Calls `step.run(ctx)` directly, not via `run_steps`/`_collect`: this outcome is
    genuinely `auto_fixable=True` (issue #27's own point), and since #81 `ReviewStep`
    (`supports_fix_round=True`) now gets automatically re-run by `pipeline/executor.py`'s
    round loop whenever `run_steps` sees that -- this test is about `ReviewStep.run`'s own
    single-round output shape, not the round loop (that belongs to
    `tests/pipeline/test_executor.py` and this file's own "Fix mode (issue #81)" section
    below), so it deliberately bypasses `run_steps` to stay decoupled from it."""

    repo, diff = _real_repo_with_diff(tmp_path)
    agent: Agent = ClaudeCLI()
    ctx = StepContext(cwd=repo, agent=agent, diff=diff, intent=_EXPLICIT_INTENT)
    step: Step = ReviewStep(executable=CLEAN_FAKE_CLI)

    outcome = asyncio.run(step.run(ctx))
    asyncio.run(agent.close())

    findings = outcome.payload
    assert isinstance(findings, ReviewOutput)
    # The pipeline-owned-delivery finding was stripped by the scope filter.
    assert len(findings.findings) == 2
    assert all(f.review_scope != "pipeline-owned-delivery" for f in findings.findings)

    assert outcome.needs_approval is False
    assert outcome.auto_fixable is True


def test_review_step_needs_approval_and_is_not_auto_fixable_on_an_ask_user_finding(
    tmp_path: Path,
) -> None:
    """End-to-end (issue #27): a surviving "source"-scoped "ask-user" finding alongside an
    "auto-fix" finding must block (`needs_approval=True`) and must NOT be reported
    auto-fixable -- `auto_fixable` requires no surviving finding to resolve to "ask-user",
    even when another finding resolves to "auto-fix"."""

    repo, diff = _real_repo_with_diff(tmp_path)
    agent: Agent = ClaudeCLI()
    ctx = StepContext(
        cwd=repo, agent=agent, diff=diff, intent=_EXPLICIT_INTENT, on_approval_needed=_approve
    )
    step: Step = ReviewStep(executable=BLOCKING_FAKE_CLI)

    outcome = _only_outcome(asyncio.run(_collect([step], ctx)))
    asyncio.run(agent.close())

    findings = outcome.payload
    assert isinstance(findings, ReviewOutput)
    assert len(findings.findings) == 2

    assert outcome.needs_approval is True
    assert outcome.auto_fixable is False


def test_review_step_prompt_includes_intent_conformance_clause_for_explicit_intent(
    tmp_path: Path,
) -> None:
    """Issue #27: `ReviewStep.run` appends `intent_conformance_clause(ctx.intent.source)`
    to its prompt when `ctx.intent.source == "explicit"` -- proven by the fake CLI echoing
    back whether it saw the clause's distinctive opening sentence."""

    repo, diff = _real_repo_with_diff(tmp_path)
    agent: Agent = ClaudeCLI()
    ctx = StepContext(cwd=repo, agent=agent, diff=diff, intent=_EXPLICIT_INTENT)
    step: Step = ReviewStep(executable=PROMPT_PROBE_FAKE_CLI)

    outcome = _only_outcome(asyncio.run(_collect([step], ctx)))
    asyncio.run(agent.close())

    findings = outcome.payload
    assert isinstance(findings, ReviewOutput)
    assert findings.risk_rationale == "saw intent-conformance clause"


def test_review_step_prompt_omits_intent_conformance_clause_for_non_explicit_intent(
    tmp_path: Path,
) -> None:
    """Issue #27: the same clause must NOT appear when `ctx.intent.source` is not
    "explicit" -- mirroring `intent_conformance_clause`'s own provenance rule (see
    `code_review.prompt.review`)."""

    repo, diff = _real_repo_with_diff(tmp_path)
    agent: Agent = ClaudeCLI()
    ctx = StepContext(cwd=repo, agent=agent, diff=diff, intent=_INFERRED_INTENT)
    step: Step = ReviewStep(executable=PROMPT_PROBE_FAKE_CLI)

    outcome = _only_outcome(asyncio.run(_collect([step], ctx)))
    asyncio.run(agent.close())

    findings = outcome.payload
    assert isinstance(findings, ReviewOutput)
    assert findings.risk_rationale == "did not see intent-conformance clause"


def test_review_step_calls_agent_exactly_once_per_round(tmp_path: Path) -> None:
    """Issue #27: `run_steps` yields exactly one "running"/"completed" event pair for a
    single `ReviewStep` round, and `ReviewStep.run` (see `steps/review.py`) contains
    exactly one `await ctx.agent.run(...)` call -- together, a single correct outcome from
    one fake-CLI invocation is sufficient proof there is no retry or re-review *within one
    round*, without needing a fake CLI script rigged to fail on a second call.

    Uses `PROMPT_PROBE_FAKE_CLI` (empty findings, so `auto_fixable=False`), not
    `CLEAN_FAKE_CLI` -- since issue #81, `ReviewStep`'s `supports_fix_round=True` means a
    genuinely `auto_fixable=True` outcome (what `CLEAN_FAKE_CLI` returns) gets
    automatically re-run by `pipeline/executor.py`'s round loop, which is a *different*,
    intentional multi-round behavior this test is not about -- see this file's own "Fix
    mode (issue #81)" section for that behavior's own test."""

    repo, diff = _real_repo_with_diff(tmp_path)
    agent: Agent = ClaudeCLI()
    ctx = StepContext(cwd=repo, agent=agent, diff=diff, intent=_EXPLICIT_INTENT)
    step: Step = ReviewStep(executable=PROMPT_PROBE_FAKE_CLI)

    events = asyncio.run(_collect([step], ctx))
    asyncio.run(agent.close())

    running = [e for e in events if e.status == "running"]
    completed = [e for e in events if e.status == "completed"]
    assert len(running) == 1
    assert len(completed) == 1
    outcome = completed[0].outcome
    assert outcome is not None
    assert outcome.auto_fixable is False  # sanity: no round loop was even eligible to fire


# --- Fix mode (issue #81) -----------------------------------------------------------------


def test_review_step_supports_fix_round_is_true() -> None:
    assert ReviewStep.supports_fix_round is True


def test_review_step_automatic_fix_round_edits_the_tree_and_returns_a_fresh_review_output(
    tmp_path: Path,
) -> None:
    """End-to-end proof of issue #81's headline behavior, driven through the real
    `run_steps`/`pipeline/executor.py` round loop against a real `ReviewStep`: an outcome
    with one auto-fix finding and no ask-user finding triggers exactly one automatic
    re-run before any park, the fake agent's own edit to the working tree really lands on
    disk (proving `ReviewStep`'s fix-mode prompt, `build_review_fix_prompt`, grants real
    edit access rather than just re-requesting the same schema), the fix round's prompt
    actually carried the auto-fix finding's own description (`FixRound.instructions`, via
    `pipeline/findings.py`'s `describe_auto_fix_findings`), and the returned `ReviewOutput`
    from that round is a fresh verdict -- new findings, new risk_level/risk_rationale --
    not an echo of the finding that triggered it."""

    repo, diff = _real_repo_with_diff(tmp_path)
    original_greeting = (repo / "greeting.txt").read_text()

    agent: Agent = ClaudeCLI()
    ctx = StepContext(cwd=repo, agent=agent, diff=diff, intent=_EXPLICIT_INTENT)
    step: Step = ReviewStep(executable=AUTO_FIX_ROUND_FAKE_CLI)

    events = asyncio.run(_collect([step], ctx))
    asyncio.run(agent.close())

    completed = [e for e in events if e.status == "completed"]
    assert len(completed) == 2  # exactly one automatic re-run, then the run reaches "clean"

    first_outcome, second_outcome = completed[0].outcome, completed[1].outcome
    assert first_outcome is not None
    assert second_outcome is not None

    assert first_outcome.auto_fixable is True
    assert first_outcome.needs_approval is False
    first_findings = first_outcome.payload
    assert isinstance(first_findings, ReviewOutput)
    assert len(first_findings.findings) == 1
    assert first_findings.risk_rationale == "initial pass: one auto-fixable style finding"

    assert second_outcome.auto_fixable is False
    assert second_outcome.needs_approval is False
    second_findings = second_outcome.payload
    assert isinstance(second_findings, ReviewOutput)
    # A fresh verdict, not an echo of round 1's finding.
    assert second_findings.findings == []
    assert second_findings.risk_rationale != first_findings.risk_rationale
    assert second_findings.risk_rationale.startswith("fix round: clean after edits")
    # The fix-round prompt actually carried round 1's finding description forward.
    assert "saw_instructions=True" in second_findings.risk_rationale

    # The fake agent's own edit really landed on the working tree.
    updated_greeting = (repo / "greeting.txt").read_text()
    assert updated_greeting != original_greeting
    assert updated_greeting.endswith("fixed\n")


# --- Activity reporting (issue #65) --------------------------------------------------------


async def _drain_activity_events(relay: ActivityRelay, count: int) -> list[ActivityEvent]:
    """Collect exactly `count` events off `relay`, mirroring `ReviewApp`'s own activity
    worker (`tui/app.py`'s `_consume_activities`, wired to `ActivityRelay.next_event()`
    in a background task) -- see `tui/activity.py`'s module docstring, "Consuming side".
    Started as a background task *before* the steps run, since `next_event()` blocks until
    an event is queued and `ReviewStep.run` reports its span synchronously within the same
    event loop."""

    return [await relay.next_event() for _ in range(count)]


def test_review_step_reports_exactly_one_activity_span_for_the_agent_call(
    tmp_path: Path,
) -> None:
    """Issue #65's first acceptance criterion: `ReviewStep.run` wraps its one
    `ctx.agent.run` call in `ctx.report_activity(...)`, producing exactly one activity
    ("started" then "finished") spanning the call, with a real elapsed duration on the
    "finished" event -- proven with a real `StepContext.activity_reporter` (a real
    `ActivityRelay`, satisfying `pipeline/step.py`'s `ActivityReporter` Protocol purely
    structurally, per that module's own design note), not a mock.

    Uses `PROMPT_PROBE_FAKE_CLI`, not `CLEAN_FAKE_CLI` -- for the same reason
    `test_review_step_calls_agent_exactly_once_per_round` above does (issue #81:
    `CLEAN_FAKE_CLI`'s outcome is genuinely `auto_fixable=True`, which would trigger a
    second, automatic round -- and so a second activity span -- that this single-round test
    is not about)."""

    repo, diff = _real_repo_with_diff(tmp_path)
    agent: Agent = ClaudeCLI()
    relay = ActivityRelay()
    ctx = StepContext(
        cwd=repo, agent=agent, diff=diff, intent=_EXPLICIT_INTENT, activity_reporter=relay
    )
    step: Step = ReviewStep(executable=PROMPT_PROBE_FAKE_CLI)

    async def scenario() -> list[ActivityEvent]:
        drain_task = asyncio.ensure_future(_drain_activity_events(relay, 2))
        async for _event in run_steps([step], ctx):
            pass
        return await drain_task

    started, finished = asyncio.run(scenario())
    asyncio.run(agent.close())

    assert started.status == "started"
    assert started.label == "Agent: reviewing diff via claude"
    assert started.parent_id is None

    assert finished.status == "finished"
    assert finished.label == started.label
    # Same span, not a fresh one -- the "finished" event closes the exact activity the
    # "started" event opened.
    assert finished.activity_id == started.activity_id
    # A real elapsed duration: monotonic time only ever moves forward, so a genuine call
    # (however fast) leaves the "finished" timestamp no earlier than the "started" one.
    assert finished.timestamp >= started.timestamp


def test_review_step_still_finishes_its_activity_span_when_the_agent_call_raises(
    tmp_path: Path,
) -> None:
    """Issue #65's second acceptance criterion: the same shape must hold whether the call
    succeeds or raises -- a failed agent call still leaves its activity line showing a
    duration, not stuck mid-tick, because `ctx.report_activity`'s `async with` block
    finishes the activity on any exit path (see `ActivityRelay.activity`'s `finally`).
    `NONZERO_EXIT_FAKE_CLI` makes `ctx.agent.run` raise `ProcessExitError` -- proving this
    against a real failure from the real `ClaudeCLI` backend, not a stand-in exception."""

    repo, diff = _real_repo_with_diff(tmp_path)
    agent: Agent = ClaudeCLI()
    relay = ActivityRelay()
    ctx = StepContext(
        cwd=repo, agent=agent, diff=diff, intent=_EXPLICIT_INTENT, activity_reporter=relay
    )
    step: Step = ReviewStep(executable=NONZERO_EXIT_FAKE_CLI)

    async def scenario() -> list[ActivityEvent]:
        drain_task = asyncio.ensure_future(_drain_activity_events(relay, 2))
        with pytest.raises(ProcessExitError):
            async for _event in run_steps([step], ctx):
                pass
        return await drain_task

    started, finished = asyncio.run(scenario())
    asyncio.run(agent.close())

    assert started.status == "started"
    assert started.label == "Agent: reviewing diff via claude"

    assert finished.status == "finished"
    assert finished.activity_id == started.activity_id
    assert finished.timestamp >= started.timestamp


def test_review_step_streams_each_tool_call_as_a_nested_activity(tmp_path: Path) -> None:
    """`_tool_stream_relay` (`steps/review.py`) turns each `TOOL_USE`/`TOOL_RESULT` pair
    from a real streaming `ClaudeCLI` call into its own nested activity span, opened on
    `TOOL_USE` and closed on its matching `TOOL_RESULT` -- proven against
    `STREAMS_A_TOOL_CALL_FAKE_CLI`'s real stream-json transcript (one tool call), not a
    stand-in `Agent`. `run_steps` yields exactly one round here (a clean answer, no
    park), so this fixture's 4 activity events -- Agent started, Tool started, Tool
    finished, Agent finished -- are the whole stream."""

    repo, diff = _real_repo_with_diff(tmp_path)
    agent: Agent = ClaudeCLI()
    relay = ActivityRelay()
    ctx = StepContext(
        cwd=repo, agent=agent, diff=diff, intent=_EXPLICIT_INTENT, activity_reporter=relay
    )
    step: Step = ReviewStep(executable=STREAMS_A_TOOL_CALL_FAKE_CLI)

    async def scenario() -> list[ActivityEvent]:
        drain_task = asyncio.ensure_future(_drain_activity_events(relay, 4))
        async for _event in run_steps([step], ctx):
            pass
        return await drain_task

    agent_started, tool_started, tool_finished, agent_finished = asyncio.run(scenario())
    asyncio.run(agent.close())

    assert agent_started.status == "started"
    assert agent_started.label == "Agent: reviewing diff via claude"
    assert agent_started.parent_id is None

    assert tool_started.status == "started"
    assert tool_started.label == "Tool: Read(/fake/path.txt)"
    # Nested inside the agent-call span -- the whole point of `_tool_stream_relay`.
    assert tool_started.parent_id == agent_started.activity_id

    assert tool_finished.status == "finished"
    assert tool_finished.activity_id == tool_started.activity_id
    assert tool_finished.label == tool_started.label

    assert agent_finished.status == "finished"
    assert agent_finished.activity_id == agent_started.activity_id
