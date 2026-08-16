"""Barrel module re-exporting this package's public (and test-imported) widgets and
helpers, so `from code_review.tui.widgets import X` resolves regardless of which
submodule defines `X`.
"""

from __future__ import annotations

from code_review.tui.widgets.base import _BorderedBox
from code_review.tui.widgets.Findings.finding import (
    _FOOTER_HINT,
    FindingBox,
    _findings_of,
    _findings_summary,
)
from code_review.tui.widgets.Findings.findings_description import (
    FindingExpandedDescription,
    FindingsDescription,
    FindingTitle,
    format_finding,
    render_description,
    render_title,
    truncate_to_one_line,
)
from code_review.tui.widgets.Findings.findings_list import Finding, _FindingsListView
from code_review.tui.widgets.Findings.findings_suggestion import (
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
    _SHIMMER_TICK_SECONDS,
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
    "FindingBox",
    "FindingExpandedDescription",
    "FindingsDescription",
    "FindingsSuggestion",
    "FindingTitle",
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
    "_SHIMMER_TICK_SECONDS",
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
    "render_title",
    "truncate_to_one_line",
]
