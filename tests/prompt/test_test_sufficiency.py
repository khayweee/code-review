"""Tests for the test-sufficiency step's prompt-construction helper (Milestone 6, issue
#59): `build_test_sufficiency_prompt`. Pure function tests -- no agent, no subprocess,
mirroring `tests/prompt/test_review.py`'s structure and its `_SpyAgent` helper.
"""

from __future__ import annotations

from pathlib import Path

from code_review.pipeline.step import StepContext
from code_review.prompt.test_sufficiency import build_test_sufficiency_prompt
from code_review.steps.intent import Intent


class _SpyAgent:
    async def run(self, opts: object) -> object:  # pragma: no cover - not exercised
        raise AssertionError("build_test_sufficiency_prompt must not call the agent")

    async def close(self) -> None:  # pragma: no cover - not exercised
        pass


def _ctx(intent: Intent, diff: str = "diff --git a/f b/f\n+hello\n") -> StepContext:
    return StepContext(cwd=Path("."), agent=_SpyAgent(), diff=diff, intent=intent)  # type: ignore[arg-type]


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
