"""Cancellation-cleanup tests for ``process_group.terminate_process_group``.

https://github.com/khayweee/code-review/issues/6 - whichever way an adapter's call ends
(success, non-zero exit, a parse/validation failure, or the caller cancelling the
awaiting task), nothing the subprocess started should still be running afterward. This
is a shared, backend-agnostic guarantee (see ``process_group.py``); ``ClaudeCLI`` is
just the one adapter that exists today to drive it through. These tests exercise the
cancellation path specifically, since that's the one where cleanup must run from a
``finally`` block while a ``CancelledError`` is in flight, and they go through the real
``ClaudeCLI.run()`` call rather than invoking ``terminate_process_group`` directly, so
the process tree is genuine - a mocked subprocess could not prove this property.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Callable
from pathlib import Path

import pytest
from pydantic import BaseModel

from code_review.agent import ClaudeCLI, RunOpts
from code_review.agent import process_group as process_group_module

FAKES = Path(__file__).parent / "fakes"
HANGS_WITH_LIVE_GRANDCHILD_CLI = FAKES / "hangs_with_live_grandchild.py"
HANGS_IGNORING_SIGTERM_CLI = FAKES / "hangs_ignoring_sigterm.py"

_POLL_INTERVAL_SECONDS = 0.02
_POLL_DEADLINE_SECONDS = 5.0


class Answer(BaseModel):
    answer: str


async def _poll_until(
    predicate: Callable[[], bool], deadline_seconds: float = _POLL_DEADLINE_SECONDS
) -> bool:
    """Poll ``predicate()`` until it's true, bounded by a deadline. No fixed sleeps.

    Uses ``asyncio.sleep`` rather than ``time.sleep`` because this runs concurrently
    with the ``agent.run()`` task on the same event loop - a blocking sleep here would
    starve that task of the loop time it needs to ever spawn its subprocess.
    """

    deadline = time.monotonic() + deadline_seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(_POLL_INTERVAL_SECONDS)
    return bool(predicate())


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _read_grandchild_pid(pid_file: Path) -> int:
    return int(pid_file.read_text().strip())


def test_cancelling_run_kills_live_grandchild_of_hung_process(tmp_path: Path) -> None:
    pid_file = tmp_path / "grandchild.pid"

    async def scenario() -> None:
        agent = ClaudeCLI()
        task: asyncio.Task[object] = asyncio.ensure_future(
            agent.run(
                RunOpts(
                    prompt="hang please",
                    cwd=tmp_path,
                    output_schema=Answer,
                    executable=HANGS_WITH_LIVE_GRANDCHILD_CLI,
                )
            )
        )

        assert await _poll_until(pid_file.exists), "grandchild never wrote its pid file"
        grandchild_pid = _read_grandchild_pid(pid_file)
        assert _pid_alive(grandchild_pid), "grandchild should be alive before cancellation"

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert await _poll_until(lambda: not _pid_alive(grandchild_pid)), (
            "grandchild survived cancellation of run()"
        )

    asyncio.run(scenario())


def test_cancelling_run_escalates_to_sigkill_when_group_ignores_sigterm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(process_group_module, "_TERMINATION_GRACE_SECONDS", 0.1)
    monkeypatch.setattr(process_group_module, "_KILL_TIMEOUT_SECONDS", 1.0)
    pid_file = tmp_path / "grandchild.pid"

    async def scenario() -> float:
        agent = ClaudeCLI()
        task: asyncio.Task[object] = asyncio.ensure_future(
            agent.run(
                RunOpts(
                    prompt="hang please",
                    cwd=tmp_path,
                    output_schema=Answer,
                    executable=HANGS_IGNORING_SIGTERM_CLI,
                )
            )
        )

        assert await _poll_until(pid_file.exists), "grandchild never wrote its pid file"
        grandchild_pid = _read_grandchild_pid(pid_file)
        assert _pid_alive(grandchild_pid), "grandchild should be alive before cancellation"

        started = time.monotonic()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        elapsed = time.monotonic() - started

        assert await _poll_until(lambda: not _pid_alive(grandchild_pid)), (
            "grandchild survived cancellation of run()"
        )
        return elapsed

    elapsed = asyncio.run(scenario())

    # Well under a second proves the SIGKILL escalation fired - the fake ignores
    # SIGTERM entirely, so surviving on SIGTERM alone would mean waiting out the
    # unpatched multi-second grace/kill deadlines instead.
    assert elapsed < 2.0, f"cancellation took {elapsed}s; SIGKILL escalation likely did not fire"
