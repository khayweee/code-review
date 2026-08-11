"""`ReviewApp`: full-screen live view of a review run.

Takes `registry` and `events` (an `AsyncIterator[StepEvent]`) as constructor args instead of
building them itself -- `cli.py` passes `run_steps(steps, ctx)`; tests pass a hand-built fake
generator. This seam is what makes the app testable via Textual's `Pilot`/`run_test()`,
independent of a real agent subprocess.

`input_relay`, `activity_relay`, and `approval_relay` are three optional, independently
polled seams of the same shape (see their own modules): each starts its own worker in
`on_mount` only when supplied, relaying human input, sub-step activity, and park decisions.
None of them add a new `StepEvent` status or change `run_steps`'s yield shape.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Sequence

from textual.app import App, ComposeResult
from textual.timer import Timer
from textual.widget import MountError

from code_review.pipeline.step import StepEvent
from code_review.tui.activity import ActivityEvent, ActivityRelay
from code_review.tui.approval_relay import ApprovalRelay
from code_review.tui.input_relay import InputRelay
from code_review.tui.screens import InputPromptScreen
from code_review.tui.state import StepRow, backfill, final_status_message, latest_findings
from code_review.tui.widgets import FindingsList, PipelineBox, StatusBox

# How often a running step's elapsed duration re-renders between events.
_TICK_INTERVAL = 0.25


class ReviewApp(App[None]):
    """Renders `registry` as a live Pipeline box, driven by `events`.

    Re-renders the Pipeline box on every `StepEvent` and on a timer tick, so a running
    step's elapsed duration ticks visibly between events. Once `events` is exhausted or
    raises, the run is marked done, the tick timer stops, and a Status box appears naming
    the outcome -- the app no longer exits itself; see `action_exit_when_done`. On an
    exception, the failing step is inferred as whichever step was last seen `"running"`
    with no matching `"completed"` yet (`StepEvent` itself has no "failed" status -- see
    `state.py`).

    Also mounts a Findings box, driven by `state.latest_findings`, at the same points the
    Pipeline box re-renders -- mounted only while there is something to show.
    """

    # "e" is a no-op until `self._done` -- see `action_exit_when_done`.
    BINDINGS = [("e", "exit_when_done", "Exit")]

    def __init__(
        self,
        registry: Sequence[str],
        events: AsyncIterator[StepEvent],
        input_relay: InputRelay | None = None,
        activity_relay: ActivityRelay | None = None,
        approval_relay: ApprovalRelay | None = None,
    ) -> None:
        super().__init__()
        # Not `_registry` -- `textual.app.App` already owns that attribute (its mounted-
        # widget registry); shadowing it corrupts app teardown instead of raising here.
        self._step_registry = tuple(registry)
        self._events = events
        # None disables the corresponding worker in `on_mount` below.
        self._input_relay = input_relay
        self._activity_relay = activity_relay
        self._approval_relay = approval_relay
        # Raw events in receipt order, NOT yet tagged with an owning step -- `_rows` tags
        # them from here via `_tag_activity_events` immediately before every render.
        self._activity_events: list[ActivityEvent] = []
        self._seen: list[StepEvent] = []
        # Step last seen "running" with no "completed" yet -- who to blame if `events`
        # raises. Reset to None once that step's "completed" event arrives.
        self._running_step: str | None = None
        # Set once, in `_consume_events`'s `except` branch, to whichever step was running
        # when it raised -- read by every later render so the Pipeline box keeps showing
        # that step as failed for the rest of the app's lifetime.
        self._failed_step: str | None = None
        # Step currently awaiting a human's approve/skip/fix/abort decision, or None.
        self._parked_step: str | None = None
        # Steps a human has answered "skip" for -- kept for the app's lifetime, the same
        # "stays visible" rule `_failed_step` follows.
        self._skipped_steps: set[str] = set()
        # `id()` of every `StepOutcome` a human has already resolved. Without this,
        # `latest_findings` keeps re-matching the same resolved outcome forever once no
        # later step has findings of its own to supersede it, leaving `FindingsList`
        # mounted at full parked height after the run ends.
        self._resolved_outcome_ids: set[int] = set()
        # True once `events` is exhausted or raises.
        self._done = False

        # Set only if iterating `events` raised; `cli.py` checks this after `run()` returns
        # to surface a step failure as a real nonzero CLI exit.
        self.error: BaseException | None = None

    def compose(self) -> ComposeResult:
        yield PipelineBox(self._rows())

    def on_mount(self) -> None:
        self._tick_timer: Timer = self.set_interval(_TICK_INTERVAL, self._render)
        self.run_worker(self._consume_events(), exclusive=True)
        if self._input_relay is not None:
            # Own worker group -- `exclusive` only cancels other workers in the *same*
            # group, so this must not share the (default, exclusive) events worker's group.
            self.run_worker(self._relay_input(), group="input-relay")
        if self._activity_relay is not None:
            self.run_worker(self._consume_activities(), group="activity-relay")
        if self._approval_relay is not None:
            self.run_worker(self._relay_approval(), group="approval-relay")

    def action_exit_when_done(self) -> None:
        """Bound to "e" -- exits only once the run has finished."""

        if self._done:
            self.exit()

    def _rows(self) -> list[StepRow]:
        return backfill(
            self._step_registry,
            self._seen,
            now=time.monotonic(),
            failed_step=self._failed_step,
            parked_step=self._parked_step,
            skipped_steps=self._skipped_steps,
            activity_events=_tag_activity_events(self._seen, self._activity_events),
        )

    def _render(self) -> None:
        """Re-render every box driven by current state.

        Four independent callers can reach this -- the tick timer, `_consume_events`,
        `_consume_activities`, and `_relay_approval` -- any of which can still be
        scheduled to run after `self.exit()` fires (e.g. a background worker's own
        pending render, mid-flight when the app starts exiting). `Widget.mount()` treats
        a widget already flagged `_closing`/`_pruning` as a graceful no-op, but that flag
        lags one step behind `App.exit()` itself: `is_attached` (which every mount call
        checks first) goes `False` the instant `exit()` sets `App._exit`, before
        `_closing`/`_pruning` catch up -- so a render landing in that gap raises
        `MountError` instead of no-op'ing. Nothing is worth rendering once the app is on
        its way out either way, so this simply drops the render rather than crash it.
        """

        try:
            self.query_one(PipelineBox).update_rows(self._rows())
            self._render_findings()
            self._render_status()
        except MountError:
            pass

    def _render_findings(self) -> None:
        """Mount, update in place, or remove the Findings box, driven by
        `latest_findings(self._seen)`. Unlike `PipelineBox`, which is always composed,
        this box is mounted dynamically and only while there is something to show -- no
        findings means no box at all, not an empty one."""

        visible_events = [
            event for event in self._seen if id(event.outcome) not in self._resolved_outcome_ids
        ]
        result = latest_findings(visible_events)
        boxes = list(self.query(FindingsList))
        if result is None:
            for box in boxes:
                box.remove()
        else:
            step_name, output = result
            if boxes:
                boxes[0].update_findings(output, step_name)
            else:
                self.mount(FindingsList(output, step_name))

    def _render_status(self) -> None:
        """Mount, update in place, or remove the Status box, mirroring
        `_render_findings`'s dynamic-mount pattern: it appears only once `self._done`."""

        boxes = list(self.query(StatusBox))
        if not self._done:
            for box in boxes:
                box.remove()
            return
        message = final_status_message(self.error)
        if boxes:
            boxes[0].update_status(message)
        else:
            self.mount(StatusBox(message))

    async def _consume_events(self) -> None:
        try:
            async for event in self._events:
                self._seen.append(event)
                self._running_step = event.step_name if event.status == "running" else None
                self._render()
        except Exception as exc:  # reported via `self.error`, not swallowed
            self.error = exc
            self._failed_step = self._running_step
        finally:
            self._done = True
            self._tick_timer.stop()
            self._render()

    async def _relay_input(self) -> None:
        """Poll `self._input_relay` for prompts a blocked backend subprocess relayed --
        show a modal, resolve the matching `request_input` call with the human's answer."""

        assert self._input_relay is not None
        while True:
            prompt, future = await self._input_relay.next_request()
            answer = await self.push_screen_wait(InputPromptScreen(prompt))
            future.set_result(answer)

    async def _consume_activities(self) -> None:
        """Poll `self._activity_relay` for reported sub-step activity and re-render.

        Owner correlation does NOT happen here at receipt time -- see
        `_tag_activity_events` for why that has to be computed later, at render time.
        """

        assert self._activity_relay is not None
        while True:
            event = await self._activity_relay.next_event()
            self._activity_events.append(event)
            self._render()

    async def _relay_approval(self) -> None:
        """Poll `self._approval_relay` for a parked step's approve/skip/fix/abort request.

        Marks `self._parked_step` and re-renders (the Findings box already shows this
        step's outcome -- it mounted on the "completed" event before the park was noticed),
        then awaits the mounted `FindingsList.await_decision()` directly -- no modal.
        "skip" is recorded into `self._skipped_steps`; "abort" is `run_steps`'s own job via
        `RunAbortedError` once the resolved future lets it resume. Clears `_parked_step`
        and re-renders again before resolving `future`, so the app's own rendered state
        already reflects the decision by the time the parked `run_steps` call resumes.
        """

        assert self._approval_relay is not None
        while True:
            step_name, outcome, future = await self._approval_relay.next_request()
            self._parked_step = step_name
            self._render()
            findings_box = self.query_one(FindingsList)
            response = await findings_box.await_decision()
            self._parked_step = None
            self._resolved_outcome_ids.add(id(outcome))
            if response.decision == "skip":
                self._skipped_steps.add(step_name)
            self._render()
            future.set_result(response)


