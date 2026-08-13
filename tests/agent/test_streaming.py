"""Tests for the streaming (`--output-format stream-json`) call path: `_parse_stream_line`
against real stream-json line shapes, and `ClaudeCLI.run`/`_run_streaming` end to end
against `STREAMING_TOOL_CALL_FAKE_CLI`, a fake CLI that emits a real NDJSON transcript
(tool_use, its matching tool_result, then the final `"result"` line).

`StreamEvent`/`StreamEventType` themselves are plain frozen dataclasses/enums with no
behavior of their own -- not worth testing in isolation; what matters is that a real
stream-json transcript parses into the right sequence of `StreamEvent`s and that the
final structured output/usage still come through correctly.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from pydantic import BaseModel

from code_review.agent.base import RunOpts
from code_review.agent.claude_cli import ClaudeCLI, _parse_stream_line
from code_review.agent.streaming import StreamEvent, StreamEventType

FAKES = Path(__file__).parent / "fakes"
STREAMING_TOOL_CALL_FAKE_CLI = FAKES / "streaming_tool_call.py"


class SimpleOutput(BaseModel):
    answer: str


# --- _parse_stream_line ------------------------------------------------------------------


def test_parse_stream_line_reads_a_tool_use_block() -> None:
    line = {
        "type": "assistant",
        "message": {
            "content": [
                {"type": "tool_use", "id": "tool_1", "name": "Read", "input": {"file_path": "/x"}}
            ]
        },
    }

    event = _parse_stream_line(line, session_id="sess_1")

    assert event is not None
    assert event.type is StreamEventType.TOOL_USE
    assert event.payload == {"tool_name": "Read", "tool_id": "tool_1", "input": {"file_path": "/x"}}
    assert event.session_id == "sess_1"


def test_parse_stream_line_reads_a_matching_tool_result() -> None:
    line = {
        "type": "user",
        "message": {
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "tool_1",
                    "content": "file contents",
                    "is_error": False,
                }
            ]
        },
    }

    event = _parse_stream_line(line, session_id="sess_1")

    assert event is not None
    assert event.type is StreamEventType.TOOL_RESULT
    assert event.payload == {
        "tool_id": "tool_1",
        "output": "file contents",
        "is_error": False,
    }


def test_parse_stream_line_ignores_a_tool_results_top_level_parent_tool_use_id() -> None:
    """A `tool_result` block correlates via its own `tool_use_id`, not the enclosing
    message's `parent_tool_use_id` -- that field names the tool call a *subagent* is
    nested under, if any, which is unrelated to which tool this result answers."""

    line = {
        "type": "user",
        "parent_tool_use_id": "some_other_tool",
        "message": {
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "tool_1",
                    "content": "ok",
                    "is_error": False,
                }
            ]
        },
    }

    event = _parse_stream_line(line, session_id=None)

    assert event is not None
    assert event.payload["tool_id"] == "tool_1"


def test_parse_stream_line_returns_none_for_an_uninteresting_line() -> None:
    assert _parse_stream_line({"type": "system"}, session_id=None) is None


# --- ClaudeCLI.run against a real stream-json transcript ----------------------------------


def test_streaming_run_emits_tool_use_then_tool_result_before_returning(tmp_path: Path) -> None:
    events: list[StreamEvent] = []

    async def on_stream_event(event: StreamEvent) -> None:
        events.append(event)

    async def _run() -> None:
        adapter = ClaudeCLI()
        try:
            result = await adapter.run(
                RunOpts(
                    prompt="test",
                    cwd=tmp_path,
                    output_schema=SimpleOutput,
                    executable=STREAMING_TOOL_CALL_FAKE_CLI,
                    on_stream_event=on_stream_event,
                )
            )
            assert isinstance(result.output, SimpleOutput)
            assert result.usage is not None
        finally:
            await adapter.close()

    asyncio.run(_run())

    assert [e.type for e in events] == [StreamEventType.TOOL_USE, StreamEventType.TOOL_RESULT]
    assert events[0].payload["tool_id"] == events[1].payload["tool_id"] == "tool_1"


def test_streaming_run_without_a_callback_uses_the_legacy_json_path(tmp_path: Path) -> None:
    """`on_stream_event=None` must stay on `claude_cli.py`'s legacy `--output-format json`
    path -- `STREAMING_TOOL_CALL_FAKE_CLI` only emits stream-json, so a call reaching it
    without streaming args would fail to parse a structured answer."""

    async def _run() -> None:
        adapter = ClaudeCLI()
        try:
            result = await adapter.run(
                RunOpts(
                    prompt="test",
                    cwd=tmp_path,
                    output_schema=SimpleOutput,
                    executable=FAKES / "valid_output.py",
                    on_stream_event=None,
                )
            )
            assert isinstance(result.output, SimpleOutput)
        finally:
            await adapter.close()

    asyncio.run(_run())
