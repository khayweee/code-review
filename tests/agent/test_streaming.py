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
import hashlib
from pathlib import Path

from pydantic import BaseModel

from code_review.agent.base import RunOpts
from code_review.agent.claude_cli import ClaudeCLI, _parse_stream_line, _read_stream_line
from code_review.agent.streaming import StreamEvent, StreamEventType

FAKES = Path(__file__).parent / "fakes"
STREAMING_TOOL_CALL_FAKE_CLI = FAKES / "streaming_tool_call.py"
STREAMING_TWO_TOOL_CALLS_FAKE_CLI = FAKES / "streaming_two_tool_calls.py"
STREAMING_LARGE_TOOL_RESULT_FAKE_CLI = FAKES / "streaming_large_tool_result.py"

# Must match the payload streaming_large_tool_result.py emits.
LARGE_TOOL_RESULT_PAYLOAD = hashlib.sha256(b"large tool result").hexdigest() * 8000


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

    events = _parse_stream_line(line, session_id="sess_1")

    assert len(events) == 1
    event = events[0]
    assert event.type is StreamEventType.TOOL_USE
    assert event.payload == {"tool_name": "Read", "tool_id": "tool_1", "input": {"file_path": "/x"}}
    assert event.session_id == "sess_1"


def test_parse_stream_line_reads_two_parallel_tool_use_blocks() -> None:
    """Claude routinely emits multiple `tool_use` blocks in one turn (parallel tool
    calls); every block must surface as its own event, in order -- not just the first."""

    line = {
        "type": "assistant",
        "message": {
            "content": [
                {"type": "tool_use", "id": "tool_1", "name": "Read", "input": {"file_path": "/x"}},
                {
                    "type": "tool_use",
                    "id": "tool_2",
                    "name": "Grep",
                    "input": {"pattern": "foo"},
                },
            ]
        },
    }

    events = _parse_stream_line(line, session_id="sess_1")

    assert len(events) == 2
    assert [e.type for e in events] == [StreamEventType.TOOL_USE, StreamEventType.TOOL_USE]
    assert events[0].payload == {
        "tool_name": "Read",
        "tool_id": "tool_1",
        "input": {"file_path": "/x"},
    }
    assert events[1].payload == {
        "tool_name": "Grep",
        "tool_id": "tool_2",
        "input": {"pattern": "foo"},
    }


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

    events = _parse_stream_line(line, session_id="sess_1")

    assert len(events) == 1
    event = events[0]
    assert event.type is StreamEventType.TOOL_RESULT
    assert event.payload == {
        "tool_id": "tool_1",
        "output": "file contents",
        "is_error": False,
    }


def test_parse_stream_line_reads_two_parallel_tool_results() -> None:
    """The Claude CLI bundles results for parallel `tool_use` calls into a single
    subsequent `"user"`-type message with multiple `tool_result` blocks, one per
    tool_use id, in the same order -- every block must surface as its own event."""

    line = {
        "type": "user",
        "message": {
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "tool_1",
                    "content": "file contents",
                    "is_error": False,
                },
                {
                    "type": "tool_result",
                    "tool_use_id": "tool_2",
                    "content": "grep matches",
                    "is_error": True,
                },
            ]
        },
    }

    events = _parse_stream_line(line, session_id="sess_1")

    assert len(events) == 2
    assert [e.type for e in events] == [StreamEventType.TOOL_RESULT, StreamEventType.TOOL_RESULT]
    assert events[0].payload == {
        "tool_id": "tool_1",
        "output": "file contents",
        "is_error": False,
    }
    assert events[1].payload == {
        "tool_id": "tool_2",
        "output": "grep matches",
        "is_error": True,
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

    events = _parse_stream_line(line, session_id=None)

    assert len(events) == 1
    assert events[0].payload["tool_id"] == "tool_1"


def test_parse_stream_line_returns_an_empty_list_for_an_uninteresting_line() -> None:
    assert _parse_stream_line({"type": "system"}, session_id=None) == []


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


def test_streaming_run_emits_both_of_two_parallel_tool_calls(tmp_path: Path) -> None:
    """Issue #132: Claude routinely emits multiple `tool_use` blocks in one turn
    (parallel tool calls), whose results the CLI then bundles into a single subsequent
    `"user"`-type line with multiple `tool_result` blocks. Both calls -- not just the
    first -- must surface as `StreamEvent`s, end to end through `ClaudeCLI.run`."""

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
                    executable=STREAMING_TWO_TOOL_CALLS_FAKE_CLI,
                    on_stream_event=on_stream_event,
                )
            )
            assert isinstance(result.output, SimpleOutput)
        finally:
            await adapter.close()

    asyncio.run(_run())

    assert [e.type for e in events] == [
        StreamEventType.TOOL_USE,
        StreamEventType.TOOL_USE,
        StreamEventType.TOOL_RESULT,
        StreamEventType.TOOL_RESULT,
    ]
    assert events[0].payload["tool_id"] == "tool_1"
    assert events[1].payload["tool_id"] == "tool_2"
    assert events[2].payload["tool_id"] == "tool_1"
    assert events[3].payload["tool_id"] == "tool_2"


