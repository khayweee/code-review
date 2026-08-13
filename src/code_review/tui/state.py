"""Pure, Textual-independent backfill of pipeline progress into display rows.

`backfill` turns `pipeline.step.StepEvent`s seen so far into one `StepRow` per registry
entry; no Textual import, so it's unit-testable against hand-built `StepEvent`s. Every
comparison inside `backfill` keys off the canonical step name (`Step.get_name()`); its
optional `display_names` param only relabels the resulting `StepRow.name` for rendering, so
this module stays agnostic of any particular naming scheme.

`StepEvent.status` only distinguishes `"running"`/`"completed"` -- a step that raises never
gets a "completed" event, so the caller passes the failing step's name in as `failed_step`.
`parked_step`/`skipped_steps` are the same kind of caller-supplied override: `run_steps`
already yields a step's "completed" event before checking `outcome.needs_approval`, so
park/skip override what "completed" would otherwise render, rather than being a third state.

`latest_findings` scans `events` for the most recently completed step whose outcome carries
a non-empty `ReviewOutput`/`TestSufficiencyOutput`/bare `list[Finding]`.

`final_status_message` is the Status box's text once a run has finished.

`StepRow.detail` is a generic, opt-in extra line of text rendered inline on a step's own
row, next to its duration -- not PR-specific (a future step could reuse it), but today only
`backfill` populates it, from a completed `PullRequestOutcome` payload (`PRStep`'s "opened"/
"updated" PR link). `None` renders nothing extra, matching this codebase's "no box, not a
placeholder" discipline (see `PipelineBox.__init__`'s `branch` handling).

`ActivityRow`/`backfill_activities` do the same kind of extraction for the activity stream
`tui.activity.ActivityRelay` produces, grouping tagged `(step_name, ActivityEvent)` pairs
into one `ActivityRow` per activity, attached to its owning `StepRow.activities`. An activity
whose "finished" event carries an `error` (`ActivityHandle.fail(...)` was called) renders
`"failed"` with `detail` set to that error text, instead of `"completed"`.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from code_review.pipeline.findings import Finding
from code_review.pipeline.step import StepEvent, StepOutcome
from code_review.steps.intent import Intent
from code_review.steps.pr import PullRequestOutcome
from code_review.steps.review import ReviewOutput
from code_review.steps.test_sufficiency import TestSufficiencyOutput
from code_review.tui.activity import ActivityEvent

Status = Literal["pending", "running", "completed", "failed", "parked", "skipped"]


@dataclass(frozen=True, slots=True)
class ActivityRow:
    """One nested activity line, rendered under its owning step's `StepRow`.

    Reuses `Status` (only ever `"running"`/`"completed"`/`"failed"` here) so it renders with
    the same icon/duration formatting a `StepRow` uses. `detail` mirrors `StepRow.detail`'s
    precedent: `None` renders nothing extra, a "failed" row sets it to the
    `ActivityHandle.fail(...)` detail text (e.g. `"exit 1"`) that failed it.
    """

    label: str
    status: Status
    duration: float | None  # elapsed-so-far while running, final duration once finished
    detail: str | None = None


def backfill_activities(
    step_name: str, activity_events: Sequence[tuple[str | None, ActivityEvent]], *, now: float
) -> list[ActivityRow]:
    """Turn `activity_events` -- `(owning_step_name, ActivityEvent)` pairs -- into one
    `ActivityRow` per activity reported under `step_name`, in first-seen order. Pairs tagged
    with a different step (or `None`) are ignored.

    An activity with no matching "finished" event yet reports `now - started_at`; a finished
    one reports its own final elapsed time, `"completed"` unless its "finished" event carries
    an `error` (`"failed"`, with `detail` set to that error text).
    """

    started_at: dict[int, float] = {}
    finished_duration: dict[int, float] = {}
    finished_error: dict[int, str | None] = {}
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
            finished_error[event.activity_id] = event.error

    rows = []
    for activity_id in order:
        if activity_id in finished_duration:
            error = finished_error[activity_id]
            rows.append(
                ActivityRow(
                    label=label_by_id[activity_id],
                    status="failed" if error is not None else "completed",
                    duration=finished_duration[activity_id],
                    detail=error,
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

    name: str  # display name, e.g. "Intent" -- see `backfill`'s `display_names` param
    status: Status
    duration: float | None  # None while pending; elapsed-so-far while running/failed
    activities: tuple[ActivityRow, ...] = ()  # nested activity lines, first-seen order
    # Extra one-line text rendered after the duration -- see module docstring. None renders
    # nothing extra.
    detail: str | None = None


def _detail_for_completed_payload(
    payload: list[Finding] | ReviewOutput | TestSufficiencyOutput | Intent | PullRequestOutcome,
) -> str | None:
    """`StepRow.detail` text for a completed step's `outcome.payload`, or `None` for any
    payload shape that has no extra text to show -- currently only `PullRequestOutcome`
    (`PRStep`'s "opened"/"updated" PR link) does.
    """

    if not isinstance(payload, PullRequestOutcome):
        return None
    verb = "opened" if payload.created else "updated"
    return f"→ {verb} {payload.url}"


def backfill(
    registry: Sequence[str],
    events: Sequence[StepEvent],
    *,
    now: float,
    failed_step: str | None = None,
    parked_step: str | None = None,
    skipped_steps: Collection[str] = (),
    activity_events: Sequence[tuple[str | None, ActivityEvent]] = (),
    display_names: Mapping[str, str] | None = None,
) -> list[StepRow]:
    """Turn `events` seen so far into one `StepRow` per `registry` entry, in order.

    A registry entry with no event yet is `"pending"`. A `"running"` event with no matching
    `"completed"` yet is `"running"` (or `"failed"` if its name equals `failed_step`), with
    `duration` computed as `now - started_at`. A `"completed"` event is `"completed"`
    unless its name equals `parked_step` (`"parked"`) or is in `skipped_steps`
    (`"skipped"`) -- only the plain `"completed"` case also sets `StepRow.detail`, via
    `_detail_for_completed_payload` (see that function and `StepRow`'s own docstring).
    Each row's `activities` comes from `backfill_activities`.

    `registry`/`events`/`failed_step`/`parked_step`/`skipped_steps` all key off the same
    canonical per-step name (`Step.get_name()`, matched via `StepEvent.step_name`) -- that
    name is what every comparison in this function uses. `display_names` (typically
    `steps.registry.STEP_DISPLAY_NAMES`) is applied only at the very end, translating each
    row's rendered `StepRow.name` to a friendlier label; a name with no entry renders as-is.
    """

    started_at_by_step: dict[str, float] = {}
    duration_by_completed_step: dict[str, float] = {}
    outcome_by_completed_step: dict[str, StepOutcome] = {}
    for event in events:
        if event.status == "running":
            started_at_by_step[event.step_name] = event.started_at
        else:
            assert event.duration is not None  # a "completed" event always carries one
            duration_by_completed_step[event.step_name] = event.duration
            assert event.outcome is not None  # a "completed" event always carries one
            outcome_by_completed_step[event.step_name] = event.outcome

    rows = []
    for name in registry:
        display_name = name if display_names is None else display_names.get(name, name)
        activities = tuple(backfill_activities(name, activity_events, now=now))
        if name == parked_step:
            rows.append(
                StepRow(
                    name=display_name,
                    status="parked",
                    duration=duration_by_completed_step.get(name),
                    activities=activities,
                )
            )
        elif name in skipped_steps:
            rows.append(
                StepRow(
                    name=display_name,
                    status="skipped",
                    duration=duration_by_completed_step.get(name),
                    activities=activities,
                )
            )
        elif name in duration_by_completed_step:
            rows.append(
                StepRow(
                    name=display_name,
                    status="completed",
                    duration=duration_by_completed_step[name],
                    activities=activities,
                    detail=_detail_for_completed_payload(outcome_by_completed_step[name].payload),
                )
            )
        elif name in started_at_by_step:
            status: Status = "failed" if name == failed_step else "running"
            rows.append(
                StepRow(
                    name=display_name,
                    status=status,
                    duration=now - started_at_by_step[name],
                    activities=activities,
                )
            )
        else:
            rows.append(
                StepRow(name=display_name, status="pending", duration=None, activities=activities)
            )
    return rows


def latest_findings(
    events: Sequence[StepEvent],
) -> tuple[str, ReviewOutput | TestSufficiencyOutput | list[Finding]] | None:
    """Return the most recently completed step's name paired with its
    `ReviewOutput`/`TestSufficiencyOutput`/bare `list[Finding]`, or `None` if none exists.

    A `"completed"` event counts only when `outcome.payload` is one of those three shapes
    (`IntentStep`'s outcome carries a bare `Intent`, not findings) and non-empty. Scans
    `events` in order and keeps the last match, so most-recent-completion wins rather than
    accumulating history.
    """

    result: tuple[str, ReviewOutput | TestSufficiencyOutput | list[Finding]] | None = None
    for event in events:
        if event.status != "completed":
            continue
        outcome = event.outcome
        if outcome is None:
            continue
        payload = outcome.payload
        if isinstance(payload, (ReviewOutput, TestSufficiencyOutput)) and payload.findings:
            result = (event.step_name, payload)
        elif isinstance(payload, list) and payload:
            result = (event.step_name, payload)
    return result


def final_status_message(error: BaseException | None) -> str:
    """The Status box's message once a run has finished. `error` is `None` for a clean run
    or the raised exception for one that broke mid-step."""

    outcome = "Pipeline ran successfully." if error is None else f"Pipeline failed: {error}"
    return f"{outcome}\n\nPress 'e' to exit."
