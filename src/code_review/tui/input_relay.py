"""`InputRelay`: queues an agent subprocess's stdin prompts for a human to answer.

Deliberately Textual-import-free, so its queueing contract is unit-testable in isolation
(`tests/tui/test_input_relay.py`), independent of a running `App`/`Pilot`.

Exists to break a construction-order cycle: `cli.py` needs `ctx.on_input_needed` bound
before `StepContext`/`run_steps(...)` can be built, but `ReviewApp` is constructed *from*
that same `events` generator -- so neither side can hold a live reference to the other yet.
`InputRelay` is built first and handed to both independently: `cli.py` passes
`relay.request_input` as `StepContext.on_input_needed`, and `relay` itself as `ReviewApp`'s
`input_relay`; `ReviewApp`'s worker calls `next_request` to learn what to show and
`future.set_result(...)` to resolve the human's answer.
"""

from __future__ import annotations

import asyncio


class InputRelay:
    """Queues `(prompt, future)` pairs between a blocked backend call and a running TUI."""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[tuple[str, asyncio.Future[str]]] = asyncio.Queue()

    async def request_input(self, prompt: str) -> str:
        """The `on_input_needed`-shaped callable: queue `prompt` and await its answer.

        Called from `claude_cli.py`'s stdin-relay loop. Blocks until whatever is consuming
        `next_request` resolves the future it was handed for this call.
        """

        future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        await self._queue.put((prompt, future))
        return await future

    async def next_request(self) -> tuple[str, asyncio.Future[str]]:
        """Consumed by `ReviewApp`'s worker: the next queued prompt and its answer future.

        Blocks until a `request_input` call queues one. The caller is expected to collect
        an answer from the human and call `future.set_result(answer)`.
        """

        return await self._queue.get()