def _tag_activity_events(
    seen: Sequence[StepEvent], activity_events: Sequence[ActivityEvent]
) -> list[tuple[str | None, ActivityEvent]]:
    """Attribute each of `activity_events` to the step that reported it, purely from
    `seen`'s own `StepEvent` timestamps -- computed fresh on every render, not tagged once
    at collection time.

    `_consume_activities` and `_consume_events` are two independently scheduled workers
    draining two independent queues, so nothing guarantees a "finished" `ActivityEvent` is
    tagged before the owning step's own "completed" `StepEvent` advances
    `self._running_step` to the next step. Tagging at receipt time can therefore split one
    activity span's started/finished pair across two different owners and crash
    `state.backfill_activities`' duration lookup.

    Sound because steps run strictly sequentially (`pipeline/AGENTS.md`): each step's own
    running window -- its "running" event's `started_at` through its "completed" event's
    implied end time, or open-ended while still running -- fully contains every activity it
    reports. Both events of one span get the SAME owner, computed once from the "started"
    event and reused for "finished", so a "finished" timestamp landing a hair outside the
    window can never split one span across two owners.
    """

    windows: dict[str, tuple[float, float | None]] = {}
    for step_event in seen:
        if step_event.status == "running":
            windows[step_event.step_name] = (step_event.started_at, None)
        else:
            assert step_event.duration is not None
            # Falls back to this event's own `started_at` if no "running" event preceded
            # it -- some hand-built test fixtures skip straight to "completed".
            started_at, _ = windows.get(step_event.step_name, (step_event.started_at, None))
            windows[step_event.step_name] = (started_at, started_at + step_event.duration)

    owner_by_activity_id: dict[int, str | None] = {}
    tagged: list[tuple[str | None, ActivityEvent]] = []
    for activity_event in activity_events:
        if activity_event.status == "started":
            owner_by_activity_id[activity_event.activity_id] = _owning_step(
                activity_event.timestamp, windows
            )
        tagged.append((owner_by_activity_id.get(activity_event.activity_id), activity_event))
    return tagged


def _owning_step(timestamp: float, windows: dict[str, tuple[float, float | None]]) -> str | None:
    """Which step's running window `timestamp` falls inside, or `None` if it falls inside
    none of them (not expected in practice, but a safe fallback rather than an assertion)."""

    for name, (started_at, completed_at) in windows.items():
        if timestamp < started_at:
            continue
        if completed_at is None or timestamp < completed_at:
            return name
    return None
