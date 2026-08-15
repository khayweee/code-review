"""`InputRelay`: queues an agent subprocess's stdin prompts for a human to answer.

Textual-import-free, so it's unit-testable without a running `App`/`Pilot`. Built before
`StepContext`/`ReviewApp` and handed to both, breaking their construction-order cycle
(`cli.py` passes `relay.request_input` as `on_input_needed`; `ReviewApp`'s worker calls
`next_request`/resolves the `pending_answer` it returns).
"""

from __future__ import annotations

import asyncio

from code_review.tui.schemas import InputRequest


class InputRelay:
    """Queues `InputRequest`s between a blocked backend call and a running TUI.
    `InputRequest.pending_answer` is an `asyncio.Future[str]` -- named for what it holds
    (the human's typed answer, not yet available) rather than the generic asyncio type."""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[InputRequest] = asyncio.Queue()

    async def request_input(self, prompt: str) -> str:
        """Queue an `InputRequest` for `prompt` and await its answer. Blocks until
        `next_request`'s caller resolves the returned `pending_answer`."""

        pending_answer: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        await self._queue.put(InputRequest(prompt, pending_answer))
        return await pending_answer

    async def next_request(self) -> InputRequest:
        """The next queued `InputRequest`; blocks until `request_input` queues one."""

        return await self._queue.get()
