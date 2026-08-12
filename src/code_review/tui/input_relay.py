"""`InputRelay`: queues an agent subprocess's stdin prompts for a human to answer.

Textual-import-free, so it's unit-testable without a running `App`/`Pilot`. Built before
`StepContext`/`ReviewApp` and handed to both, breaking their construction-order cycle
(`cli.py` passes `relay.request_input` as `on_input_needed`; `ReviewApp`'s worker calls
`next_request`/resolves the future it returns).
"""

from __future__ import annotations

import asyncio


class InputRelay:
    """Queues `(prompt, future)` pairs between a blocked backend call and a running TUI."""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[tuple[str, asyncio.Future[str]]] = asyncio.Queue()

    async def request_input(self, prompt: str) -> str:
        """Queue `prompt` and await its answer. Blocks until `next_request`'s caller
        resolves the returned future."""

        future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        await self._queue.put((prompt, future))
        return await future

    async def next_request(self) -> tuple[str, asyncio.Future[str]]:
        """The next queued prompt and its answer future; blocks until `request_input`
        queues one."""

        return await self._queue.get()
