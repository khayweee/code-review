"""Pure, Textual-independent backfill of pipeline progress into display rows.

`backfill` is the seam that lets this repo's Pilot-driven and pure unit tests stay
separate: it turns `pipeline.step.StepEvent`s seen so far into one `StepRow` per registry
entry, with no Textual import anywhere in this module. `tui/app.py` calls it on every
event and on a timer tick; `tests/tui/test_state.py` calls it directly against hand-built
`StepEvent`s, independent of a running `App`.

`StepEvent.status` only distinguishes `"running"`/`"completed"` (see `pipeline/step.py`) --
a step that raises never gets its "completed" event, so "failed" is not something the
executor reports. The caller here (the App) is the only thing that knows a run aborted
mid-step; it passes that step's name in as `failed_step` so the final render can show it as
failed instead of stuck "running" forever.

`latest_findings` (issue #42, widened for #61) is the same kind of pure extraction as
`backfill`, scanning `events` for the most recently completed step whose outcome carries a
non-empty `ReviewOutput` or `TestSufficiencyOutput`. It imports both from `steps.review`/
`steps.test_sufficiency` -- a data-schema import only, not a `ReviewStep`/
`TestSufficiencyStep`/agent-call dependency, and fine directionally (`tui` importing a
`steps/`-defined pydantic model does not create a cycle, since `steps/` never imports
`tui/`). `tui/app.py` calls it alongside `backfill` on every event and on the timer tick;
`tests/tui/test_state.py` calls it directly against hand-built `StepEvent`s.

`final_status_message` is the same kind of pure extraction again: the Status box's text
once a run has finished, so a run's outcome (and the "e" exits now" cue) is what's left on
screen instead of an app that silently exits itself the instant the last event arrives --
see `app.py`'s `_render_status` for why that self-exit was removed.

`ActivityRow`/`backfill_activities` (issue #66) are the same kind of pure extraction again,
for the second, independent activity stream `tui.activity.ActivityRelay` produces:
`backfill_activities` groups the `(step_name, ActivityEvent)` pairs `app.py`'s activity
worker has tagged and collected (see `activity.py`'s module docstring for why that tagging,
not `ActivityRelay` itself, is what assigns an activity to a step) into one `ActivityRow`
per activity reported under one given step, using the identical "elapsed-while-running,
final-once-finished" duration rule `backfill` uses for `StepRow`. `backfill` itself now
attaches each step's own `ActivityRow`s to that `StepRow`'s new `activities` field --
`tui/widgets.py`'s `PipelineBox` renders them as nested lines under their owning step's row
regardless of that step's own current status (pending/running/completed/failed), so an
activity's line -- and its final duration -- stays visible once reported, the same way a
completed `StepRow` itself stays visible for the rest of the run.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from code_review.pipeline.step import StepEvent
from code_review.steps.review import ReviewOutput
from code_review.steps.test_sufficiency import TestSufficiencyOutput
from code_review.tui.activity import ActivityEvent

Status = Literal["pending", "running", "completed", "failed"]


@dataclass(frozen=True, slots=True)
class ActivityRow:
    """One nested activity line, rendered under its owning step's `StepRow` (see
    `backfill_activities`). Reuses `Status` (only ever `"running"`/`"completed"` here) so
    `tui/widgets.py` can render it with the exact same `_STATUS_ICONS`/`format_duration`
    conventions a `StepRow` uses, rather than a parallel icon/formatting set.
    """

    label: str
    status: Status
    # None only transiently (never actually produced today -- `ActivityRelay.activity`
    # always queues a "started" event before any "finished" one can exist); elapsed-so-far
    # while running, the event's own final duration once finished. Mirrors `StepRow.duration`.
    duration: float | None


def backfill_activities(
    step_name: str, activity_events: Sequence[tuple[str | None, ActivityEvent]], *, now: float
) -> list[ActivityRow]:
    """Turn `activity_events` -- `(owning_step_name, ActivityEvent)` pairs, as `app.py`'s
    `_consume_activities` worker tags and collects them -- into one `ActivityRow` per
    activity reported under `step_name`, in first-seen order. Pairs tagged with a
    different step name (or `None`, e.g. an activity that arrived with no step yet marked
    running) are ignored here -- correlation itself already happened at the tagging site
    (`app.py`), not in this function.

    Mirrors `backfill`'s own duration rule: an activity with a `"started"` event and no
    matching `"finished"` one yet reports `now - started_at`; one with both reports the
    `"finished"` event's own elapsed time, not recomputed against `now`.
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

    # Registry display name (e.g. "IntentStep"), identical to `Step.get_name()`.
    name: str

    # Current render state for this row, derived by `backfill` from the events seen so
    # far plus an optional `failed_step` override -- never set directly by `StepEvent`
    # itself, which has no "failed" status (see module docstring).
    status: Status

    # None while pending (no event seen yet); elapsed-so-far while running or failed
    # (computed against the caller-supplied `now`); the event's own final duration once
    # completed.
    duration: float | None

    # Nested activity lines reported under this step (issue #66), in first-seen order --
    # `()` for a step with no reported activity (every step today, until #64/#65 land a
    # real producer). See `backfill_activities`.
    activities: tuple[ActivityRow, ...] = ()


