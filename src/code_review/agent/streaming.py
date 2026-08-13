"""Stream event types and utilities for observable LLM execution.

Decoupled from the Agent backend so any LLM provider can emit compatible events.
Consumed by TUI, test observers, or other live-display systems.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class StreamEventType(Enum):
    """Observable event types emitted during an LLM call execution."""

    TOOL_USE = "tool_use"  # Agent called a tool
    TOOL_RESULT = "tool_result"  # Tool result received
    ASSISTANT_TEXT = "assistant_text"  # Final assistant text response
    THINKING = "thinking"  # Extended thinking blocks
    ERROR = "error"  # Tool execution error


@dataclass(frozen=True)
class StreamEvent:
    """A single observable moment in an LLM execution.

    Emitted as the agent runs; consumed by TUI or other observers for live display.
    """

    type: StreamEventType
    # Payload structure varies by type:
    # TOOL_USE: {tool_name: str, tool_id: str, input: dict}
    # TOOL_RESULT: {tool_id: str, output: str, is_error: bool}
    # ASSISTANT_TEXT: {content: str}
    # THINKING: {content: str}
    # ERROR: {tool_id: str | None, message: str}
    payload: dict[str, Any]
    timestamp: float  # time.time() when event occurred
    session_id: str | None = None  # backend session identifier if available
