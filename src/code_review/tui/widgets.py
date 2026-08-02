"""The Pipeline box: one line per registry step, live status icon, elapsed/final duration.

Rendering-only. Every widget here takes the `StepRow`s it displays as plain data (see
`state.py`) -- it never reads a `StepEvent` stream or a registry itself. That split keeps
row rendering unit-testable via `render_rows` in isolation, and widget mounting/refresh
testable via Textual's `Pilot` (`tests/tui/test_widgets.py`), without needing a live event
stream either way.
"""

from __future__ import annotations

from collections.abc import Sequence

from textual.widgets import Static

from code_review.tui.state import Status, StepRow

# One glyph per status, chosen to be unambiguous at a glance in a live-updating terminal:
# a human reads this while it changes, so legibility beats cleverness. Kept as its own
# small table (rather than inline in the format function) so the mapping is easy to eyeball
# and to unit-test on its own.
_STATUS_ICONS: dict[Status, str] = {
    "pending": "○",  # ○ hollow circle: not started yet
    "running": "◐",  # ◐ half-filled circle: in progress
    "completed": "✓",  # ✓ check mark: finished successfully
    "failed": "✗",  # ✗ cross mark: raised before it could complete
}


def format_duration(duration: float) -> str:
    """Render a duration in seconds as `M:SS` once it reaches a minute, else `0.3s`."""

    if duration < 60:
        return f"{duration:.1f}s"
    minutes, seconds = divmod(int(duration), 60)
    return f"{minutes}:{seconds:02d}"


def format_row(row: StepRow) -> str:
    """Render one `StepRow` as `<icon> <name>  <duration>` (duration omitted while pending)."""

    icon = _STATUS_ICONS[row.status]
    duration = "" if row.duration is None else f"  {format_duration(row.duration)}"
    return f"{icon} {row.name}{duration}"


def render_rows(rows: Sequence[StepRow]) -> str:
    """Render every row as one line each, in the order given."""

    return "\n".join(format_row(row) for row in rows)


class PipelineBox(Static):
    """A bordered box listing every registered step and its current status."""

    DEFAULT_CSS = """
    PipelineBox {
        border: round $primary;
        padding: 0 1;
        height: auto;
    }
    """

    def __init__(
        self,
        rows: Sequence[StepRow] = (),
        *,
        id: str | None = None,  # noqa: A002 -- matches Textual's own Widget.__init__ shape
        classes: str | None = None,
    ) -> None:
        super().__init__(render_rows(rows), id=id, classes=classes)
        self.border_title = "Pipeline"

    def update_rows(self, rows: Sequence[StepRow]) -> None:
        """Replace the displayed rows with `rows`, re-rendered in order."""

        self.update(render_rows(rows))
