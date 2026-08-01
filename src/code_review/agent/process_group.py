"""Shared process-group teardown for backend adapters that spawn subprocesses.

https://github.com/khayweee/code-review/issues/6 - a spawned agent process can start its
own children (test runners, build watchers, git), and terminating only the direct child
leaves those grandchildren running as orphans. Any adapter that spawns with
``start_new_session=True`` (making the child's PID double as the whole process group's
PGID) can hand its ``Process`` to ``terminate_process_group`` to get the same guarantee:
nothing it started is still running once the call returns, on every exit path - success,
non-zero exit, a parse/validation failure, or cancellation.
"""

from __future__ import annotations

import asyncio
import os
import signal

# How long to wait after SIGTERM, and after SIGKILL, before giving up. Deliberately
# module-level constants rather than a public parameter: only tests need to shrink
# these, via monkeypatch.
_TERMINATION_GRACE_SECONDS = 5.0
_KILL_TIMEOUT_SECONDS = 5.0
_POLL_INTERVAL_SECONDS = 0.02


async def terminate_process_group(process: asyncio.subprocess.Process) -> None:
    """Guarantee nothing ``process`` started is still running once this returns.

    Requires ``process`` to have been spawned with ``start_new_session=True``, so its
    PID doubles as its process group's PGID and signalling the group (``os.killpg``)
    reaches grandchildren it spawned, not just the direct child. Escalates from SIGTERM
    to SIGKILL if the group is still alive after a grace deadline, then reaps the direct
    child so it doesn't linger as a zombie. Every wait below is deadline-bounded, so a
    descendant that refuses to die can never make this coroutine hang forever - it gives
    up cleanly instead.
    """

    pgid = process.pid
    if _signal_group(pgid, signal.SIGTERM) and not await _group_exited(
        pgid, _TERMINATION_GRACE_SECONDS
    ):
        if _signal_group(pgid, signal.SIGKILL):
            await _group_exited(pgid, _KILL_TIMEOUT_SECONDS)

    try:
        await asyncio.wait_for(process.wait(), timeout=_KILL_TIMEOUT_SECONDS)
    except TimeoutError:
        pass  # Give up cleanly rather than block this coroutine forever.


def _signal_group(pgid: int, sig: signal.Signals) -> bool:
    """Send ``sig`` to the process group; return False if it was already empty."""

    try:
        os.killpg(pgid, sig)
    except ProcessLookupError:
        return False
    return True


async def _group_exited(pgid: int, deadline_seconds: float) -> bool:
    """Poll, bounded by a deadline, until the process group has no members left."""

    loop = asyncio.get_running_loop()
    deadline = loop.time() + deadline_seconds
    while loop.time() < deadline:
        if not _group_alive(pgid):
            return True
        await asyncio.sleep(_POLL_INTERVAL_SECONDS)
    return not _group_alive(pgid)


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    return True
