"""`ActivityRelay`: a queue of `ActivityEvent`s for nested sub-step activity (e.g.
`RebaseStep`'s individual `git` calls, or `ReviewStep`'s one agent-call span), reported via
`async with relay.activity("label"): ...` and drained via `next_event()`.

Textual-import-free, so it's unit-testable without a running `App`/`Pilot`.

Nesting is automatic: a module-level `contextvars.ContextVar` tracks whichever activity is
currently open in the running coroutine, so a nested block records the enclosing activity as
its own `parent_id` with no call site passing a parent id explicitly.

One `ActivityRelay` instance is built per pipeline run, not per agent call.
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

# Currently open activity in the running coroutine, or None. Set/reset only by
# `ActivityRelay.activity`; scoped correctly per asyncio task by `contextvars` itself.
_current_activity_id: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "_current_activity_id", default=None
)


@dataclass(frozen=True, slots=True)
class ActivityEvent:
    """One reported activity transition: an `activity()` block starting or finishing."""

    activity_id: int
    # Derived from whichever activity was open when this one started; None if top-level.
    parent_id: int | None
    label: str
    status: ActivityStatus
    timestamp: float  # time.monotonic()


class ActivityRelay:
    """Queues `ActivityEvent`s from a reporting `activity()` block to a consuming
    `next_event()` caller."""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[ActivityEvent] = asyncio.Queue()
        self._next_id = count(1)  # unique within this relay's own event stream

    @asynccontextmanager
    async def activity(self, label: str) -> AsyncIterator[None]:
        """Report one nested unit of work named `label` for the duration of this block.

        See module docstring for the automatic-nesting/`parent_id` mechanics.
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
        """The next queued `ActivityEvent`; blocks until one is queued."""

        return await self._queue.get()
