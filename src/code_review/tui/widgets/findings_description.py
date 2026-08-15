"""The left column of one `Finding` row: severity dot, description, location.

Renders on every row (unlike `FindingsSuggestion`, which only shows for the highlighted
row). While parked, also shows a decided-marker prefix so a row's recorded decision
stays visible after browsing away from it.
"""

from __future__ import annotations

from pathlib import Path

from rich.text import Text
from textual.widgets import Static

from code_review.pipeline.findings import Finding as FindingData
from code_review.pipeline.schemas import ApprovalDecision
from code_review.tui.widgets.styles import (
    _DECISION_MARKER_ICONS,
    _DECISION_MARKER_STYLES,
    _DOT_ICON,
    _SEVERITY_DOT_STYLES,
)


def format_finding(finding: FindingData) -> str:
    """Render one `Finding` as `<severity>: <description>`, with ` (<location>)` appended
    only when `finding.location` is not `None`."""

    location = "" if finding.location is None else f" ({finding.location})"
    return f"{finding.severity}: {finding.description}{location}"


def render_description(finding: FindingData, decision: ApprovalDecision | None = None) -> Text:
    """`FindingsDescription`'s content: severity dot, then `format_finding`'s text.

    `decision` is this row's recorded park decision; `None` (the default) renders no
    marker, "fix"/"skip" prefix a small marker so a decided row is visually distinct.
    """

    text = Text()
    if decision is not None:
        icon = _DECISION_MARKER_ICONS.get(decision)
        if icon is not None:
            text.append(f"{icon} ", style=_DECISION_MARKER_STYLES[decision])
    text.append(_DOT_ICON, style=_SEVERITY_DOT_STYLES[finding.severity])
    text.append(f" {format_finding(finding)}")
    return text


class FindingsDescription(Static):
    """The left column of one `Finding` row: severity dot, description, location.

    `width: 1fr` matches `FindingsSuggestion`'s own `1fr` for an even 50/50 split. No
    `border-right` divider -- `FindingsSuggestion` draws its own full border when visible.
    """

    DEFAULT_CSS = (
        Path(__file__).with_name("tokens.tcss").read_text()
        + "\n"
        + Path(__file__).with_suffix(".tcss").read_text()
    )

    def __init__(
        self,
        finding: FindingData,
        *,
        id: str | None = None,  # noqa: A002 -- matches Textual's own Widget.__init__ shape
        classes: str | None = None,
    ) -> None:
        super().__init__(render_description(finding), id=id, classes=classes)

    def update_finding(
        self, finding: FindingData, decision: ApprovalDecision | None = None
    ) -> None:
        self.update(render_description(finding, decision))
