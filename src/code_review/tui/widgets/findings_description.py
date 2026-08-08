"""The left column of one `Finding` row: severity dot, description, location.

- `FindingsDescription` renders on every row, unlike `FindingsSuggestion` which only
  ever shows for the highlighted row.
- While parked, it also shows a decided-marker prefix (`render_description`'s `decision`
  parameter) so a human browsing away from a row they just decided still sees it recorded.
- `format_finding`/`render_description` are pure and unit-tested without Textual.
"""

from __future__ import annotations

from pathlib import Path

from rich.text import Text
from textual.widgets import Static

from code_review.pipeline.findings import Finding as FindingData
from code_review.pipeline.step import ApprovalDecision
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

    - A colored `_DOT_ICON` (`_SEVERITY_DOT_STYLES`, keyed by `finding.severity`) is the
      per-finding risk indicator, reusing `severity` rather than a new field.
    - `decision` is this row's own recorded park decision; `None` (the default) renders
      identically to before that feature existed, so every non-parked call site is unaffected.
    - When `decision` is "fix"/"skip", a small marker (`_DECISION_MARKER_ICONS`/`_STYLES`)
      is prefixed so a human can tell a decided row apart from an undecided one while
      browsing any row during a park, not just the highlighted one.
    - Any other `decision` value (or `None`) renders with no marker at all, matching this
      module's "no exceptions on data outside its documented shape" style.
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

    - `width: 1fr`, matched to `FindingsSuggestion`'s own `1fr`, so every row shares the
      same 50/50 split regardless of highlight state or description length.
    - A long description wraps within its half of the row via Textual's default word-wrap.
    - No `border-right` divider -- `FindingsSuggestion` draws its own full border when
      visible, which already marks the split.
    """

    DEFAULT_CSS = Path(__file__).with_suffix(".tcss").read_text()

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
