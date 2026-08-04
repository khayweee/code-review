"""Tests for the test-sufficiency step's prompt-construction helpers: `build_test_sufficiency_
prompt` (Milestone 6, issue #59) and `build_test_sufficiency_fix_prompt` (Milestone 7, issue
#82). Pure function tests -- no agent, no subprocess, mirroring `tests/prompt/test_review.py`'s
structure and its `_SpyAgent` helper.
"""

from __future__ import annotations

from pathlib import Path

from code_review.pipeline.step import FixRound, StepContext
from code_review.prompt.test_sufficiency import (
    build_test_sufficiency_fix_prompt,
    build_test_sufficiency_prompt,
)
from code_review.steps.intent import Intent


class _SpyAgent:
    async def run(self, opts: object) -> object:  # pragma: no cover - not exercised
        raise AssertionError("build_test_sufficiency_prompt must not call the agent")

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


def test_build_test_sufficiency_prompt_includes_the_diff() -> None:
    intent = Intent(summary="add retry logic", source="explicit", score=1.0)

    prompt = build_test_sufficiency_prompt(_ctx(intent))

    assert "diff --git a/f b/f" in prompt
    assert "+hello" in prompt


def test_build_test_sufficiency_prompt_includes_the_wrapped_intent_block() -> None:
    intent = Intent(summary="use a queue, not polling", source="explicit", score=1.0)

    prompt = build_test_sufficiency_prompt(_ctx(intent))

    assert "-----BEGIN USER INTENT-----" in prompt
    assert "use a queue, not polling" in prompt


def test_build_test_sufficiency_prompt_contains_the_decision_ladder_rungs() -> None:
    intent = Intent(summary="add retry logic", source="explicit", score=1.0)

    prompt = build_test_sufficiency_prompt(_ctx(intent))

    assert "existing test" in prompt
    assert "manual verification" in prompt
    assert "unverified" in prompt


def test_build_test_sufficiency_prompt_contains_the_not_sufficient_evidence_clause() -> None:
    intent = Intent(summary="add retry logic", source="explicit", score=1.0)

    prompt = build_test_sufficiency_prompt(_ctx(intent))

    assert "unit tests passing is not sufficient evidence by itself" in prompt.lower()


def test_build_test_sufficiency_prompt_contains_the_complete_suite_prohibition() -> None:
    intent = Intent(summary="add retry logic", source="explicit", score=1.0)

    prompt = build_test_sufficiency_prompt(_ctx(intent))

    lowered = prompt.lower()
    assert "entire test suite" in lowered or "whole test suite" in lowered
    assert "not permission to run nothing" in lowered


def test_build_test_sufficiency_prompt_contains_the_test_quality_rule() -> None:
    intent = Intent(summary="add retry logic", source="explicit", score=1.0)

    prompt = build_test_sufficiency_prompt(_ctx(intent))

    assert "grep" in prompt.lower()


def test_build_test_sufficiency_prompt_puts_the_diff_before_the_intent_block() -> None:
    intent = Intent(summary="add retry logic", source="explicit", score=1.0)

    prompt = build_test_sufficiency_prompt(_ctx(intent))

    assert prompt.index("diff --git") < prompt.index("-----BEGIN USER INTENT-----")


def test_build_test_sufficiency_prompt_contains_the_suggestion_obligation_clause() -> None:
    intent = Intent(summary="add retry logic", source="explicit", score=1.0)

    prompt = build_test_sufficiency_prompt(_ctx(intent))

    assert "ask-user" in prompt
    assert "concrete, actionable remediation options" in prompt


# --- build_test_sufficiency_fix_prompt (issue #82) -----------------------------------------


def test_build_test_sufficiency_fix_prompt_includes_the_fix_round_instructions() -> None:
    intent = Intent(summary="add retry logic", source="explicit", score=1.0)
    fix_round = FixRound(instructions="- [warning] write a test for the retry path")

    prompt = build_test_sufficiency_fix_prompt(_ctx(intent, fix_round=fix_round))

    assert "write a test for the retry path" in prompt


def test_build_test_sufficiency_fix_prompt_includes_the_stale_diff_warning() -> None:
    intent = Intent(summary="add retry logic", source="explicit", score=1.0)
    fix_round = FixRound(instructions="do the thing")

    prompt = build_test_sufficiency_fix_prompt(_ctx(intent, fix_round=fix_round))

    assert "ORIGINAL test-sufficiency assessment" in prompt
    assert "re-inspect the live working tree" in prompt


def test_build_test_sufficiency_fix_prompt_includes_the_diff() -> None:
    intent = Intent(summary="add retry logic", source="explicit", score=1.0)
    fix_round = FixRound(instructions="do the thing")

    prompt = build_test_sufficiency_fix_prompt(_ctx(intent, fix_round=fix_round))

    assert "diff --git a/f b/f" in prompt
    assert "+hello" in prompt


def test_build_test_sufficiency_fix_prompt_includes_the_wrapped_intent_block() -> None:
    intent = Intent(summary="use a queue, not polling", source="explicit", score=1.0)
    fix_round = FixRound(instructions="do the thing")

    prompt = build_test_sufficiency_fix_prompt(_ctx(intent, fix_round=fix_round))

    assert "-----BEGIN USER INTENT-----" in prompt
    assert "use a queue, not polling" in prompt


def test_build_test_sufficiency_fix_prompt_contains_all_five_guardrail_clauses() -> None:
    intent = Intent(summary="add retry logic", source="explicit", score=1.0)
    fix_round = FixRound(instructions="do the thing")

    prompt = build_test_sufficiency_fix_prompt(_ctx(intent, fix_round=fix_round))
    lowered = prompt.lower()

    assert "existing test" in prompt
    assert "manual verification" in prompt
    assert "unverified" in prompt
    assert "unit tests passing is not sufficient evidence by itself" in lowered
    assert "not permission to run nothing" in lowered
    assert "grep" in lowered
    assert "concrete, actionable remediation options" in lowered


def test_build_test_sufficiency_fix_prompt_instructs_a_fresh_reassessment_not_an_echo() -> None:
    intent = Intent(summary="add retry logic", source="explicit", score=1.0)
    fix_round = FixRound(instructions="do the thing")

    prompt = build_test_sufficiency_fix_prompt(_ctx(intent, fix_round=fix_round))

    assert "re-run your own test-sufficiency assessment" in prompt.lower()
    assert "not what you must report back" in prompt.lower()


def test_build_test_sufficiency_fix_prompt_puts_the_fix_instruction_before_the_diff() -> None:
    intent = Intent(summary="add retry logic", source="explicit", score=1.0)
    fix_round = FixRound(instructions="do the thing")

    prompt = build_test_sufficiency_fix_prompt(_ctx(intent, fix_round=fix_round))

    assert prompt.index("You are running a fix round") < prompt.index("diff --git")


def test_build_test_sufficiency_fix_prompt_asserts_when_fix_round_is_none() -> None:
    intent = Intent(summary="add retry logic", source="explicit", score=1.0)

    try:
        build_test_sufficiency_fix_prompt(_ctx(intent, fix_round=None))
    except AssertionError:
        pass
    else:  # pragma: no cover - defensive
        raise AssertionError("expected build_test_sufficiency_fix_prompt to assert")
