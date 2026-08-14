"""`ReviewApp`: full-screen live view of a review run.

Takes `registry` and `events` (an `AsyncIterator[StepEvent]`) as constructor args rather than
building them itself, so tests can drive it with a hand-built fake generator via Textual's
`Pilot`/`run_test()`.

`input_relay`, `activity_relay`, and `approval_relay` are optional, independently polled
seams (see their own modules): each starts its own worker in `on_mount` only when supplied.

`branch` is an optional display-only string (the branch under review, already known to
`cli.py`), read once at startup and handed to `PipelineBox` as its `border_subtitle`; `None`
shows no subtitle at all.

`display_names` is an optional canonical-name -> friendly-label mapping (e.g.
`steps.registry.STEP_DISPLAY_NAMES`), applied only where a step name is rendered (Pipeline
box rows, Findings box title) -- every internal comparison (registry order, `parked_step`,
`skipped_steps`, activity attribution) still keys off the canonical name from `registry`/
`StepEvent.step_name`. `None`/`{}` renders every step under its raw canonical name.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Mapping, Sequence

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

    Re-renders on every `StepEvent` and on a timer tick, so a running step's elapsed
    duration ticks visibly between events. Once `events` is exhausted or raises, the run is
    marked done, the tick timer stops, and a Status box appears naming the outcome (the app
    no longer exits itself; see `action_exit_when_done`). On an exception, the failing step
    is inferred as whichever step was last seen `"running"` with no matching `"completed"`
    yet.

    Also mounts a Findings box, driven by `state.latest_findings`, only while there is
    something to show.
    """

    BINDINGS = [("e", "exit_when_done", "Exit")]  # no-op until `self._done`

    def __init__(
        self,
        registry: Sequence[str],
        events: AsyncIterator[StepEvent],
        input_relay: InputRelay | None = None,
        activity_relay: ActivityRelay | None = None,
        approval_relay: ApprovalRelay | None = None,
        branch: str | None = None,
        display_names: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__()
        # Not `_registry` -- shadows `textual.app.App`'s own mounted-widget registry.
        self._step_registry = tuple(registry)
        self._events = events
        # Canonical step name (`registry`/`StepEvent.step_name`) -> friendly label, e.g.
        # `steps.registry.STEP_DISPLAY_NAMES`. `{}` renders every row under its raw name.
        self._display_names = display_names or {}
        # None disables the corresponding worker in `on_mount` below.
        self._input_relay = input_relay
        self._activity_relay = activity_relay
        self._approval_relay = approval_relay
        # Read once at startup, not live-polled -- a run's branch can't change mid-run
        # under this project's own concurrency model (see docs/GATE-MODEL.md).
        self._branch = branch
        # Raw events in receipt order, not yet tagged with an owning step; `_rows` tags
        # them via `_tag_activity_events` immediately before every render.
        self._activity_events: list[ActivityEvent] = []
        self._seen: list[StepEvent] = []
        # Step last seen "running" with no "completed" yet; who to blame if `events` raises.
        self._running_step: str | None = None
        # Set if `events` raises, to whichever step was running; keeps rendering as failed.
        self._failed_step: str | None = None
        # Step currently awaiting a human's approve/skip/fix/abort decision, or None.
        self._parked_step: str | None = None
        # Steps a human has answered "skip" for; stays visible for the app's lifetime.
        self._skipped_steps: set[str] = set()
        # id() of every StepOutcome a human has resolved, so `latest_findings` stops
        # re-matching it once no later step supersedes it.
        self._resolved_outcome_ids: set[int] = set()
        self._done = False  # True once `events` is exhausted or raises

        # Set only if iterating `events` raised; `cli.py` uses it for a nonzero exit code.
        self.error: BaseException | None = None

    def compose(self) -> ComposeResult:
        yield PipelineBox(self._rows(), branch=self._branch)

    def on_mount(self) -> None:
        self._tick_timer: Timer = self.set_interval(_TICK_INTERVAL, self._render)
        self.run_worker(self._consume_events(), exclusive=True)
        if self._input_relay is not None:
            # Separate group so `exclusive` on the events worker doesn't cancel this one.
            self.run_worker(self._relay_input(), group="input-relay")
        if self._activity_relay is not None:
            self.run_worker(self._consume_activities(), group="activity-relay")
        if self._approval_relay is not None:
            self.run_worker(self._relay_approval(), group="approval-relay")

    def action_exit_when_done(self) -> None:
        """Exits only once the run has finished."""

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
            display_names=self._display_names,
        )

    def _render(self) -> None:
        """Re-render every box driven by current state.

        Swallows `MountError`: a render can still be scheduled (tick timer or a worker)
        right as `self.exit()` fires, and `is_attached` goes False slightly before
        `Widget.mount()`'s own closing/pruning check catches up, so a render landing in
        that gap raises instead of no-op'ing. Nothing is worth rendering at that point
        anyway.
        """

        try:
            self.query_one(PipelineBox).update_rows(self._rows())
            self._render_findings()
            self._render_status()
        except MountError:
            pass

    def _render_findings(self) -> None:
        """Mount, update in place, or remove the Findings box, driven by
        `latest_findings(self._seen)`. Mounted only while there is something to show."""

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
            display_name = self._display_names.get(step_name, step_name)
            if boxes:
                boxes[0].update_findings(output, display_name)
            else:
                self.mount(FindingsList(output, display_name))

    def _render_status(self) -> None:
        """Mount, update in place, or remove the Status box; appears only once
        `self._done`."""

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
            # See PipelineBox.stop_shimmer's own docstring: left running, its independent
            # timer starves this final _render() from ever actually reaching the screen.
            self.query_one(PipelineBox).stop_shimmer()
            self._render()

    async def _relay_input(self) -> None:
        """Poll `self._input_relay` for relayed prompts, show a modal, and resolve the
        matching `request_input` call with the human's answer."""

        assert self._input_relay is not None
        while True:
            prompt, future = await self._input_relay.next_request()
            answer = await self.push_screen_wait(InputPromptScreen(prompt))
            future.set_result(answer)

    async def _consume_activities(self) -> None:
        """Poll `self._activity_relay` for reported sub-step activity and re-render.

        Owner correlation happens later, at render time -- see `_tag_activity_events`.
        """

        assert self._activity_relay is not None
        while True:
            event = await self._activity_relay.next_event()
            self._activity_events.append(event)
            self._render()

    async def _relay_approval(self) -> None:
        """Poll `self._approval_relay` for a parked step's approve/skip/fix/abort request.

        Marks `self._parked_step`, re-renders, then awaits the mounted
        `FindingsList.await_decision()` directly (no modal). "skip" is recorded into
        `self._skipped_steps`; "abort" is `run_steps`'s own job via `RunAbortedError`.
        Clears `_parked_step` and re-renders again before resolving `future`, so rendered
        state reflects the decision before the parked `run_steps` call resumes.
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
    """Attribute each of `activity_events` to the step that reported it, from `seen`'s
    `StepEvent` timestamps. Computed fresh on every render, not tagged at receipt time,
    because `_consume_activities` and `_consume_events` are two independently scheduled
    workers -- tagging at receipt could split one activity span's started/finished pair
    across two owners if a step transition races the activity events.

    Steps run strictly sequentially, so each step's running window (its "running" event's
    `started_at` through its "completed" event's implied end, or open-ended while running)
    fully contains every activity it reports. Both events of one span get the same owner,
    computed once from the "started" event and reused for "finished".
    """

    windows: dict[str, tuple[float, float | None]] = {}
    for step_event in seen:
        if step_event.status == "running":
            windows[step_event.step_name] = (step_event.started_at, None)
        else:
            assert step_event.duration is not None
            # Falls back to this event's own started_at if no "running" event preceded it.
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
    """Which step's running window `timestamp` falls inside, or `None` if none do."""

    for name, (started_at, completed_at) in windows.items():
        if timestamp < started_at:
            continue
        if completed_at is None or timestamp < completed_at:
            return name
    return None
