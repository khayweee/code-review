"""Pure unit tests for `backfill`/`latest_findings`, independent of Textual (see
`state.py`'s docstring).

No `App`, no `Pilot`, no subprocess -- every `StepEvent` here is hand-built.
"""

from __future__ import annotations

import pytest

from code_review.pipeline.findings import Finding
from code_review.pipeline.step import StepEvent, StepOutcome
from code_review.steps.intent import Intent
from code_review.steps.review import ReviewOutput
from code_review.steps.test_sufficiency import TestSufficiencyOutput
from code_review.tui.activity import ActivityEvent
from code_review.tui.state import (
    ActivityRow,
    StepRow,
    backfill,
    backfill_activities,
    final_status_message,
    latest_findings,
)

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


# --- latest_findings ---------------------------------------------------------------------

_FINDING = Finding(severity="warning", description="example finding", review_scope="source")


def _review_output(*findings: Finding) -> ReviewOutput:
    return ReviewOutput(findings=list(findings), risk_level="low", risk_rationale="fine")


def _test_sufficiency_output(*findings: Finding) -> TestSufficiencyOutput:
    return TestSufficiencyOutput(
        findings=list(findings), tested=[], testing_summary="fine", artifacts=[]
    )


def test_latest_findings_with_no_events_returns_none() -> None:
    assert latest_findings([]) is None


def test_latest_findings_ignores_a_completed_step_whose_outcome_is_not_a_review_output() -> None:
    # IntentStep-shaped: `outcome.findings` is an `Intent`, not a `ReviewOutput`.
    intent_outcome = StepOutcome(
        needs_approval=False,
        auto_fixable=False,
        findings=Intent(summary="add retries", source="explicit", score=1.0),
    )
    events = [
        StepEvent(
            step_name="IntentStep",
            status="completed",
            outcome=intent_outcome,
            started_at=1.0,
            duration=0.1,
        )
    ]

    assert latest_findings(events) is None


def test_latest_findings_ignores_a_completed_step_with_an_empty_findings_list() -> None:
    outcome = StepOutcome(needs_approval=False, auto_fixable=False, findings=_review_output())
    events = [
        StepEvent(
            step_name="ReviewStep",
            status="completed",
            outcome=outcome,
            started_at=1.0,
            duration=0.1,
        )
    ]

    assert latest_findings(events) is None


def test_latest_findings_returns_the_review_output_when_findings_are_non_empty() -> None:
    output = _review_output(_FINDING)
    outcome = StepOutcome(needs_approval=True, auto_fixable=False, findings=output)
    events = [
        StepEvent(
            step_name="ReviewStep",
            status="completed",
            outcome=outcome,
            started_at=1.0,
            duration=0.1,
        )
    ]

    assert latest_findings(events) is output


def test_latest_findings_with_two_completed_steps_the_later_one_wins() -> None:
    earlier = _review_output(
        Finding(severity="info", description="first pass", review_scope="source")
    )
    later = _review_output(
        Finding(severity="error", description="second pass", review_scope="source")
    )
    events = [
        StepEvent(
            step_name="ReviewStep",
            status="completed",
            outcome=StepOutcome(needs_approval=False, auto_fixable=False, findings=earlier),
            started_at=1.0,
            duration=0.1,
        ),
        StepEvent(
            step_name="TestSufficiencyStep",
            status="completed",
            outcome=StepOutcome(needs_approval=True, auto_fixable=False, findings=later),
            started_at=2.0,
            duration=0.1,
        ),
    ]

    assert latest_findings(events) is later


def test_latest_findings_ignores_a_completed_test_sufficiency_step_with_empty_findings() -> None:
    outcome = StepOutcome(
        needs_approval=False, auto_fixable=False, findings=_test_sufficiency_output()
    )
    events = [
        StepEvent(
            step_name="TestSufficiencyStep",
            status="completed",
            outcome=outcome,
            started_at=1.0,
            duration=0.1,
        )
    ]

    assert latest_findings(events) is None


def test_latest_findings_returns_the_test_sufficiency_output_when_findings_are_non_empty() -> None:
    output = _test_sufficiency_output(_FINDING)
    outcome = StepOutcome(needs_approval=True, auto_fixable=False, findings=output)
    events = [
        StepEvent(
            step_name="TestSufficiencyStep",
            status="completed",
            outcome=outcome,
            started_at=1.0,
            duration=0.1,
        )
    ]

    assert latest_findings(events) is output


def test_latest_findings_with_a_review_output_then_a_test_sufficiency_output_the_later_wins() -> (
    None
):
    earlier = _review_output(
        Finding(severity="info", description="first pass", review_scope="source")
    )
    later = _test_sufficiency_output(
        Finding(severity="error", description="second pass", review_scope="source")
    )
    events = [
        StepEvent(
            step_name="ReviewStep",
            status="completed",
            outcome=StepOutcome(needs_approval=False, auto_fixable=False, findings=earlier),
            started_at=1.0,
            duration=0.1,
        ),
        StepEvent(
            step_name="TestSufficiencyStep",
            status="completed",
            outcome=StepOutcome(needs_approval=True, auto_fixable=False, findings=later),
            started_at=2.0,
            duration=0.1,
        ),
    ]

    assert latest_findings(events) is later


