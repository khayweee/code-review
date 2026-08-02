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

from code_review.pipeline.step import StepEvent, StepOutcome
from code_review.tui.app import ReviewApp
from code_review.tui.widgets import PipelineBox

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
