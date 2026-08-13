"""Tests for streaming event emission during agent calls.

Verifies that StreamEvents are correctly emitted and can be consumed by
observers (TUI, test code, etc.).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from pydantic import BaseModel

from code_review.agent.base import RunOpts
from code_review.agent.claude_cli import ClaudeCLI
from code_review.agent.streaming import StreamEvent, StreamEventType

FAKES = Path(__file__).parent / "fakes"
FAKE_CLI = FAKES / "valid_output.py"


class SimpleOutput(BaseModel):
    """Minimal output schema for testing."""

    answer: str


def test_streaming_backward_compatible_without_callback(tmp_path: Path) -> None:
    """Verify that agent calls work normally when on_stream_event is None."""

    async def _run() -> None:
        adapter = ClaudeCLI()

        try:
            result = await adapter.run(
                RunOpts(
                    prompt="test",
                    cwd=tmp_path,
                    output_schema=SimpleOutput,
                    executable=FAKE_CLI,
                    on_stream_event=None,  # Explicitly no streaming
                )
            )

            # Should work fine without streaming callback
            assert isinstance(result.output, SimpleOutput)
            assert result.usage is not None

        finally:
            await adapter.close()

    asyncio.run(_run())


def test_stream_event_structure() -> None:
    """Verify that StreamEvent instances have the correct structure."""

    # Create a sample event
    event = StreamEvent(
        type=StreamEventType.TOOL_USE,
        payload={"tool_name": "Read", "tool_id": "tool_1", "input": {"file_path": "/tmp/test"}},
        timestamp=1234567890.0,
        session_id="sess_123",
    )

    # Verify all fields
    assert event.type == StreamEventType.TOOL_USE
    assert event.payload["tool_name"] == "Read"
    assert event.timestamp == 1234567890.0
    assert event.session_id == "sess_123"

    # Verify frozen
    with pytest.raises(AttributeError):
        event.timestamp = 9999.0  # type: ignore


def test_all_stream_event_types() -> None:
    """Verify all StreamEventType values exist and can be instantiated."""

    for event_type in StreamEventType:
        event = StreamEvent(
            type=event_type,
            payload={},
            timestamp=0.0,
        )
        assert event.type == event_type
