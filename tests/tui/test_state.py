"""Pure unit tests for `backfill`, independent of Textual (see `state.py`'s docstring).

No `App`, no `Pilot`, no subprocess -- every `StepEvent` here is hand-built.
"""

from __future__ import annotations

import pytest

from code_review.pipeline.step import StepEvent, StepOutcome
from code_review.tui.state import StepRow, backfill

REGISTRY = ("IntentStep", "RebaseStep", "ReviewStep")

_OUTCOME = StepOutcome(needs_approval=False, auto_fixable=False, findings=None)


def test_backfill_with_no_events_renders_every_registry_entry_as_pending() -> None:
    rows = backfill(REGISTRY, [], now=100.0)

    assert rows == [
        StepRow(name="IntentStep", status="pending", duration=None),
        StepRow(name="RebaseStep", status="pending", duration=None),
        StepRow(name="ReviewStep", status="pending", duration=None),
    ]


def test_backfill_with_a_running_step_reports_elapsed_duration_against_now() -> None:
    events = [
        StepEvent(
            step_name="IntentStep", status="running", outcome=None, started_at=10.0, duration=None
        )
    ]

    rows = backfill(REGISTRY, events, now=12.5)

    assert rows[0] == StepRow(name="IntentStep", status="running", duration=2.5)
    # Later registry entries are still pending -- no event has touched them yet.
    assert rows[1] == StepRow(name="RebaseStep", status="pending", duration=None)
    assert rows[2] == StepRow(name="ReviewStep", status="pending", duration=None)


def test_backfill_elapsed_duration_ticks_as_now_advances() -> None:
    events = [
        StepEvent(
            step_name="IntentStep", status="running", outcome=None, started_at=10.0, duration=None
        )
    ]

    earlier = backfill(REGISTRY, events, now=10.1)
    later = backfill(REGISTRY, events, now=10.9)

    assert earlier[0].duration == pytest.approx(0.1)
    assert later[0].duration == pytest.approx(0.9)
    assert later[0].duration > earlier[0].duration


def test_backfill_with_a_completed_step_reports_its_final_duration_not_elapsed() -> None:
    events = [
        StepEvent(
            step_name="IntentStep", status="running", outcome=None, started_at=10.0, duration=None
        ),
        StepEvent(
            step_name="IntentStep",
            status="completed",
            outcome=_OUTCOME,
            started_at=10.0,
            duration=0.4,
        ),
    ]

    # `now` is far past the step's own duration -- a completed step's duration must come
    # from its own event, not `now - started_at`.
    rows = backfill(REGISTRY, events, now=999.0)

    assert rows[0] == StepRow(name="IntentStep", status="completed", duration=0.4)
    assert rows[1] == StepRow(name="RebaseStep", status="pending", duration=None)
    assert rows[2] == StepRow(name="ReviewStep", status="pending", duration=None)


def test_backfill_marks_the_named_failed_step_as_failed_with_elapsed_duration() -> None:
    events = [
        StepEvent(
            step_name="RebaseStep", status="running", outcome=None, started_at=5.0, duration=None
        )
    ]

    rows = backfill(REGISTRY, events, now=7.0, failed_step="RebaseStep")

    assert rows[1] == StepRow(name="RebaseStep", status="failed", duration=2.0)


def test_backfill_failed_step_override_does_not_affect_other_running_steps() -> None:
    """`failed_step` names exactly the step that broke -- it must not recolor an
    unrelated step that also happens to still be `"running"`."""

    events = [
        StepEvent(
            step_name="IntentStep", status="running", outcome=None, started_at=1.0, duration=None
        ),
        StepEvent(
            step_name="RebaseStep", status="running", outcome=None, started_at=2.0, duration=None
        ),
    ]

    rows = backfill(REGISTRY, events, now=3.0, failed_step="RebaseStep")

    assert rows[0] == StepRow(name="IntentStep", status="running", duration=2.0)
    assert rows[1] == StepRow(name="RebaseStep", status="failed", duration=1.0)


def test_backfill_a_completed_step_is_not_recolored_failed_even_if_named() -> None:
    """A step that already completed cannot also be the one that failed -- `failed_step`
    only overrides a step still mid-flight ("running" with no "completed" event yet)."""

    events = [
        StepEvent(
            step_name="IntentStep", status="running", outcome=None, started_at=1.0, duration=None
        ),
        StepEvent(
            step_name="IntentStep",
            status="completed",
            outcome=_OUTCOME,
            started_at=1.0,
            duration=0.2,
        ),
    ]

    rows = backfill(REGISTRY, events, now=5.0, failed_step="IntentStep")

    assert rows[0] == StepRow(name="IntentStep", status="completed", duration=0.2)
