"""`ApprovalRelay`: queues a parked step's approve/skip/fix/abort decision for a human to
answer.

Textual-import-free, so it's unit-testable without a running `App`/`Pilot`. Built before
`StepContext`/`ReviewApp` and handed to both, breaking their construction-order cycle
(`cli.py` passes `relay.request_approval` as `on_approval_needed`; `ReviewApp`'s worker calls
`next_request`/resolves the future it returns).
"""

from __future__ import annotations

import asyncio

from code_review.pipeline.step import ApprovalResponse, StepOutcome


class ApprovalRelay:
    """Queues `(step_name, outcome, future)` triples between a parked step (`pipeline.
    executor.run_steps`) and a running TUI."""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[tuple[str, StepOutcome, asyncio.Future[ApprovalResponse]]] = (
            asyncio.Queue()
        )

    async def request_approval(self, step_name: str, outcome: StepOutcome) -> ApprovalResponse:
        """Queue `(step_name, outcome)` and await the human's response. Blocks until
        `next_request`'s caller resolves the returned future."""

        future: asyncio.Future[ApprovalResponse] = asyncio.get_running_loop().create_future()
        await self._queue.put((step_name, outcome, future))
        return await future

    async def next_request(
        self,
    ) -> tuple[str, StepOutcome, asyncio.Future[ApprovalResponse]]:
        """The next queued park request (step name, outcome, future to resolve); blocks
        until `request_approval` queues one."""

        return await self._queue.get()
