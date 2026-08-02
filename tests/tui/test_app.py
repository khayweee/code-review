"""`ReviewApp` tests, driven with fake `StepEvent`s through Textual's `Pilot`/`run_test()`.

No `run_steps`, no `StepContext`, no `Agent`/`ClaudeCLI` subprocess anywhere here -- every
scenario below hands `ReviewApp` a hand-written async generator yielding `StepEvent`s
directly, per this issue's acceptance criteria (see `app.py`'s module docstring for why the
constructor takes `events: AsyncIterator[StepEvent]` rather than building its own stream).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator

from textual.widgets import Input, Static

from code_review.pipeline.findings import Finding
from code_review.pipeline.step import StepEvent, StepOutcome
from code_review.steps.review import ReviewOutput
from code_review.tui.app import ReviewApp
from code_review.tui.input_relay import InputRelay
from code_review.tui.screens import InputPromptScreen
from code_review.tui.widgets import FindingsBox, PipelineBox, render_findings

REGISTRY = ("IntentStep", "RebaseStep", "ReviewStep")

_OUTCOME = StepOutcome(needs_approval=False, auto_fixable=False, findings=None)


async def _one_step_completes() -> AsyncIterator[StepEvent]:
    started = time.monotonic()
    yield StepEvent(
        step_name="IntentStep", status="running", outcome=None, started_at=started, duration=None
    )
    await asyncio.sleep(0)
    yield StepEvent(
        step_name="IntentStep",
        status="completed",
        outcome=_OUTCOME,
        started_at=started,
        duration=0.01,
    )


async def _second_step_raises() -> AsyncIterator[StepEvent]:
    started = time.monotonic()
    yield StepEvent(
        step_name="IntentStep", status="running", outcome=None, started_at=started, duration=None
    )
    await asyncio.sleep(0)
    yield StepEvent(
        step_name="IntentStep",
        status="completed",
        outcome=_OUTCOME,
        started_at=started,
        duration=0.01,
    )
    rebase_started = time.monotonic()
    yield StepEvent(
        step_name="RebaseStep",
        status="running",
        outcome=None,
        started_at=rebase_started,
        duration=None,
    )
    await asyncio.sleep(0)
    raise RuntimeError("rebase conflict")


def test_review_app_renders_every_registered_step_as_pending_before_any_event() -> None:
    async def never_yields() -> AsyncIterator[StepEvent]:
        return
        yield  # pragma: no cover - makes this an async generator

    async def scenario() -> None:
        app = ReviewApp(REGISTRY, never_yields())
        async with app.run_test() as pilot:
            await pilot.pause()
            box = app.query_one(PipelineBox)
            assert box.content == "○ IntentStep\n○ RebaseStep\n○ ReviewStep"
            app.exit()

    asyncio.run(scenario())


def test_review_app_shows_not_yet_implemented_steps_as_pending_throughout_the_run() -> None:
    async def scenario() -> None:
        app = ReviewApp(REGISTRY, _one_step_completes())
        async with app.run_test() as pilot:
            await pilot.pause()
            box = app.query_one(PipelineBox)
            # RebaseStep and ReviewStep have no class yet -- they stay pending placeholders
            # for the whole run, even once IntentStep has finished.
            assert "○ RebaseStep" in box.content
            assert "○ ReviewStep" in box.content

    asyncio.run(scenario())


def test_review_app_exits_itself_on_success_with_no_keypress_and_no_error() -> None:
    async def scenario() -> None:
        app = ReviewApp(REGISTRY, _one_step_completes())
        async with app.run_test() as pilot:
            for _ in range(20):
                if not app.is_running:
                    break
                await pilot.pause()
                await asyncio.sleep(0.01)

        assert app.is_running is False
        assert app.error is None

    asyncio.run(scenario())


def test_review_app_final_render_shows_intent_step_completed() -> None:
    async def scenario() -> None:
        app = ReviewApp(REGISTRY, _one_step_completes())
        async with app.run_test() as pilot:
            for _ in range(20):
                if not app.is_running:
                    break
                await pilot.pause()
                await asyncio.sleep(0.01)
            box = app.query_one(PipelineBox)
            assert "✓ IntentStep" in box.content

    asyncio.run(scenario())


def test_review_app_records_the_error_and_marks_the_mid_flight_step_failed() -> None:
    async def scenario() -> None:
        app = ReviewApp(REGISTRY, _second_step_raises())
        async with app.run_test() as pilot:
            for _ in range(20):
                if not app.is_running:
                    break
                await pilot.pause()
                await asyncio.sleep(0.01)

        assert app.is_running is False
        assert isinstance(app.error, RuntimeError)
        assert str(app.error) == "rebase conflict"

    asyncio.run(scenario())


def test_review_app_relays_a_queued_input_request_through_a_modal() -> None:
    """Issue #41 end-to-end proof: a concurrent `relay.request_input(...)` call (standing
    in for a blocked backend subprocess) surfaces as a modal with the right prompt text;
    typing an answer and pressing enter dismisses it and resolves the original call with
    that answer."""

    async def hangs_until_cancelled() -> AsyncIterator[StepEvent]:
        # Unlike `never_yields()` elsewhere in this file, this deliberately never lets
        # `_consume_events` finish and call `self.exit()` on its own -- the test needs the
        # app to stay alive for the whole modal round-trip, and calls `app.exit()` itself
        # once done (matching the pattern other tests in this file already use).
        await asyncio.Future()
        yield  # pragma: no cover - unreachable, only makes this an async generator

    async def scenario() -> str:
        relay = InputRelay()
        app = ReviewApp(REGISTRY, hangs_until_cancelled(), input_relay=relay)
        async with app.run_test() as pilot:
            await pilot.pause()

            request_task = asyncio.ensure_future(relay.request_input("Allow write? (y/n): "))
            for _ in range(20):
                if isinstance(app.screen, InputPromptScreen):
                    break
                await pilot.pause()
                await asyncio.sleep(0.01)

            assert isinstance(app.screen, InputPromptScreen)
            prompt_text = " ".join(str(widget.content) for widget in app.screen.query(Static))
            assert "Allow write? (y/n): " in prompt_text

            await pilot.click(Input)
            await pilot.press(*"yes", "enter")
            await pilot.pause()

            answer = await request_task
            app.exit()
            return answer

    answer = asyncio.run(scenario())

    assert answer == "yes"


def _review_output(*findings: Finding) -> ReviewOutput:
    return ReviewOutput(findings=list(findings), risk_level="low", risk_rationale="fine")


async def _review_step_completes_with_findings() -> AsyncIterator[StepEvent]:
    started = time.monotonic()
    yield StepEvent(
        step_name="ReviewStep", status="running", outcome=None, started_at=started, duration=None
    )
    await asyncio.sleep(0)
    output = _review_output(
        Finding(severity="error", description="removes error handling", review_scope="source")
    )
    outcome = StepOutcome(needs_approval=True, auto_fixable=False, findings=output)
    yield StepEvent(
        step_name="ReviewStep",
        status="completed",
        outcome=outcome,
        started_at=started,
        duration=0.01,
    )


async def _review_step_completes_with_no_findings() -> AsyncIterator[StepEvent]:
    started = time.monotonic()
    yield StepEvent(
        step_name="ReviewStep", status="running", outcome=None, started_at=started, duration=None
    )
    await asyncio.sleep(0)
    outcome = StepOutcome(needs_approval=False, auto_fixable=False, findings=_review_output())
    yield StepEvent(
        step_name="ReviewStep",
        status="completed",
        outcome=outcome,
        started_at=started,
        duration=0.01,
    )


async def _two_steps_complete_with_findings() -> AsyncIterator[StepEvent]:
    started = time.monotonic()
    earlier = _review_output(
        Finding(severity="info", description="first pass finding", review_scope="source")
    )
    later = _review_output(
        Finding(severity="error", description="second pass finding", review_scope="source")
    )
    yield StepEvent(
        step_name="ReviewStep",
        status="completed",
        outcome=StepOutcome(needs_approval=False, auto_fixable=False, findings=earlier),
        started_at=started,
        duration=0.01,
    )
    await asyncio.sleep(0)
    yield StepEvent(
        step_name="TestSufficiencyStep",
        status="completed",
        outcome=StepOutcome(needs_approval=True, auto_fixable=False, findings=later),
        started_at=started,
        duration=0.01,
    )


def test_review_app_shows_a_findings_box_once_a_step_completes_with_non_empty_findings() -> None:
    async def scenario() -> None:
        app = ReviewApp(REGISTRY, _review_step_completes_with_findings())
        async with app.run_test() as pilot:
            for _ in range(20):
                if not app.is_running:
                    break
                await pilot.pause()
                await asyncio.sleep(0.01)

            expected = _review_output(
                Finding(
                    severity="error", description="removes error handling", review_scope="source"
                )
            )
            box = app.query_one(FindingsBox)
            assert box.content == render_findings(expected)
            assert box.border_title == "Findings"

    asyncio.run(scenario())


def test_review_app_shows_no_findings_box_when_the_only_completed_outcome_has_no_findings() -> None:
    async def scenario() -> None:
        app = ReviewApp(REGISTRY, _one_step_completes())
        async with app.run_test() as pilot:
            for _ in range(20):
                if not app.is_running:
                    break
                await pilot.pause()
                await asyncio.sleep(0.01)

            # `_one_step_completes` reports an outcome with `findings=None` -- not a
            # `ReviewOutput` -- so no Findings box should ever mount.
            assert list(app.query(FindingsBox)) == []

    asyncio.run(scenario())


def test_review_app_shows_no_findings_box_when_the_completed_review_output_is_empty() -> None:
    async def scenario() -> None:
        app = ReviewApp(REGISTRY, _review_step_completes_with_no_findings())
        async with app.run_test() as pilot:
            for _ in range(20):
                if not app.is_running:
                    break
                await pilot.pause()
                await asyncio.sleep(0.01)

            assert list(app.query(FindingsBox)) == []

    asyncio.run(scenario())


def test_review_app_findings_box_shows_the_later_of_two_completed_findings() -> None:
    async def scenario() -> None:
        app = ReviewApp(REGISTRY, _two_steps_complete_with_findings())
        async with app.run_test() as pilot:
            for _ in range(20):
                if not app.is_running:
                    break
                await pilot.pause()
                await asyncio.sleep(0.01)

            box = app.query_one(FindingsBox)
            assert "second pass finding" in box.content
            assert "first pass finding" not in box.content

    asyncio.run(scenario())


def test_review_app_final_render_on_failure_shows_the_broken_step_as_failed() -> None:
    async def scenario() -> None:
        app = ReviewApp(REGISTRY, _second_step_raises())
        async with app.run_test() as pilot:
            for _ in range(20):
                if not app.is_running:
                    break
                await pilot.pause()
                await asyncio.sleep(0.01)
            box = app.query_one(PipelineBox)
            assert "✓ IntentStep" in box.content
            assert "✗ RebaseStep" in box.content
            # ReviewStep never ran -- still a pending placeholder even after the failure.
            assert "○ ReviewStep" in box.content

    asyncio.run(scenario())
