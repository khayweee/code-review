"""`ReviewApp`: the full-screen live pipeline-progress view (Milestone 13, issue #40).

Takes `registry` and `events` as constructor arguments rather than importing
`steps.registry`/`pipeline.executor` itself -- production code (`cli.py`) passes
`run_steps(steps, ctx)` as `events`; tests pass a hand-built fake async generator yielding
`StepEvent`s directly. That injection seam is what makes this app testable with Textual's
`Pilot`/`run_test()`, independent of a real agent subprocess (see `tests/tui/test_app.py`).

`input_relay` (issue #41) is the same kind of injection seam: an optional
`tui.input_relay.InputRelay` this app polls, in a second worker, for prompts a blocked
backend subprocess relayed via `StepContext.on_input_needed`. See `input_relay.py`'s
module docstring for why the relay object -- not a live reference to `ReviewApp` itself --
is what `cli.py` hands to both sides of that seam.

**The app no longer exits itself once `events` is exhausted or raises.** It used to --
but with today's pipeline being a single near-instant `IntentStep` (Rebase/Review/Test
sufficiency/PR aren't wired in yet), a real run flashes onto the alternate screen buffer
and is gone in well under a second, indistinguishable from nothing having happened at all.
Instead, the run's end (success or failure) is marked `self._done = True`, a Status box
appears (`widgets.StatusBox`, driven by `state.final_status_message`) naming the outcome,
and the app waits for the user to press "e" (`action_exit_when_done`, a no-op until
`self._done`) before actually exiting. This applies equally to a failed run: seeing the
broken step and the error message on screen matters at least as much as a clean exit does.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Sequence

from textual.app import App, ComposeResult
from textual.timer import Timer

from code_review.pipeline.step import StepEvent
from code_review.tui.input_relay import InputRelay
from code_review.tui.screens import InputPromptScreen
from code_review.tui.state import StepRow, backfill, final_status_message, latest_findings
from code_review.tui.widgets import FindingsBox, PipelineBox, StatusBox

# How often a running step's elapsed duration re-renders between events. Short enough to
# look live to a human, long enough not to burn CPU on a terminal repaint loop.
_TICK_INTERVAL = 0.25


class ReviewApp(App[None]):
    """Renders `registry` as a live Pipeline box, driven by `events`.

    Iterates `events` in a worker started on mount, re-rendering the Pipeline box after
    every `StepEvent` and on a timer tick (so a running step's elapsed duration visibly
    ticks between events). Once `events` is exhausted or raises, the run is marked done
    (`self._done`), the tick timer stops, and a Status box appears naming the outcome --
    the app no longer exits itself at that point; see `action_exit_when_done`. On an
    exception, the raising step is inferred as whichever step was last seen `"running"`
    with no matching `"completed"` yet (see `state.py`'s `backfill` docstring for why
    `StepEvent` itself has no "failed" status), the Pipeline box renders that step as
    failed, and the exception is stored on `error` for the caller (`cli.py`) to turn into
    a nonzero exit once `run()` returns.

    Also mounts a Findings box (issue #42), driven by `state.py`'s `latest_findings`, at
    the same points the Pipeline box re-renders. Like the Status box, it is mounted only
    while there is something to show -- see `_render_findings` below.
    """

    # "e" is a no-op until `self._done` (see `action_exit_when_done`) -- it cannot cut a
    # real run short, only dismiss the app once there is nothing left for it to do.
    BINDINGS = [("e", "exit_when_done", "Exit")]

    def __init__(
        self,
        registry: Sequence[str],
        events: AsyncIterator[StepEvent],
        input_relay: InputRelay | None = None,
    ) -> None:
        super().__init__()
        # Named `_step_registry`, not `_registry` -- `textual.app.App` already owns a
        # `_registry` attribute internally (its mounted-widget registry); shadowing it
        # corrupts app teardown instead of raising anywhere near this assignment.
        self._step_registry = tuple(registry)
        self._events = events
        # None (the default, and every test that doesn't exercise the relay) means no
        # interactive-input worker starts in `on_mount` below -- see `input_relay.py`.
        self._input_relay = input_relay
        self._seen: list[StepEvent] = []
        # Name of the step most recently seen "running" with no "completed" yet -- the
        # step a mid-flight exception must be blamed on. Reset to None once that step's
        # "completed" event arrives.
        self._running_step: str | None = None
        # Set once, in `_consume_events`'s `except` branch, to whichever step was running
        # when it raised -- read by every later `_rows()` call (including the timer tick
        # and the final render) so the Pipeline box keeps showing that step as failed for
        # the rest of the app's lifetime, not just in one render call right before exit.
        self._failed_step: str | None = None
        # True once `events` is exhausted or raises -- the run itself has finished either
        # way. `action_exit_when_done` only acts once this is set; `_render_status` only
        # shows the Status box once this is set.
        self._done = False

        # Set only if iterating `events` raised; None means the run finished normally.
        # `cli.py` checks this after `run()` returns to surface a step failure as a real
        # nonzero CLI exit even though the TUI itself already exited cleanly.
        self.error: BaseException | None = None

    def compose(self) -> ComposeResult:
        yield PipelineBox(self._rows())

    def on_mount(self) -> None:
        self._tick_timer: Timer = self.set_interval(_TICK_INTERVAL, self._render)
        self.run_worker(self._consume_events(), exclusive=True)
        if self._input_relay is not None:
            # A distinct worker group from the (default-group, exclusive) events worker
            # above -- `exclusive` only cancels other workers in the *same* group, so this
            # would otherwise race with and cancel `_consume_events` on startup.
            self.run_worker(self._relay_input(), group="input-relay")

    def action_exit_when_done(self) -> None:
        """Bound to "e" -- exits the app, but only once the run has actually finished."""

        if self._done:
            self.exit()

    def _rows(self) -> list[StepRow]:
        return backfill(
            self._step_registry, self._seen, now=time.monotonic(), failed_step=self._failed_step
        )

    def _render(self) -> None:
        self.query_one(PipelineBox).update_rows(self._rows())
        self._render_findings()
        self._render_status()

    def _render_findings(self) -> None:
        """Mount, update in place, or remove the Findings box, driven by
        `latest_findings(self._seen)` (see `state.py`). Unlike `PipelineBox`, which is
        always composed, `FindingsBox` is mounted dynamically and only while there is
        something to show -- a step with no findings must show no Findings box at all, not
        an empty one (issue #42's acceptance criteria), which a permanently-composed box
        cannot express on its own."""

        output = latest_findings(self._seen)
        boxes = list(self.query(FindingsBox))
        if output is None:
            for box in boxes:
                box.remove()
        elif boxes:
            boxes[0].update_findings(output)
        else:
            self.mount(FindingsBox(output))

    def _render_status(self) -> None:
        """Mount, update in place, or remove the Status box, mirroring
        `_render_findings`'s own dynamic-mount pattern: it appears only once `self._done`
        is set, showing `state.final_status_message(self.error)` -- a still-running
        pipeline shows no Status box at all."""

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
        except Exception as exc:  # reported via `self.error`, not swallowed -- see below
            self.error = exc
            self._failed_step = self._running_step
        finally:
            self._done = True
            self._tick_timer.stop()
            self._render()

    async def _relay_input(self) -> None:
        """Poll `self._input_relay` for prompts a blocked backend subprocess relayed.

        Runs for the app's whole lifetime (cancelled automatically on `self.exit()`, like
        every other worker) -- each iteration surfaces one prompt as a modal, collects one
        line of human input, and resolves the matching `InputRelay.request_input` call
        with it, then waits for the next one.
        """

        assert self._input_relay is not None
        while True:
            prompt, future = await self._input_relay.next_request()
            answer = await self.push_screen_wait(InputPromptScreen(prompt))
            future.set_result(answer)