def backfill(
    registry: Sequence[str],
    events: Sequence[StepEvent],
    *,
    now: float,
    failed_step: str | None = None,
    activity_events: Sequence[tuple[str | None, ActivityEvent]] = (),
) -> list[StepRow]:
    """Turn `events` seen so far into one `StepRow` per `registry` entry, in order.

    A registry entry with no event yet is `"pending"`. A `"running"` event with no
    matching `"completed"` event yet is `"running"` (or `"failed"` if its name equals
    `failed_step`), with `duration` computed as `now - started_at`. A `"completed"` event
    is `"completed"`, with `duration` taken from that event itself. Each row's
    `activities` comes from `backfill_activities(name, activity_events, now=now)` --
    `()` when `activity_events` is omitted, so every existing caller/test keeps working
    unchanged.
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
        if name in duration_by_completed_step:
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


def latest_findings(events: Sequence[StepEvent]) -> ReviewOutput | TestSufficiencyOutput | None:
    """Return the most recently completed step's `ReviewOutput`/`TestSufficiencyOutput`, or
    `None` if none exists.

    A `"completed"` event counts only when its `outcome.findings` is a `ReviewOutput` or
    `TestSufficiencyOutput` instance (guarding against e.g. `IntentStep`'s outcome, whose
    `findings` is an `Intent`, never triggering a findings display -- see `pipeline/step.py`'s
    `StepOutcome.findings`, deliberately untyped as `object`) AND that output's `findings`
    list is non-empty. Scans `events` in order and keeps the last match, so two completed
    steps that both carry findings resolve to whichever completed later -- regardless of
    which of the two schemas each one is -- matching `PipelineBox`'s own "one box, most
    recent wins" display -- not an accumulated history across steps.
    """

    result: ReviewOutput | TestSufficiencyOutput | None = None
    for event in events:
        if event.status != "completed":
            continue
        outcome = event.outcome
        if outcome is None:
            continue
        findings = outcome.findings
        if isinstance(findings, (ReviewOutput, TestSufficiencyOutput)) and findings.findings:
            result = findings
    return result


def final_status_message(error: BaseException | None) -> str:
    """The Status box's message once a run has finished (see `app.py`'s `_render_status`):
    one line naming the outcome, then the reminder that pressing "e" now closes the app --
    the run itself won't produce anything further either way. `error` is `None` for a
    clean run and the raised exception for one that broke mid-step; either way this is the
    one thing on screen once the run is done, so a fast (or headless-fast, e.g. today's
    single-`IntentStep` pipeline) run cannot flash by and vanish with no visible trace of
    what happened. Pure, like `backfill`/`latest_findings`, so `tests/tui/test_state.py`
    can pin its wording directly, independent of a running `App`/`Pilot`.
    """

    outcome = "Pipeline ran successfully." if error is None else f"Pipeline failed: {error}"
    return f"{outcome}\n\nPress 'e' to exit."
