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

`latest_findings` (issue #42) is the same kind of pure extraction as `backfill`, scanning
`events` for the most recently completed step whose outcome carries a non-empty
`ReviewOutput`. It imports `ReviewOutput` from `steps.review` -- a data-schema import only,
not a `ReviewStep`/agent-call dependency, and fine directionally (`tui` importing a
`steps/`-defined pydantic model does not create a cycle, since `steps/` never imports
`tui/`). `tui/app.py` calls it alongside `backfill` on every event and on the timer tick;
`tests/tui/test_state.py` calls it directly against hand-built `StepEvent`s.

`final_status_message` is the same kind of pure extraction again: the Status box's text
once a run has finished, so a run's outcome (and the "e" exits now" cue) is what's left on
screen instead of an app that silently exits itself the instant the last event arrives --
see `app.py`'s `_render_status` for why that self-exit was removed.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from code_review.pipeline.step import StepEvent
from code_review.steps.review import ReviewOutput

Status = Literal["pending", "running", "completed", "failed"]


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


def backfill(
    registry: Sequence[str],
    events: Sequence[StepEvent],
    *,
    now: float,
    failed_step: str | None = None,
) -> list[StepRow]:
    """Turn `events` seen so far into one `StepRow` per `registry` entry, in order.

    A registry entry with no event yet is `"pending"`. A `"running"` event with no
    matching `"completed"` event yet is `"running"` (or `"failed"` if its name equals
    `failed_step`), with `duration` computed as `now - started_at`. A `"completed"` event
    is `"completed"`, with `duration` taken from that event itself.
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
        if name in duration_by_completed_step:
            rows.append(
                StepRow(name=name, status="completed", duration=duration_by_completed_step[name])
            )
        elif name in started_at_by_step:
            status: Status = "failed" if name == failed_step else "running"
            rows.append(StepRow(name=name, status=status, duration=now - started_at_by_step[name]))
        else:
            rows.append(StepRow(name=name, status="pending", duration=None))
    return rows


def latest_findings(events: Sequence[StepEvent]) -> ReviewOutput | None:
    """Return the most recently completed step's `ReviewOutput`, or `None` if none exists.

    A `"completed"` event counts only when its `outcome.findings` is a `ReviewOutput`
    instance (guarding against e.g. `IntentStep`'s outcome, whose `findings` is an `Intent`,
    never triggering a findings display -- see `pipeline/step.py`'s `StepOutcome.findings`,
    deliberately untyped as `object`) AND that `ReviewOutput.findings` list is non-empty.
    Scans `events` in order and keeps the last match, so two completed steps that both carry
    findings resolve to whichever completed later, matching `PipelineBox`'s own "one box,
    most recent wins" display -- not an accumulated history across steps.
    """

    result: ReviewOutput | None = None
    for event in events:
        if event.status != "completed":
            continue
        outcome = event.outcome
        if outcome is None:
            continue
        findings = outcome.findings
        if isinstance(findings, ReviewOutput) and findings.findings:
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
