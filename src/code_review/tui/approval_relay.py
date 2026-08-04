"""`ApprovalRelay`: queues a parked step's approve/skip/fix/abort decision for a human to
answer.

Deliberately Textual-import-free, matching `input_relay.py`/`activity.py`/`state.py`'s own
rule (see `tui/AGENTS.md`) -- this keeps `ApprovalRelay`'s queueing contract unit-testable
in isolation (`tests/tui/test_approval_relay.py`), independent of a running `App`/`Pilot`.

Same construction-order-cycle shape `InputRelay`'s own docstring explains: `cli.py` needs
`ctx.on_approval_needed` (see `pipeline/step.py`'s `StepContext.on_approval_needed`) bound
before `StepContext`/`run_steps(...)` can be built, but `ReviewApp` is constructed *from*
that same `events` generator -- so neither side can hold a live reference to the other at
construction time. `ApprovalRelay` is the thing built first and handed to both
independently: `cli.py` passes `relay.request_approval` as `StepContext.on_approval_needed`,
and `relay` itself as `ReviewApp`'s `approval_relay`; `ReviewApp`'s worker calls
`next_request` to learn which step parked and what its `StepOutcome` was, and
`future.set_result(response)` to resolve the human's approve/skip/fix/abort choice back to
whichever `request_approval` call (i.e. whichever parked `run_steps` call) is waiting on it.

A distinct module/class from `InputRelay`, not a reuse of it, because the shape of the
answer differs: `InputRelay` collects one line of free text for a blocked backend
subprocess; this collects an `ApprovalResponse` for a parked pipeline step. Mirrors
`InputRelay`'s own queueing/future mechanics exactly otherwise.

`ApprovalDecision`/`ApprovalResponse` (issue #81) are imported from `pipeline.step` rather
than defined here -- this module used to define its own `Decision = Literal["approve",
"skip", "abort"]`, but the type the approval seam carries is no longer a bare `Literal`
once a "fix" decision needs to carry free-text instructions alongside it (see
`pipeline/step.py`'s own docstring for why `ApprovalResponse` is shaped the way it is); the
same `tui/` already imports `StepOutcome` from that module, so this follows the existing
import-direction precedent rather than keeping a second, now-narrower definition here.
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
        """The `on_approval_needed`-shaped callable: queue `(step_name, outcome)` and await
        the human's response.

        Called from `pipeline.executor.run_steps` (via `StepContext.on_approval_needed`)
        once a round's outcome needs a park (see that module's "The approval park"/"The
        fix-round loop" sections). Blocks until whatever is consuming `next_request`
        resolves the future it was handed for this call.
        """

        future: asyncio.Future[ApprovalResponse] = asyncio.get_running_loop().create_future()
        await self._queue.put((step_name, outcome, future))
        return await future

    async def next_request(
        self,
    ) -> tuple[str, StepOutcome, asyncio.Future[ApprovalResponse]]:
        """Consumed by `ReviewApp`'s worker: the next queued park request -- the parked
        step's name, its `StepOutcome`, and the future to resolve with an
        `ApprovalResponse`.

        Blocks until a `request_approval` call queues one. The caller is expected to
        collect a decision (and, for "fix", free-text instructions) from the human and call
        `future.set_result(response)` to unblock the matching `request_approval` call.
        """

        return await self._queue.get()
