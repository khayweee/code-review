"""`ReviewApp`: the full-screen live pipeline-progress view (Milestone 13, issue #40).

Takes `registry` and `events` as constructor arguments rather than importing
`steps.registry`/`pipeline.executor` itself -- production code (`cli.py`) passes
`run_steps(steps, ctx)` as `events`; tests pass a hand-built fake async generator yielding
`StepEvent`s directly. That injection seam is what makes this app testable with Textual's
`Pilot`/`run_test()`, independent of a real agent subprocess (see `tests/tui/test_app.py`).
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Sequence

from textual.app import App, ComposeResult

from code_review.pipeline.step import StepEvent
from code_review.tui.state import StepRow, backfill
from code_review.tui.widgets import PipelineBox

# How often a running step's elapsed duration re-renders between events. Short enough to
# look live to a human, long enough not to burn CPU on a terminal repaint loop.
_TICK_INTERVAL = 0.25


class ReviewApp(App[None]):
    """Renders `registry` as a live Pipeline box, driven by `events`.

    Iterates `events` in a worker started on mount, re-rendering the Pipeline box after
    every `StepEvent` and on a timer tick (so a running step's elapsed duration visibly
    ticks between events). Exits itself -- no keypress required -- once `events` is
    exhausted or raises. On an exception, the raising step is inferred as whichever step
    was last seen `"running"` with no matching `"completed"` yet (see `state.py`'s
    `backfill` docstring for why `StepEvent` itself has no "failed" status), the Pipeline
    box renders that step as failed one last time, and the exception is stored on `error`
    for the caller (`cli.py`) to turn into a nonzero exit after `run()` returns.
    """

    def __init__(self, registry: Sequence[str], events: AsyncIterator[StepEvent]) -> None:
        super().__init__()
        # Named `_step_registry`, not `_registry` -- `textual.app.App` already owns a
        # `_registry` attribute internally (its mounted-widget registry); shadowing it
        # corrupts app teardown instead of raising anywhere near this assignment.
        self._step_registry = tuple(registry)
        self._events = events
        self._seen: list[StepEvent] = []
        # Name of the step most recently seen "running" with no "completed" yet -- the
        # step a mid-flight exception must be blamed on. Reset to None once that step's
        # "completed" event arrives.
        self._running_step: str | None = None

        # Set only if iterating `events` raised; None means the run finished normally.
        # `cli.py` checks this after `run()` returns to surface a step failure as a real
        # nonzero CLI exit even though the TUI itself already exited cleanly.
        self.error: BaseException | None = None

    def compose(self) -> ComposeResult:
        yield PipelineBox(self._rows())

    def on_mount(self) -> None:
        self.set_interval(_TICK_INTERVAL, self._render)
        self.run_worker(self._consume_events(), exclusive=True)

    def _rows(self, *, failed_step: str | None = None) -> list[StepRow]:
        return backfill(
            self._step_registry, self._seen, now=time.monotonic(), failed_step=failed_step
        )

    def _render(self, *, failed_step: str | None = None) -> None:
        self.query_one(PipelineBox).update_rows(self._rows(failed_step=failed_step))

    async def _consume_events(self) -> None:
        try:
            async for event in self._events:
                self._seen.append(event)
                self._running_step = event.step_name if event.status == "running" else None
                self._render()
        except Exception as exc:  # reported via `self.error`, not swallowed -- see below
            self.error = exc
            self._render(failed_step=self._running_step)
        finally:
            self.exit()
