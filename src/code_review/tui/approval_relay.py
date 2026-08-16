"""`ApprovalRelay`: queues a parked step's approve/skip/fix/abort decision for a human to
answer.

Textual-import-free, so it's unit-testable without a running `App`/`Pilot`. Built before
`StepContext`/`ReviewApp` and handed to both, breaking their construction-order cycle
(`cli.py` passes `relay.request_approval` as `on_approval_needed`; `ReviewApp`'s worker calls
`next_request`/resolves the `pending_response` it returns).
"""

from __future__ import annotations

import asyncio

from code_review.pipeline.schemas import ApprovalResponse
from code_review.pipeline.step import StepOutcome
from code_review.tui.schemas import ApprovalRequest


class ApprovalRelay:
    """Queues `ApprovalRequest`s between a parked step (`pipeline.executor.run_steps`) and
    a running TUI. `ApprovalRequest.pending_response` is an `asyncio.Future[ApprovalResponse]`
    -- named for what it holds (the human's answer, not yet available) rather than the
    generic asyncio type, since a reader unfamiliar with `Future` shouldn't need to already
    know that vocabulary to see what this is."""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[ApprovalRequest] = asyncio.Queue()

    async def request_approval(self, step_name: str, outcome: StepOutcome) -> ApprovalResponse:
        """Queue an `ApprovalRequest` for `(step_name, outcome)` and await the human's
        response. Blocks until `next_request`'s caller resolves the returned
        `pending_response`."""

        pending_response: asyncio.Future[ApprovalResponse] = (
            asyncio.get_running_loop().create_future()
        )
        await self._queue.put(ApprovalRequest(step_name, outcome, pending_response))
        return await pending_response

    async def next_request(self) -> ApprovalRequest:
        """The next queued `ApprovalRequest`; blocks until `request_approval` queues one."""

        return await self._queue.get()
