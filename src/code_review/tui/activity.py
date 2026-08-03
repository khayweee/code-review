"""`ActivityRelay`: a second, independent progress stream for nested sub-step activity
(issue #66) -- e.g. `RebaseStep`'s individual `git fetch`/`git rebase` calls (issue #64,
not built here) or `ReviewStep`'s one coarse agent-call span (issue #65, not built here).
Neither real producer exists yet; this module is proven end to end with a hand-built,
synthetic reporter feeding events into a real `ReviewApp`, mirroring issue #41's own
precedent for `InputRelay` (see that module's docstring for the analogous framing).

Deliberately Textual-import-free, matching `input_relay.py`'s own rule (see `tui/AGENTS.md`)
-- this keeps `ActivityRelay`'s queueing contract unit-testable in isolation
(`tests/tui/test_activity.py`), independent of a running `App`/`Pilot`.

**Reporting side**: `async with relay.activity("label"): ...` -- a step wraps one unit of
work in this, and an `ActivityEvent` with `status="started"` is queued on entry, one with
`status="finished"` on exit (even on an exception -- the underlying generator's `finally`
runs on every exit path). Nesting is automatic, not caller-managed: a module-level
`contextvars.ContextVar` tracks whichever activity is currently open in the running
coroutine, so a nested `async with` block records the enclosing activity as its own
`parent_id` with no call site passing a parent id explicitly. This is naturally safe across
concurrent `asyncio.Task`s too -- each task gets its own copy of the context at creation --
though this repo's own steps run strictly sequentially in one task (see `pipeline/AGENTS.md`),
so that concurrency safety is a bonus this module gets for free, not a requirement anything
here exercises.

**Consuming side**: `next_event()` awaits the next queued `ActivityEvent`, mirroring
`InputRelay.next_request()`'s shape exactly. One `ActivityRelay` instance is built per run
(backed by one `asyncio.Queue`), the same way `InputRelay` is -- see `cli.py`'s wiring and
`pipeline/step.py`'s `ActivityReporter` Protocol / `StepContext.activity_reporter`/
`report_activity` for how a step reaches this without importing `tui/` directly.

`ActivityRelay` itself never needs to know steps exist: correlating a received event to
"which step" is `tui/app.py`'s job, tagging each one with whichever step is currently
running at receipt time (`ReviewApp._running_step`) -- steps run strictly sequentially and
never in parallel, so that tag is always correct. `tui/state.py`'s `backfill_activities`
does the actual per-step grouping from those tagged pairs; see that module's docstring.
"""

from __future__ import annotations

import asyncio
import contextvars
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from itertools import count
from typing import Literal

ActivityStatus = Literal["started", "finished"]

# Tracks whichever activity is currently open in the running coroutine -- `None` when no
# `activity()` block is open. Read (as a new activity's `parent_id`) and set/reset only by
# `ActivityRelay.activity` itself; nothing outside this module touches it directly. A
# module-level var (rather than a per-instance one) is fine because `contextvars` already
# scopes it correctly per asyncio task, and this repo builds at most one `ActivityRelay`
# per run (see `cli.py`).
_current_activity_id: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "_current_activity_id", default=None
)


@dataclass(frozen=True, slots=True)
class ActivityEvent:
    """One reported activity transition: an `activity()` block starting or finishing."""

    activity_id: int
    # Auto-derived from whichever activity was open when this one started (see module
    # docstring) -- `None` for a top-level activity with no enclosing `async with`.
    parent_id: int | None
    label: str
    status: ActivityStatus
    # `time.monotonic()`, matching `pipeline/step.py`'s `StepEvent` -- nothing here needs
    # to correlate against an external clock, only measure elapsed time within this run.
    timestamp: float


class ActivityRelay:
    """Queues `ActivityEvent`s from a reporting `async with relay.activity(...)` block to a
    consuming `next_event()` caller, backed by one `asyncio.Queue` per instance/run."""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[ActivityEvent] = asyncio.Queue()
        # Per-instance counter, not the module-level contextvar above -- ids only need to
        # be unique within this one relay's own event stream.
        self._next_id = count(1)

    @asynccontextmanager
    async def activity(self, label: str) -> AsyncIterator[None]:
        """Report one nested unit of work named `label` for as long as this block's body
        runs -- see module docstring for the automatic-nesting/`parent_id` mechanics.

        This is the `ActivityReporter`-shaped method `pipeline.step.StepContext.
        report_activity` delegates to (`pipeline/step.py`'s `ActivityReporter` Protocol).
        """

        activity_id = next(self._next_id)
        parent_id = _current_activity_id.get()
        token = _current_activity_id.set(activity_id)
        await self._queue.put(
            ActivityEvent(activity_id, parent_id, label, "started", time.monotonic())
        )
        try:
            yield
        finally:
            _current_activity_id.reset(token)
            await self._queue.put(
                ActivityEvent(activity_id, parent_id, label, "finished", time.monotonic())
            )

    async def next_event(self) -> ActivityEvent:
        """Consumed by `ReviewApp`'s activity worker: the next queued `ActivityEvent`.

        Blocks until an `activity()` call queues one.
        """

        return await self._queue.get()
