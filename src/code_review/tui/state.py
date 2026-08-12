"""Pure, Textual-independent backfill of pipeline progress into display rows.

`backfill` turns `pipeline.step.StepEvent`s seen so far into one `StepRow` per registry
entry; no Textual import, so it's unit-testable against hand-built `StepEvent`s.

`StepEvent.status` only distinguishes `"running"`/`"completed"` -- a step that raises never
gets a "completed" event, so the caller passes the failing step's name in as `failed_step`.
`parked_step`/`skipped_steps` are the same kind of caller-supplied override: `run_steps`
already yields a step's "completed" event before checking `outcome.needs_approval`, so
park/skip override what "completed" would otherwise render, rather than being a third state.

`latest_findings` scans `events` for the most recently completed step whose outcome carries
a non-empty `ReviewOutput`/`TestSufficiencyOutput`/bare `list[Finding]`.

`final_status_message` is the Status box's text once a run has finished.

`ActivityRow`/`backfill_activities` do the same kind of extraction for the activity stream
`tui.activity.ActivityRelay` produces, grouping tagged `(step_name, ActivityEvent)` pairs
into one `ActivityRow` per activity, attached to its owning `StepRow.activities`.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass
from typing import Literal

from code_review.pipeline.findings import Finding
from code_review.pipeline.step import StepEvent
from code_review.steps.review import ReviewOutput
from code_review.steps.test_sufficiency import TestSufficiencyOutput
from code_review.tui.activity import ActivityEvent

Status = Literal["pending", "running", "completed", "failed", "parked", "skipped"]


@dataclass(frozen=True, slots=True)
class ActivityRow:
    """One nested activity line, rendered under its owning step's `StepRow`.

    Reuses `Status` (only ever `"running"`/`"completed"` here) so it renders with the same
    icon/duration formatting a `StepRow` uses.
    """

    label: str
    status: Status
    duration: float | None  # elapsed-so-far while running, final duration once finished


def backfill_activities(
    step_name: str, activity_events: Sequence[tuple[str | None, ActivityEvent]], *, now: float
) -> list[ActivityRow]:
    """Turn `activity_events` -- `(owning_step_name, ActivityEvent)` pairs -- into one
    `ActivityRow` per activity reported under `step_name`, in first-seen order. Pairs tagged
    with a different step (or `None`) are ignored.

    An activity with no matching "finished" event yet reports `now - started_at`; a
    finished one reports its own final elapsed time.
    """

    started_at: dict[int, float] = {}
    finished_duration: dict[int, float] = {}
    label_by_id: dict[int, str] = {}
    order: list[int] = []

    for owner, event in activity_events:
        if owner != step_name:
            continue
        if event.activity_id not in label_by_id:
            order.append(event.activity_id)
        label_by_id[event.activity_id] = event.label
        if event.status == "started":
            started_at[event.activity_id] = event.timestamp
        else:
            finished_duration[event.activity_id] = event.timestamp - started_at[event.activity_id]

    rows = []
    for activity_id in order:
        if activity_id in finished_duration:
            rows.append(
                ActivityRow(
                    label=label_by_id[activity_id],
                    status="completed",
                    duration=finished_duration[activity_id],
                )
            )
        else:
            rows.append(
                ActivityRow(
                    label=label_by_id[activity_id],
                    status="running",
                    duration=now - started_at[activity_id],
                )
            )
    return rows


@dataclass(frozen=True, slots=True)
class StepRow:
    """One line of the live pipeline-progress view."""

    name: str  # registry display name, e.g. "IntentStep"
    status: Status
    duration: float | None  # None while pending; elapsed-so-far while running/failed
    activities: tuple[ActivityRow, ...] = ()  # nested activity lines, first-seen order


def backfill(
    registry: Sequence[str],
    events: Sequence[StepEvent],
    *,
    now: float,
    failed_step: str | None = None,
    parked_step: str | None = None,
    skipped_steps: Collection[str] = (),
    activity_events: Sequence[tuple[str | None, ActivityEvent]] = (),
) -> list[StepRow]:
    """Turn `events` seen so far into one `StepRow` per `registry` entry, in order.

    A registry entry with no event yet is `"pending"`. A `"running"` event with no matching
    `"completed"` yet is `"running"` (or `"failed"` if its name equals `failed_step`), with
    `duration` computed as `now - started_at`. A `"completed"` event is `"completed"`
    unless its name equals `parked_step` (`"parked"`) or is in `skipped_steps`
    (`"skipped"`). Each row's `activities` comes from `backfill_activities`.
    """

    started_at_by_step: dict[str, float] = {}
    duration_by_completed_step: dict[str, float] = {}
    for event in events:
        if event.status == "running":
            started_at_by_step[event.step_name] = event.started_at
        else:
            assert event.duration is not None  # a "completed" event always carries one
            duration_by_completed_step[event.step_name] = event.duration

    rows = []
    for name in registry:
        activities = tuple(backfill_activities(name, activity_events, now=now))
        if name == parked_step:
            rows.append(
                StepRow(
                    name=name,
                    status="parked",
                    duration=duration_by_completed_step.get(name),
                    activities=activities,
                )
            )
        elif name in skipped_steps:
            rows.append(
                StepRow(
                    name=name,
                    status="skipped",
                    duration=duration_by_completed_step.get(name),
                    activities=activities,
                )
            )
        elif name in duration_by_completed_step:
            rows.append(
                StepRow(
                    name=name,
                    status="completed",
                    duration=duration_by_completed_step[name],
                    activities=activities,
                )
            )
        elif name in started_at_by_step:
            status: Status = "failed" if name == failed_step else "running"
            rows.append(
                StepRow(
                    name=name,
                    status=status,
                    duration=now - started_at_by_step[name],
                    activities=activities,
                )
            )
        else:
            rows.append(StepRow(name=name, status="pending", duration=None, activities=activities))
    return rows


def latest_findings(
    events: Sequence[StepEvent],
) -> tuple[str, ReviewOutput | TestSufficiencyOutput | list[Finding]] | None:
    """Return the most recently completed step's name paired with its
    `ReviewOutput`/`TestSufficiencyOutput`/bare `list[Finding]`, or `None` if none exists.

    A `"completed"` event counts only when `outcome.findings` is one of those three shapes
    (other steps' outcomes carry other types) and non-empty. Scans `events` in order and
    keeps the last match, so most-recent-completion wins rather than accumulating history.
    """

    result: tuple[str, ReviewOutput | TestSufficiencyOutput | list[Finding]] | None = None
    for event in events:
        if event.status != "completed":
            continue
        outcome = event.outcome
        if outcome is None:
            continue
        findings = outcome.findings
        if isinstance(findings, (ReviewOutput, TestSufficiencyOutput)) and findings.findings:
            result = (event.step_name, findings)
        elif isinstance(findings, list) and findings and isinstance(findings[0], Finding):
            result = (event.step_name, findings)
    return result


def final_status_message(error: BaseException | None) -> str:
    """The Status box's message once a run has finished. `error` is `None` for a clean run
    or the raised exception for one that broke mid-step."""

    outcome = "Pipeline ran successfully." if error is None else f"Pipeline failed: {error}"
    return f"{outcome}\n\nPress 'e' to exit."
