"""Shared process-group teardown for backend adapters that spawn subprocesses.

Terminating only the direct child leaves any grandchildren it spawned (test runners,
build watchers, git) running as orphans. Any adapter that spawns with
``start_new_session=True`` (child PID doubles as the process group's PGID) can hand its
``Process`` to ``terminate_process_group`` to guarantee nothing it started outlives the
call, on any exit path.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal

_logger = logging.getLogger(__name__)

# How long to wait after SIGTERM, and after SIGKILL, before giving up. Tests shrink
# these via monkeypatch.
_TERMINATION_GRACE_SECONDS = 5.0
_KILL_TIMEOUT_SECONDS = 5.0
_POLL_INTERVAL_SECONDS = 0.02


async def terminate_process_group(process: asyncio.subprocess.Process) -> None:
    """Guarantee nothing ``process`` started is still running once this returns.

    Requires ``process`` to have been spawned with ``start_new_session=True`` so its PID
    doubles as its process group's PGID and ``os.killpg`` reaches grandchildren too.
    Escalates SIGTERM to SIGKILL after a grace deadline, then reaps the direct child.
    Every wait is deadline-bounded, so a stuck descendant can't hang this coroutine.
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
        # Should not happen after SIGKILL; likely stuck in uninterruptible sleep (D
        # state). Log rather than fail silently.
        _logger.warning(
            "process group %d: direct child did not exit within %.1fs of SIGKILL; "
            "it may still be running",
            pgid,
            _KILL_TIMEOUT_SECONDS,
        )


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
