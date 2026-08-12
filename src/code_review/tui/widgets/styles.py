"""Shared icon/color constants (plain data only, no widget logic) for the widgets
package. `tokens.tcss` is the palette of record -- every hex value below must match its
`tokens.tcss` counterpart exactly (Rich style strings can't reference Textual CSS
variables at runtime, so these stay separate literal constants)."""

from __future__ import annotations

from code_review.pipeline.step import ApprovalDecision
from code_review.tui.state import Status

# Text fallback glyph per status; the live pipeline view uses a Rich Spinner for "running".
_STATUS_ICONS: dict[Status, str] = {
    "pending": "◌",
    "running": "◔",
    "completed": "✔",
    "failed": "✘",
    "parked": "⏸",
    "skipped": "⏭",
}

_ACTIVITY_STYLE = "#949494"  # tokens.tcss's $fg-secondary

# Live pipeline view only: completed/failed rows render this dot, colored by status,
# instead of the fallback glyph above. Values match tokens.tcss's $status-* tokens.
_DOT_ICON = "●"
_STATUS_DOT_STYLES: dict[Status, str] = {
    "completed": "#5fafff",
    "failed": "#bb6400",
    "parked": "#d7af00",
    "skipped": "#8a8a8a",
}

_SEVERITY_DOT_STYLES: dict[str, str] = {
    "error": "#bb6400",
    "warning": "#d7af00",
    "info": "#5fafff",
}

# Only "fix"/"skip" are keyed; any other decision renders with no marker.
_DECISION_MARKER_ICONS: dict[ApprovalDecision, str] = {
    "fix": _STATUS_ICONS["completed"],
    "skip": _STATUS_ICONS["skipped"],
}
_DECISION_MARKER_STYLES: dict[ApprovalDecision, str] = {
    "fix": _STATUS_DOT_STYLES["completed"],
    "skip": _STATUS_DOT_STYLES["skipped"],
}
