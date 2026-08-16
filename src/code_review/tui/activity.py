"""`ActivityRelay`: a queue of `ActivityEvent`s for nested sub-step activity (e.g.
`RebaseStep`'s individual `git` calls, or `ReviewStep`'s one agent-call span), reported via
`async with relay.activity("label"): ...` for a block of work, or `await
relay.log("label")` for a single point-in-time event, and drained via `next_event()`.

Textual-import-free, so it's unit-testable without a running `App`/`Pilot`.

Nesting is automatic: a module-level `contextvars.ContextVar` tracks whichever activity is
currently open in the running coroutine, so a nested block records the enclosing activity as
its own `parent_id` with no call site passing a parent id explicitly.

One `ActivityRelay` instance is built per pipeline run, not per agent call.

`ActivityEvent`/`ActivityStatus` are passive plumbing types, not defined in this file --
they live in `tui/schemas.py` (imported here at top level) alongside `ApprovalRequest`/
`InputRequest`, this package's other queued-request shapes.
"""

from __future__ import annotations

import asyncio
import contextvars
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from itertools import count

from code_review.pipeline.step import ActivityHandle
from code_review.tui.schemas import ActivityEvent

# Currently open activity in the running coroutine, or None. Set/reset only by
# `ActivityRelay.activity`; scoped correctly per asyncio task by `contextvars` itself.
_current_activity_id: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "_current_activity_id", default=None
)


class ActivityRelay:
    """Queues `ActivityEvent`s from a reporting `activity()` block to a consuming
    `next_event()` caller."""

    def __init__(self, *, on_event: Callable[[ActivityEvent], None] | None = None) -> None:
        self._queue: asyncio.Queue[ActivityEvent] = asyncio.Queue()
        self._next_id = count(1)  # unique within this relay's own event stream
        # Invoked synchronously right where an event would be queued (e.g. RunLogWriter's
        # write_activity_event), in addition to it being queued for next_event().
        self._on_event = on_event

    def _emit(self, event: ActivityEvent) -> None:
        if self._on_event is not None:
            self._on_event(event)
        self._queue.put_nowait(event)  # unbounded queue: never blocks/raises here

    @asynccontextmanager
    async def activity(self, label: str) -> AsyncIterator[ActivityHandle]:
        """Report one nested unit of work named `label` for the duration of this block.

        Yields an `ActivityHandle` the block's own body can call `.fail(detail)` on to mark
        the "finished" event as failed. See module docstring for the automatic-nesting/
        `parent_id` mechanics.
        """

        activity_id = next(self._next_id)
        parent_id = _current_activity_id.get()
        token = _current_activity_id.set(activity_id)
        handle = ActivityHandle()
        self._emit(ActivityEvent(activity_id, parent_id, label, "started", time.monotonic()))
        try:
            yield handle
        finally:
            _current_activity_id.reset(token)
            self._emit(
                ActivityEvent(
                    activity_id, parent_id, label, "finished", time.monotonic(), handle.error
                )
            )

    async def log(self, label: str) -> None:
        """Report one already-finished, near-zero-duration activity named `label` -- a
        moment in time (e.g. one LLM tool call) rather than a block of work. Emits the same
        "started"/"finished" `ActivityEvent` pair `activity()` would, back to back, so
        `next_event()`/backfill consumers need no separate code path for a one-shot event.
        """

        async with self.activity(label):
            pass

    async def next_event(self) -> ActivityEvent:
        """The next queued `ActivityEvent`; blocks until one is queued."""

        return await self._queue.get()