def test_streaming_run_survives_a_line_longer_than_the_stream_reader_limit(
    tmp_path: Path,
) -> None:
    """A stream-json line carrying a big tool result (a whole-file `Read`, a wide `Edit`)
    blows past asyncio's 64 KiB `StreamReader` limit. `readline()` answers that by
    dropping the line and raising `ValueError("Separator is found, but chunk is longer
    than limit")`, which aborted the whole review mid-run; `_read_stream_line` must
    reassemble it byte-for-byte and keep the stream in sync for the lines after it."""

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
                    executable=STREAMING_LARGE_TOOL_RESULT_FAKE_CLI,
                    on_stream_event=on_stream_event,
                )
            )
            assert isinstance(result.output, SimpleOutput)
        finally:
            await adapter.close()

    asyncio.run(_run())

    assert [e.type for e in events] == [StreamEventType.TOOL_USE, StreamEventType.TOOL_RESULT]
    # Exact payload, not just its length: a dropped or reordered chunk must not pass.
    assert events[1].payload["output"] == LARGE_TOOL_RESULT_PAYLOAD
    # The oversized line left the reader in sync, so the `"result"` line after it parsed.


def test_read_stream_line_reassembles_both_limit_overrun_shapes() -> None:
    """`readuntil` reports an over-limit line two different ways -- "Separator is found,
    but chunk is longer than limit" when the newline is already buffered past the limit,
    and "Separator is not found, and chunk exceed the limit" when it hasn't arrived yet.
    Feeding the line in one shot vs. in pieces exercises one branch each."""

    async def _run() -> tuple[bytes, bytes]:
        buffered = asyncio.StreamReader()
        buffered.feed_data(b"x" * 200_000 + b"\n")  # newline present: "Separator is found"
        buffered.feed_eof()

        piecewise = asyncio.StreamReader()
        for _ in range(4):
            piecewise.feed_data(b"y" * 50_000)  # no newline yet: "Separator is not found"
        piecewise.feed_data(b"\n")
        piecewise.feed_eof()

        return await _read_stream_line(buffered), await _read_stream_line(piecewise)

    assert asyncio.run(_run()) == (b"x" * 200_000 + b"\n", b"y" * 200_000 + b"\n")


def test_read_stream_line_returns_empty_bytes_at_eof() -> None:
    """EOF is the streaming loop's only stop condition, so `_read_stream_line` must keep
    reporting it as `b""` -- including for a final line with no trailing newline."""

    async def _run() -> list[bytes]:
        stream = asyncio.StreamReader()
        stream.feed_data(b"first\nunterminated")
        stream.feed_eof()
        return [await _read_stream_line(stream) for _ in range(3)]

    assert asyncio.run(_run()) == [b"first\n", b"unterminated", b""]


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
