"""The left column of one `Finding` row: an always-visible one-line `FindingTitle` above
a `FindingExpandedDescription` that only shows the full, untruncated text while that row
is highlighted.

Split out of what used to be a single `FindingsDescription` `Static` (issue: a long
`finding.description` read as a wall of text once several rows were mounted at once).
`FindingsDescription` is now the two-widget container; `FindingTitle` renders on every
row, `FindingExpandedDescription` mirrors `FindingsSuggestion`'s own highlight-only
visibility (`Finding._apply_mode`/`set_hidden`/`set_plain`/`set_decision`, driven from
`findings_list.py`) -- while parked, `FindingTitle` also shows a decided-marker prefix so
a row's recorded decision stays visible after browsing away from it.
"""

from __future__ import annotations

from pathlib import Path

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Static

from code_review.pipeline.findings import Finding as FindingData
from code_review.pipeline.schemas import ApprovalDecision
from code_review.tui.widgets.styles import (
    _DECISION_MARKER_ICONS,
    _DECISION_MARKER_STYLES,
    _DOT_ICON,
    _SEVERITY_DOT_STYLES,
)

# A description past this length reads as a wall of text inside a bordered ~half-box
# column. 80 matches this test suite's own default console width (`_render_content`,
# tests/tui/test_widgets.py) and a typical single readable terminal line -- long enough to
# show real content, short enough that a row stays scannable. `FindingTitle` truncates to
# it; the untruncated text is one highlight away, in `FindingExpandedDescription`.
_TITLE_MAX_CHARS = 80


def truncate_to_one_line(text: str, max_chars: int = _TITLE_MAX_CHARS) -> str:
    """Collapse `text` to its first line -- further lines are dropped (matching
    `tool_activity.py`'s `assistant_text_label` "collapse to first line" precedent for a
    summary line; a multi-line description's later lines belong in
    `FindingExpandedDescription`, not the title) -- then truncate at the last word
    boundary at or before `max_chars`, appending "…".

    Unchanged if the first line already fits within `max_chars`. A first line with no
    space before the cutoff (one long unbroken token) truncates at exactly `max_chars`
    instead of not truncating at all.
    """

    first_line = text.splitlines()[0] if text else ""
    if len(first_line) <= max_chars:
        return first_line
    truncated = first_line[:max_chars]
    last_space = truncated.rfind(" ")
    if last_space > 0:
        truncated = truncated[:last_space]
    return f"{truncated.rstrip()}…"


def format_finding(finding: FindingData, *, description: str | None = None) -> str:
    """Render one `Finding` as `<severity>: <description>`, with ` (<location>)` appended
    only when `finding.location` is not `None`.

    `description`, when given, overrides `finding.description` verbatim -- `render_title`
    passes its own `truncate_to_one_line`'d text through this; the default (`None`) reuses
    `finding.description` untouched, matching this function's pre-title-split behavior.
    """

    text = finding.description if description is None else description
    location = "" if finding.location is None else f" ({finding.location})"
    return f"{finding.severity}: {text}{location}"


def render_description(
    finding: FindingData,
    decision: ApprovalDecision | None = None,
    *,
    description: str | None = None,
) -> Text:
    """Shared content-builder for `FindingTitle`: severity dot, then `format_finding`'s
    text.

    `decision` is this row's recorded park decision; `None` (the default) renders no
    marker, "fix"/"skip" prefix a small marker so a decided row is visually distinct.
    `description` is forwarded to `format_finding` unchanged -- see its own docstring.
    """

    text = Text()
    if decision is not None:
        icon = _DECISION_MARKER_ICONS.get(decision)
        if icon is not None:
            text.append(f"{icon} ", style=_DECISION_MARKER_STYLES[decision])
    text.append(_DOT_ICON, style=_SEVERITY_DOT_STYLES[finding.severity])
    text.append(f" {format_finding(finding, description=description)}")
    return text


def render_title(finding: FindingData, decision: ApprovalDecision | None = None) -> Text:
    """`FindingTitle`'s content: `render_description`, with `finding.description` first
    collapsed to one line via `truncate_to_one_line`. `finding.location`, when present, is
    still appended in full -- only the description component is truncated."""

    return render_description(
        finding, decision, description=truncate_to_one_line(finding.description)
    )


class FindingTitle(Static):
    """Always-visible one-line summary: severity dot, decided marker, then
    `render_title`'s `<severity>: <one-line-truncated description>{ (location)}` -- what
    `FindingsDescription` used to render directly, before the title/expanded-description
    split."""

    def __init__(
        self,
        finding: FindingData,
        *,
        id: str | None = None,  # noqa: A002 -- matches Textual's own Widget.__init__ shape
        classes: str | None = None,
    ) -> None:
        super().__init__(render_title(finding), id=id, classes=classes)

    def update_finding(
        self, finding: FindingData, decision: ApprovalDecision | None = None
    ) -> None:
        self.update(render_title(finding, decision))


class FindingExpandedDescription(Static):
    """The full, untruncated `finding.description` text -- mounted on every row but only
    visible while that row is highlighted (toggled via `FindingsDescription.
    set_expanded`, itself driven by `Finding`'s mode transitions in `findings_list.py`).

    `display: none` while hidden, so an unfocused row collapses down to just its
    `FindingTitle` line instead of reserving blank space for text nobody is reading; the
    `-visible` class restores it.
    """

    def __init__(
        self,
        finding: FindingData,
        *,
        id: str | None = None,  # noqa: A002 -- matches Textual's own Widget.__init__ shape
        classes: str | None = None,
    ) -> None:
        super().__init__(finding.description, id=id, classes=classes)

    def update_finding(self, finding: FindingData) -> None:
        self.update(finding.description)


class FindingsDescription(Vertical):
    """The left column of one `Finding` row: an always-visible `FindingTitle` above a
    `FindingExpandedDescription` that only shows this finding's full text while this row
    is highlighted -- mirrors `FindingsSuggestion`'s own highlight-only visibility, so an
    unfocused row's description reads as one short line instead of a wall of text.

    `width: 1fr` matches `FindingsSuggestion`'s own `1fr` for an even 50/50 split, with a
    `border-right` divider marking the boundary between the two columns on EVERY row --
    `FindingsSuggestion` itself only draws its own border while visible, so this column's
    divider is what keeps the row's two-column shape legible and stable regardless of
    whether the suggestion column has anything to show.
    """

    DEFAULT_CSS = (
        (Path(__file__).parent.parent / "tokens.tcss").read_text()
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
        super().__init__(id=id, classes=classes)
        self._title = FindingTitle(finding)
        self._detail = FindingExpandedDescription(finding)

    def compose(self) -> ComposeResult:
        yield self._title
        yield self._detail

    def update_finding(
        self, finding: FindingData, decision: ApprovalDecision | None = None
    ) -> None:
        self._title.update_finding(finding, decision)
        self._detail.update_finding(finding)

    def set_expanded(self, expanded: bool) -> None:
        """Show/hide `FindingExpandedDescription`. Called only from `Finding`'s own mode
        transitions (`set_hidden`/`set_plain`/`set_decision`, `findings_list.py`) -- the
        same highlight-driven call sites that already toggle `FindingsSuggestion`'s
        visibility, not a second, parallel focus-tracking mechanism."""

        if expanded:
            self._detail.add_class("-visible")
        else:
            self._detail.remove_class("-visible")
