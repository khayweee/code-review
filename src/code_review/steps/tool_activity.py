"""Shared tool-call/narration activity reporting for any step that streams an agent call.

`tool_activity_label`/`tool_stream_relay` were originally `steps/review.py`-private
(`_tool_activity_label`/`_tool_stream_relay`); moved here once `steps/test_sufficiency.py`
needed the identical behavior, so neither step duplicates it.

`tool_stream_relay` builds an `on_stream_event` callback for `agent.RunOpts`: a tool call's
real duration is the span between its `TOOL_USE` and matching `TOOL_RESULT` `StreamEvent`
(see `agent/streaming.py`), correlated by `payload["tool_id"]`, so it opens a real activity
span on `TOOL_USE` via `pipeline.step.start_activity` and closes it on the matching
`TOOL_RESULT` via `finish_activity`, tracking open calls in a local `tool_id -> (activity_id,
label)` dict for the lifetime of one streamed agent call. An errored `TOOL_RESULT`
(`payload["is_error"]` truthy) closes that same span with `error` set, mirroring
`ActivityHandle.fail(detail)`'s existing convention elsewhere (e.g. `run_git`), rather than
logging a second, distinct activity. `start_activity`/`finish_activity` are null-safe, so
`reporter` (`None` when no `ActivityReporter` is attached) needs no branch here.
`ASSISTANT_TEXT` (the model's own streamed prose) stays one-shot via `log_activity`, since
narration has no natural duration to track. `THINKING` is deliberately not surfaced --
extended-thinking blocks are verbose internal scratch reasoning, out of scope for an
activity log meant to stay skimmable.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from code_review.agent.streaming import StreamEvent, StreamEventType
from code_review.pipeline.step import (
    ActivityReporter,
    finish_activity,
    log_activity,
    start_activity,
)


def _relative_to_cwd(value: str, cwd: Path | None) -> str:
    """Strip `cwd` (the review worktree's root, e.g.
    `.../worktrees/code_review_<branch>_<sha>`) off the front of `value` so activity-log
    lines read as `src/code_review/...` instead of the full worktree path. Left unchanged
    when `cwd` is unset or `value` isn't rooted under it (e.g. `command` strings, which
    aren't paths).
    """

    if cwd is None:
        return value
    prefix = f"{cwd}/"
    return value[len(prefix) :] if value.startswith(prefix) else value


def tool_activity_label(tool_name: str, tool_input: dict[str, Any], cwd: Path | None = None) -> str:
    """Render one tool call as an activity label, e.g. `Tool: Read(src/code_review/cli.py)`
    -- absolute paths are relativized to `cwd` (the worktree root) so the log stays
    readable. Falls back to a bare `Tool: <name>` when none of the common single-argument
    shapes apply.
    """

    primary = (
        tool_input.get("file_path")
        or tool_input.get("command")
        or tool_input.get("pattern")
        or tool_input.get("path")
    )
    if primary:
        primary = _relative_to_cwd(primary, cwd)
    return f"Tool: {tool_name}({primary})" if primary else f"Tool: {tool_name}"


def assistant_text_label(content: str) -> str:
    """Render one streamed assistant-text block as an activity label, e.g.
    `Agent: Let me check the auth module for a missing nil check` -- collapsed to its
    first line (any further lines are the model's own continued narration, reported as
    later, separate `ASSISTANT_TEXT` events instead). Not truncated: the Pipeline box
    renders this through Rich's default word-wrap, so a long line wraps onto a
    continuation line in the box rather than being cut off with an ellipsis.
    """

    first_line = content.strip().splitlines()[0] if content.strip() else ""
    return f"Agent: {first_line}" if first_line else "Agent: (no text)"


def tool_stream_relay(
    reporter: ActivityReporter | None,
    cwd: Path | None = None,
) -> Callable[[StreamEvent], Awaitable[None]]:
    """Build an `on_stream_event` callback that reports each tool call as a real activity
    span: `start_activity` on `TOOL_USE`, `finish_activity` on the matching `TOOL_RESULT`
    (correlated by `payload["tool_id"]`), so the reported duration is the real elapsed time
    between the two, not an instant one-shot event. An errored `TOOL_RESULT`
    (`payload["is_error"]` truthy) finishes that same span with `error` set instead of
    logging a second, distinct activity. `ASSISTANT_TEXT` (the model's own streamed
    narration) stays a one-shot `log_activity` event, via `assistant_text_label`. `cwd` (the
    step's worktree root) is threaded into `tool_activity_label` so `TOOL_USE` paths log
    relative to it.
    """

    open_calls: dict[str, tuple[int, str]] = {}

    async def relay(event: StreamEvent) -> None:
        if event.type is StreamEventType.TOOL_USE:
            tool_input = event.payload.get("input") or {}
            label = tool_activity_label(event.payload["tool_name"], tool_input, cwd)
            activity_id = await start_activity(reporter, label)
            if activity_id is not None:
                open_calls[event.payload["tool_id"]] = (activity_id, label)
        elif event.type is StreamEventType.TOOL_RESULT:
            open_call = open_calls.pop(event.payload["tool_id"], None)
            if open_call is not None:
                activity_id, label = open_call
                error = f"{event.payload.get('output')}" if event.payload.get("is_error") else None
                await finish_activity(reporter, activity_id, label, error=error)
        elif event.type is StreamEventType.ASSISTANT_TEXT:
            await log_activity(reporter, assistant_text_label(event.payload.get("content", "")))

    return relay
