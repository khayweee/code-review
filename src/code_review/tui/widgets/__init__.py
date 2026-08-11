"""The TUI's widget tree: the Pipeline box, the Findings box, and the Status box.

- One component per module (React-style); this barrel re-exports the full public (and
  private-but-imported-by-tests) surface so `from code_review.tui.widgets import X` keeps
  resolving regardless of which submodule now defines `X`.
- Dependency direction: `styles`/`base` have no internal deps; `pipeline_box`/
  `findings_description` depend on `styles`; `findings_suggestion` is standalone;
  `finding` depends on `findings_description`+`findings_suggestion`;
  `findings_list_view` depends on `finding` (and, type-only, `findings_list`);
  `findings_list` depends on `finding`+`findings_list_view`; `status_box` depends on
  `base`.
- See `tui/AGENTS.md`'s "Findings box" section for the full design rationale behind the
  Findings box's widget tree and per-finding decision model.
"""

from __future__ import annotations

from code_review.tui.widgets.base import _BorderedBox
from code_review.tui.widgets.finding import Finding
from code_review.tui.widgets.findings_description import (
    FindingsDescription,
    format_finding,
    render_description,
)
from code_review.tui.widgets.findings_list import (
    _FOOTER_HINT,
    FindingsList,
    _findings_of,
    _findings_summary,
)
from code_review.tui.widgets.findings_list_view import _FindingsListView
from code_review.tui.widgets.findings_suggestion import (
    _CUSTOM_ENTRY,
    FindingsSuggestion,
    _decision_entries,
    _render_decision_entry,
    render_custom_entry_line,
    render_decision_cycle,
    render_decision_cycle_head,
    render_suggestions_plain,
)
from code_review.tui.widgets.pipeline_box import (
    _SHIMMER_BASE_LIGHTNESS,
    _SHIMMER_PEAK_LIGHTNESS,
    PipelineBox,
    _render_row,
    format_activity_row,
    format_duration,
    format_row,
    gradient_text,
    render_rows,
    render_rows_live,
)
from code_review.tui.widgets.status_box import StatusBox
from code_review.tui.widgets.styles import (
    _ACTIVITY_STYLE,
    _DECISION_MARKER_ICONS,
    _DECISION_MARKER_STYLES,
    _DOT_ICON,
    _SEVERITY_DOT_STYLES,
    _STATUS_DOT_STYLES,
    _STATUS_ICONS,
)

__all__ = [
    "Finding",
    "FindingsDescription",
    "FindingsList",
    "FindingsSuggestion",
    "PipelineBox",
    "StatusBox",
    "_ACTIVITY_STYLE",
    "_BorderedBox",
    "_CUSTOM_ENTRY",
    "_DECISION_MARKER_ICONS",
    "_DECISION_MARKER_STYLES",
    "_DOT_ICON",
    "_FOOTER_HINT",
    "_FindingsListView",
    "_SEVERITY_DOT_STYLES",
    "_SHIMMER_BASE_LIGHTNESS",
    "_SHIMMER_PEAK_LIGHTNESS",
    "_STATUS_DOT_STYLES",
    "_STATUS_ICONS",
    "_decision_entries",
    "_findings_of",
    "_findings_summary",
    "_render_decision_entry",
    "_render_row",
    "format_activity_row",
    "format_duration",
    "format_finding",
    "format_row",
    "gradient_text",
    "render_custom_entry_line",
    "render_decision_cycle",
    "render_decision_cycle_head",
    "render_description",
    "render_rows",
    "render_rows_live",
    "render_suggestions_plain",
]
