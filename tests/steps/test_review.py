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

from code_review.agent import Agent, ClaudeCLI
from code_review.pipeline import Step, StepContext, StepEvent, StepOutcome, run_steps
from code_review.steps.intent import Intent
from code_review.steps.review import ReviewOutput, ReviewStep

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
    blocking-findings gate did, and `StepOutcome.findings` carries the already-filtered
    `ReviewOutput`, not the raw agent answer."""

    repo, diff = _real_repo_with_diff(tmp_path)
    agent: Agent = ClaudeCLI()
    ctx = StepContext(cwd=repo, agent=agent, diff=diff, intent=_EXPLICIT_INTENT)
    step: Step = ReviewStep(executable=CLEAN_FAKE_CLI)

    outcome = _only_outcome(asyncio.run(_collect([step], ctx)))
    asyncio.run(agent.close())

    findings = outcome.findings
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
    ctx = StepContext(cwd=repo, agent=agent, diff=diff, intent=_EXPLICIT_INTENT)
    step: Step = ReviewStep(executable=BLOCKING_FAKE_CLI)

    outcome = _only_outcome(asyncio.run(_collect([step], ctx)))
    asyncio.run(agent.close())

    findings = outcome.findings
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

    findings = outcome.findings
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

    findings = outcome.findings
    assert isinstance(findings, ReviewOutput)
    assert findings.risk_rationale == "did not see intent-conformance clause"


def test_review_step_calls_agent_exactly_once(tmp_path: Path) -> None:
    """Issue #27: `run_steps` yields exactly one "running"/"completed" event pair for a
    single `ReviewStep`, and `ReviewStep.run` (see `steps/review.py`) contains exactly one
    `await ctx.agent.run(...)` call -- together, a single correct outcome from one
    fake-CLI invocation is sufficient proof there is no retry or re-review, without needing
    a fake CLI script rigged to fail on a second call."""

    repo, diff = _real_repo_with_diff(tmp_path)
    agent: Agent = ClaudeCLI()
    ctx = StepContext(cwd=repo, agent=agent, diff=diff, intent=_EXPLICIT_INTENT)
    step: Step = ReviewStep(executable=CLEAN_FAKE_CLI)

    events = asyncio.run(_collect([step], ctx))
    asyncio.run(agent.close())

    running = [e for e in events if e.status == "running"]
    completed = [e for e in events if e.status == "completed"]
    assert len(running) == 1
    assert len(completed) == 1
