"""Shared icon/color constants for the widgets package.

- No widget logic lives here, only plain data (`dict`s of style strings).
- `_STATUS_ICONS`/`_STATUS_DOT_STYLES`/`_DOT_ICON`/`_ACTIVITY_STYLE` are used by
  `pipeline_box.py` for the pipeline's per-step status glyphs/colors.
- `_SEVERITY_DOT_STYLES`/`_DECISION_MARKER_ICONS`/`_DECISION_MARKER_STYLES` are used by
  `findings_description.py` for the per-finding risk dot and decided marker.
- `_DECISION_MARKER_ICONS`/`_STYLES` are derived from `_STATUS_ICONS`/`_STATUS_DOT_STYLES`,
  so they are defined below those in this same module.
"""

from __future__ import annotations

from code_review.pipeline.step import ApprovalDecision
from code_review.tui.state import Status

# One glyph per status in the deterministic text fallback. The live pipeline view uses
# a Rich spinner renderable for the running state so it can animate without any manual
# frame cycling in this module.
_STATUS_ICONS: dict[Status, str] = {
    "pending": "◌",  # hollow ring: not started yet
    "running": "◔",  # quarter-filled glyph: fallback only; live view uses Spinner
    "completed": "✔",  # check mark: finished successfully
    "failed": "✘",  # cross mark: raised before it could complete
    "parked": "⏸",  # pause: needs_approval=True, waiting on a human approve/skip/abort
    "skipped": "⏭",  # skip-forward: a human chose "skip" -- bypassed, not an error
}

# Mid-gray for activity lines, so they read as subordinate detail beneath their step.
_ACTIVITY_STYLE = "grey58"

# Live pipeline view only: a completed/failed row's icon renders as this solid dot, colored
# by status, instead of the fallback ✔/✘ glyph above.
_DOT_ICON = "●"
_STATUS_DOT_STYLES: dict[Status, str] = {
    "completed": "#5fafff",  # blue
    "failed": "#bb6400",  # orange
    "parked": "#d7af00",  # amber -- waiting on a human decision
    "skipped": "#8a8a8a",  # gray -- deliberately bypassed by a human, not an error
}

# `FindingsList`'s per-finding risk indicator: a colored `_DOT_ICON`, keyed by
# `Finding.severity`, reusing that existing field rather than a new one.
_SEVERITY_DOT_STYLES: dict[str, str] = {
    "error": "#bb6400",  # orange -- matches _STATUS_DOT_STYLES's "failed" color
    "warning": "#d7af00",  # amber -- matches _STATUS_DOT_STYLES's "parked" color
    "info": "#5fafff",  # blue -- matches _STATUS_DOT_STYLES's "completed" color
}

# `FindingsList`'s per-row decided marker, rendered by `render_description` on every row
# (not just the highlighted one) -- reuses this module's existing "completed"/"skipped"
# glyphs rather than inventing new ones. Only "fix"/"skip" ever key these; a value neither
# dict defines renders with no marker at all.
_DECISION_MARKER_ICONS: dict[ApprovalDecision, str] = {
    "fix": _STATUS_ICONS["completed"],
    "skip": _STATUS_ICONS["skipped"],
}
_DECISION_MARKER_STYLES: dict[ApprovalDecision, str] = {
    "fix": _STATUS_DOT_STYLES["completed"],
    "skip": _STATUS_DOT_STYLES["skipped"],
}
