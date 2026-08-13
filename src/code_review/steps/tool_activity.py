"""Shared tool-call/narration activity reporting for any step that streams an agent call.

`tool_activity_label`/`tool_stream_relay` were originally `steps/review.py`-private
(`_tool_activity_label`/`_tool_stream_relay`); moved here once `steps/test_sufficiency.py`
needed the identical behavior, so neither step duplicates it.

`tool_stream_relay` builds an `on_stream_event` callback for `agent.RunOpts`: each
`StreamEvent` (see `agent/streaming.py`) is a single point-in-time callback, not a block of
work, so it reports through `pipeline.step.log_activity` (one-shot) rather than
`report_activity` (open/close pairing) -- no `AsyncExitStack`/tool-id bookkeeping needed.
`log_activity` is also null-safe, so `reporter` (`None` when no `ActivityReporter` is
attached) needs no branch here. Three `StreamEventType`s are handled: `TOOL_USE` (the call
itself), an errored `TOOL_RESULT` (a second, distinct activity beyond the call), and
`ASSISTANT_TEXT` (the model's own streamed prose, rendered via `assistant_text_label`).
`THINKING` is deliberately not surfaced -- extended-thinking blocks are verbose internal
scratch reasoning, out of scope for an activity log meant to stay skimmable.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from code_review.agent.streaming import StreamEvent, StreamEventType
from code_review.pipeline.step import ActivityReporter, log_activity

_ASSISTANT_TEXT_MAX_LEN = 160


def tool_activity_label(tool_name: str, tool_input: dict[str, Any]) -> str:
    """Render one tool call as an activity label, e.g. `Tool: Read(/path/to/file)`. Falls
    back to a bare `Tool: <name>` when none of the common single-argument shapes apply.
    """

    primary = (
        tool_input.get("file_path")
        or tool_input.get("command")
        or tool_input.get("pattern")
        or tool_input.get("path")
    )
    return f"Tool: {tool_name}({primary})" if primary else f"Tool: {tool_name}"


def assistant_text_label(content: str) -> str:
    """Render one streamed assistant-text block as an activity label, e.g.
    `Agent: Let me check the auth module for a missing nil check` -- collapsed to its
    first line and truncated so one log line never sprawls across the box.
    """

    first_line = content.strip().splitlines()[0] if content.strip() else ""
    if len(first_line) > _ASSISTANT_TEXT_MAX_LEN:
        first_line = first_line[: _ASSISTANT_TEXT_MAX_LEN - 1].rstrip() + "…"
    return f"Agent: {first_line}" if first_line else "Agent: (no text)"


def tool_stream_relay(
    reporter: ActivityReporter | None,
) -> Callable[[StreamEvent], Awaitable[None]]:
    """Build an `on_stream_event` callback that logs each tool call and assistant-text block
    as a one-shot `reporter.log(...)` event: one on `TOOL_USE` (the call itself), one more on
    an errored `TOOL_RESULT` (`payload["is_error"]` truthy) describing the failure, and one
    on `ASSISTANT_TEXT` (the model's own streamed narration, via `assistant_text_label`). A
    non-error `TOOL_RESULT` is not logged -- the matching `TOOL_USE` already reported the
    call, and a successful result adds nothing beyond that.
    """

    async def relay(event: StreamEvent) -> None:
        if event.type is StreamEventType.TOOL_USE:
            tool_input = event.payload.get("input") or {}
            label = tool_activity_label(event.payload["tool_name"], tool_input)
            await log_activity(reporter, label)
        elif event.type is StreamEventType.TOOL_RESULT and event.payload.get("is_error"):
            await log_activity(reporter, f"Tool error: {event.payload.get('output')}")
        elif event.type is StreamEventType.ASSISTANT_TEXT:
            await log_activity(reporter, assistant_text_label(event.payload.get("content", "")))

    return relay
