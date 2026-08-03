"""Unit tests for `Intent` and `IntentStep` (Milestone 3, issues #18 and #19).

Pure-schema and step-orchestration tests only -- the sanitize-and-wrap prompt-construction
tests (`redact_secrets`/`strip_adversarial`/`wrap_intent`) moved to
`tests/prompt/test_intent.py` in a later structural refactor alongside the functions
themselves moving to `code_review.prompt.intent`. `IntentStep`'s tests (issue #19) use a
genuine hand-written `Agent`-protocol implementation as a spy, not a mock library
stand-in -- this repo's convention (see `tests/pipeline/test_executor.py`'s docstring) is
real `Step`/`Agent` implementations everywhere, including in tests.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

import pytest

from code_review.agent import RunOpts
from code_review.agent.base import OutputT, Result
from code_review.pipeline.step import StepContext, StepOutcome
from code_review.steps.intent import Intent, IntentStep


def test_intent_is_a_frozen_slotted_dataclass_with_expected_fields() -> None:
    intent = Intent(summary="add retry logic", source="explicit", score=0.9)

    assert intent.summary == "add retry logic"
    assert intent.source == "explicit"
    assert intent.score == 0.9
    assert intent.session_id is None


def test_intent_source_accepts_arbitrary_strings_not_a_closed_enum() -> None:
    # This milestone only ever produces "explicit", but a future milestone writes agent
    # names here -- the field must accept those without a schema change.
    intent = Intent(summary="inferred from transcript", source="claude", score=0.4)

    assert intent.source == "claude"


# --- IntentStep: proves ctx.intent is what the pipeline's first step carries ----------


@dataclass
class _SpyAgent:
    """A genuine hand-written `Agent`-protocol implementation, not a mock library
    stand-in (see module docstring): records whether `run` was ever invoked, and fails
    loudly if it was, since `IntentStep` must never call through the agent it's given."""

    run_called: bool = False

    async def run(self, opts: RunOpts[OutputT]) -> Result[OutputT]:
        self.run_called = True
        raise AssertionError("IntentStep must not call Agent.run")

    async def close(self) -> None:
        pass


def test_intent_step_never_invokes_the_agent_it_is_given() -> None:
    spy = _SpyAgent()
    intent = Intent(summary="add retry logic", source="explicit", score=1.0)
    ctx = StepContext(cwd=Path("."), agent=spy, diff="", intent=intent)

    asyncio.run(IntentStep().run(ctx))

    assert spy.run_called is False


def test_intent_step_returns_a_deterministic_outcome_carrying_ctx_intent() -> None:
    spy = _SpyAgent()
    intent = Intent(summary="add retry logic", source="explicit", score=1.0)
    ctx = StepContext(cwd=Path("."), agent=spy, diff="", intent=intent)

    outcome = asyncio.run(IntentStep().run(ctx))

    assert isinstance(outcome, StepOutcome)
    assert outcome.needs_approval is False
    assert outcome.auto_fixable is False
    assert outcome.findings is ctx.intent


def test_intent_step_raises_a_clear_error_if_ctx_intent_is_missing() -> None:
    """Defense in depth: the type system already makes `ctx.intent` required, but a
    caller that constructs a `StepContext` directly (bypassing that typing -- e.g. a test
    or a future caller) still gets a clear, actionable error instead of failing
    confusingly deep inside a later step that assumes `ctx.intent` is present."""

    spy = _SpyAgent()
    ctx = StepContext(cwd=Path("."), agent=spy, diff="", intent=None)  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="intent"):
        asyncio.run(IntentStep().run(ctx))
