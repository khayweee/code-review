"""Live streaming display of agent execution for TUI.

Consumes StreamEvent callbacks and renders tool calls/results in real time.
Integrates with Textual app for live display during pipeline runs.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from textual.message import Message
from textual.widgets import Static

if TYPE_CHECKING:
    from code_review.agent.streaming import StreamEvent


@dataclass
class StreamEventMessage(Message):
    """Textual message carrying a StreamEvent for the app to process."""

    event: StreamEvent


class StreamRelay:
    """Relays StreamEvents to a Textual app's message queue for live display.

    Satisfies the Callable[[StreamEvent], Awaitable[None]] protocol expected by
    RunOpts.on_stream_event. Call attach(app) to wire this into a Textual app.
    """

    def __init__(self) -> None:
        self._app: object | None = None

    def attach(self, app: object) -> None:
        """Attach this relay to a Textual app instance."""
        self._app = app

    async def __call__(self, event: Any) -> None:
        """Emit a StreamEvent to the app's message queue."""
        if self._app is not None:
            # Import here to avoid circular deps at module level
            from textual.app import App

            if isinstance(self._app, App):
                self._app.post_message(StreamEventMessage(event))


class StreamViewer(Static):
    """Textual widget that displays live StreamEvents in a scrollable view.

    Updates in real time as tool calls and results arrive.
    """

    DEFAULT_CSS = """
    StreamViewer {
        height: 1fr;
        border: solid $primary;
        background: $surface;
        color: $text;
    }
    """

    def __init__(self) -> None:
        super().__init__()
        self._events: list[Any] = []

    def handle_stream_event(self, message: StreamEventMessage) -> None:
        """Handle a StreamEventMessage by adding it to the display."""
        from code_review.agent.streaming import StreamEventType

        event = message.event
        self._events.append(event)

        # Keep only last 20 events
        if len(self._events) > 20:
            self._events.pop(0)

        self._update_display()

    def _update_display(self) -> None:
        """Build and render the event display."""
        from code_review.agent.streaming import StreamEventType

        lines: list[str] = []

        for evt in self._events:
            if evt.type == StreamEventType.TOOL_USE:
                tool_name = evt.payload.get("tool_name", "unknown")
                input_keys = list(evt.payload.get("input", {}).keys())
                tool_id = str(evt.payload.get("tool_id", ""))[:8]
                lines.append(f"🔧 {tool_name}({', '.join(input_keys)}) [{tool_id}]")

            elif evt.type == StreamEventType.TOOL_RESULT:
                output = str(evt.payload.get("output", ""))[:80]
                is_error = evt.payload.get("is_error", False)
                icon = "❌" if is_error else "✓"
                lines.append(f"{icon} Result: {output}")

            elif evt.type == StreamEventType.ASSISTANT_TEXT:
                content = str(evt.payload.get("content", ""))[:100]
                lines.append(f"💬 {content}")

            elif evt.type == StreamEventType.THINKING:
                content = str(evt.payload.get("content", ""))[:80]
                lines.append(f"🧠 Thinking: {content}")

            elif evt.type == StreamEventType.ERROR:
                msg = str(evt.payload.get("message", "Unknown error"))
                lines.append(f"⚠️  Error: {msg}")

        self.update("\n".join(lines) if lines else "(no events yet)")