def test_latest_findings_with_a_test_sufficiency_output_then_a_review_output_the_later_wins() -> (
    None
):
    earlier = _test_sufficiency_output(
        Finding(severity="info", description="first pass", review_scope="source")
    )
    later = _review_output(
        Finding(severity="error", description="second pass", review_scope="source")
    )
    events = [
        StepEvent(
            step_name="TestSufficiencyStep",
            status="completed",
            outcome=StepOutcome(needs_approval=False, auto_fixable=False, findings=earlier),
            started_at=1.0,
            duration=0.1,
        ),
        StepEvent(
            step_name="ReviewStep",
            status="completed",
            outcome=StepOutcome(needs_approval=True, auto_fixable=False, findings=later),
            started_at=2.0,
            duration=0.1,
        ),
    ]

    assert latest_findings(events) is later


# --- final_status_message -----------------------------------------------------------------


def test_final_status_message_reports_success_when_error_is_none() -> None:
    message = final_status_message(None)

    assert message.startswith("Pipeline ran successfully.")
    assert "Press 'e' to exit." in message


def test_final_status_message_reports_the_error_when_present() -> None:
    message = final_status_message(RuntimeError("rebase conflict"))

    assert message.startswith("Pipeline failed: rebase conflict")
    assert "Press 'e' to exit." in message


# --- backfill_activities / StepRow.activities (issue #66) ---------------------------------


def test_backfill_activities_with_no_events_is_empty() -> None:
    assert backfill_activities("RebaseStep", [], now=10.0) == []


def test_backfill_activities_ignores_events_tagged_for_a_different_step() -> None:
    events: list[tuple[str | None, ActivityEvent]] = [
        ("ReviewStep", ActivityEvent(1, None, "agent call", "started", 5.0)),
        (None, ActivityEvent(2, None, "untagged", "started", 5.0)),
    ]

    assert backfill_activities("RebaseStep", events, now=10.0) == []


def test_backfill_activities_reports_elapsed_duration_for_a_still_running_activity() -> None:
    events: list[tuple[str | None, ActivityEvent]] = [
        ("RebaseStep", ActivityEvent(1, None, "fetch", "started", 5.0)),
    ]

    rows = backfill_activities("RebaseStep", events, now=7.5)

    assert rows == [ActivityRow(label="fetch", status="running", duration=2.5)]


def test_backfill_activities_reports_final_duration_not_elapsed_once_finished() -> None:
    events: list[tuple[str | None, ActivityEvent]] = [
        ("RebaseStep", ActivityEvent(1, None, "fetch", "started", 5.0)),
        ("RebaseStep", ActivityEvent(1, None, "fetch", "finished", 5.4)),
    ]

    # `now` is far past the activity's own duration -- a finished activity's duration must
    # come from its own events, not `now - started_at`, mirroring `backfill`'s own rule.
    rows = backfill_activities("RebaseStep", events, now=999.0)

    assert len(rows) == 1
    assert rows[0].label == "fetch"
    assert rows[0].status == "completed"
    assert rows[0].duration == pytest.approx(0.4)


def test_backfill_activities_preserves_first_seen_order_across_multiple_activities() -> None:
    events: list[tuple[str | None, ActivityEvent]] = [
        ("RebaseStep", ActivityEvent(1, None, "fetch", "started", 1.0)),
        ("RebaseStep", ActivityEvent(1, None, "fetch", "finished", 1.2)),
        ("RebaseStep", ActivityEvent(2, None, "rebase", "started", 1.2)),
    ]

    rows = backfill_activities("RebaseStep", events, now=1.5)

    assert len(rows) == 2
    assert rows[0].label == "fetch"
    assert rows[0].status == "completed"
    assert rows[0].duration == pytest.approx(0.2)
    assert rows[1].label == "rebase"
    assert rows[1].status == "running"
    assert rows[1].duration == pytest.approx(0.3)


def test_backfill_attaches_each_steps_own_activities_to_its_row() -> None:
    events = [
        StepEvent(
            step_name="RebaseStep", status="running", outcome=None, started_at=1.0, duration=None
        )
    ]
    activity_events: list[tuple[str | None, ActivityEvent]] = [
        ("RebaseStep", ActivityEvent(1, None, "fetch", "started", 1.1)),
    ]

    rows = backfill(REGISTRY, events, now=1.4, activity_events=activity_events)

    # REGISTRY is ("IntentStep", "RebaseStep", "ReviewStep") -- RebaseStep is rows[1].
    assert rows[1].name == "RebaseStep"
    assert len(rows[1].activities) == 1
    assert rows[1].activities[0].label == "fetch"
    assert rows[1].activities[0].status == "running"
    assert rows[1].activities[0].duration == pytest.approx(0.3)
    # Steps with no reported activity keep an empty tuple.
    assert rows[0].activities == ()
    assert rows[2].activities == ()


def test_backfill_without_activity_events_defaults_every_row_to_no_activities() -> None:
    """`activity_events` defaults to `()` -- every existing caller/test that never passes
    it keeps getting `StepRow`s equal to ones built with an explicit empty tuple."""

    rows = backfill(REGISTRY, [], now=100.0)

    assert rows == [
        StepRow(name="IntentStep", status="pending", duration=None),
        StepRow(name="RebaseStep", status="pending", duration=None),
        StepRow(name="ReviewStep", status="pending", duration=None),
    ]
    assert all(row.activities == () for row in rows)
