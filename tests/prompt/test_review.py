"""Tests for the Review step's prompt-construction helpers (Milestone 5, issue #26):
`intent_conformance_clause` and `build_review_prompt`. Also covers the unconditional
suggestion-obligation clause added to both `build_review_prompt` and
`build_review_fix_prompt` by issue #76.

Moved here from `tests/steps/test_review.py` in a later structural refactor alongside the
functions themselves moving to `code_review.prompt.review`. `intent_conformance_clause`'s
tests are updated for its narrowed `source: str` signature (it no longer takes the whole
`Intent` object -- see the function's own docstring for why). Pure function tests -- no
agent, no subprocess.
"""

from __future__ import annotations

from pathlib import Path

from code_review.pipeline.step import FixRound, StepContext
from code_review.prompt.review import (
    build_review_fix_prompt,
    build_review_prompt,
    intent_conformance_clause,
)
from code_review.steps.intent import Intent

# --- intent_conformance_clause -----------------------------------------------------------


def test_intent_conformance_clause_is_empty_for_non_explicit_intent() -> None:
    assert intent_conformance_clause("claude") == ""


def test_intent_conformance_clause_is_present_for_explicit_intent() -> None:
    clause = intent_conformance_clause("explicit")

    assert clause != ""


def test_intent_conformance_clause_obligates_ask_user_on_required_criterion_removal() -> None:
    clause = intent_conformance_clause("explicit")

    assert "REQUIRED" in clause
    assert "ask-user" in clause


def test_intent_conformance_clause_obligates_ask_user_on_forbidden_behavior_addition() -> None:
    clause = intent_conformance_clause("explicit")

    assert "FORBIDDEN" in clause
    assert "ask-user" in clause


def test_intent_conformance_clause_applies_even_when_otherwise_risk_clean() -> None:
    clause = intent_conformance_clause("explicit")

    assert "risk-clean" in clause.lower()


# --- build_review_prompt ------------------------------------------------------------------


class _SpyAgent:
    async def run(self, opts: object) -> object:  # pragma: no cover - not exercised
        raise AssertionError("build_review_prompt must not call the agent")

    async def close(self) -> None:  # pragma: no cover - not exercised
        pass


def _ctx(
    intent: Intent,
    diff: str = "diff --git a/f b/f\n+hello\n",
    fix_round: FixRound | None = None,
) -> StepContext:
    return StepContext(  # type: ignore[arg-type]
        cwd=Path("."), agent=_SpyAgent(), diff=diff, intent=intent, fix_round=fix_round
    )


def test_build_review_prompt_includes_the_diff() -> None:
    intent = Intent(summary="add retry logic", source="explicit", score=1.0)

    prompt = build_review_prompt(_ctx(intent))

    assert "diff --git a/f b/f" in prompt
    assert "+hello" in prompt


def test_build_review_prompt_includes_the_wrapped_intent_block() -> None:
    intent = Intent(summary="use a queue, not polling", source="explicit", score=1.0)

    prompt = build_review_prompt(_ctx(intent))

    assert "-----BEGIN USER INTENT-----" in prompt
    assert "use a queue, not polling" in prompt


def test_build_review_prompt_appends_the_intent_conformance_clause_for_explicit_intent() -> None:
    intent = Intent(summary="use a queue, not polling", source="explicit", score=1.0)

    prompt = build_review_prompt(_ctx(intent))

    assert "Intent conformance is mandatory" in prompt


def test_build_review_prompt_omits_the_intent_conformance_clause_for_non_explicit_intent() -> None:
    intent = Intent(summary="use a queue, not polling", source="claude", score=0.4)

    prompt = build_review_prompt(_ctx(intent))

    assert "Intent conformance is mandatory" not in prompt


def test_build_review_prompt_puts_the_diff_before_the_intent_block() -> None:
    intent = Intent(summary="add retry logic", source="explicit", score=1.0)

    prompt = build_review_prompt(_ctx(intent))

    assert prompt.index("diff --git") < prompt.index("-----BEGIN USER INTENT-----")


def test_build_review_prompt_includes_the_suggestion_obligation_clause() -> None:
    intent = Intent(summary="add retry logic", source="explicit", score=1.0)

    prompt = build_review_prompt(_ctx(intent))

    assert "ask-user" in prompt
    assert "suggestions" in prompt


def test_build_review_prompt_includes_the_suggestion_clause_for_non_explicit_intent() -> None:
    intent = Intent(summary="add retry logic", source="claude", score=0.4)

    prompt = build_review_prompt(_ctx(intent))

    assert "concrete, actionable remediation options" in prompt


def test_build_review_fix_prompt_includes_the_suggestion_obligation_clause() -> None:
    intent = Intent(summary="add retry logic", source="explicit", score=1.0)
    fix_round = FixRound(instructions="do the thing")

    prompt = build_review_fix_prompt(_ctx(intent, fix_round=fix_round))

    assert "ask-user" in prompt
    assert "concrete, actionable remediation options" in prompt
