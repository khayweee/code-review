"""The Pipeline box: one line per registry step, live status icon, elapsed/final duration.

The Findings box (issue #42): the most recently completed step's `ReviewOutput`, one line
per finding plus a severity-count summary.

Rendering-only. Every widget here takes the data it displays as plain data (`StepRow`s for
`PipelineBox`, a `ReviewOutput` for `FindingsBox`, see `state.py`) -- neither widget ever
reads a `StepEvent` stream or a registry/agent output itself. That split keeps row/finding
rendering unit-testable via `render_rows`/`render_findings` in isolation, and widget
mounting/refresh testable via Textual's `Pilot` (`tests/tui/test_widgets.py`), without
needing a live event stream either way.
"""

from __future__ import annotations

from collections.abc import Sequence

from textual.widgets import Static

from code_review.pipeline.findings import Finding
from code_review.steps.review import ReviewOutput
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


class _BorderedBox(Static):
    """Shared base for this app's bordered, auto-height boxes (`PipelineBox`,
    `FindingsBox`, `StatusBox`). Textual resolves `DEFAULT_CSS` against a widget's whole
    class hierarchy, not just its leaf class, so defining the shared border/padding rule
    once here -- keyed to this base class's own name -- reaches every subclass without
    repeating it per box. Factored out once a third box (`StatusBox`) needed the identical
    rule; two copies were fine, three would not have been.
    """

    DEFAULT_CSS = """
    _BorderedBox {
        border: round $primary;
        padding: 0 1;
        height: auto;
    }
    """


class PipelineBox(_BorderedBox):
    """A bordered box listing every registered step and its current status."""

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


def format_finding(finding: Finding) -> str:
    """Render one `Finding` as `<severity>: <description>`, with ` (<location>)` appended
    only when `finding.location` is not `None` -- a finding with no location renders with
    no trailing parenthetical at all, rather than an empty `()`."""

    location = "" if finding.location is None else f" ({finding.location})"
    return f"{finding.severity}: {finding.description}{location}"


def render_findings(output: ReviewOutput) -> str:
    """Render every finding in `output.findings`, one per line via `format_finding`, then a
    blank line and a severity-count summary, e.g. `1 error, 2 warning, 0 info`."""

    lines = [format_finding(finding) for finding in output.findings]
    counts = {severity: 0 for severity in ("error", "warning", "info")}
    for finding in output.findings:
        counts[finding.severity] += 1
    summary = f"{counts['error']} error, {counts['warning']} warning, {counts['info']} info"
    return "\n".join([*lines, "", summary])


class FindingsBox(_BorderedBox):
    """A bordered box showing the most recently completed step's findings (see
    `state.py`'s `latest_findings`): each finding's severity, description, and location
    when it has one, plus a severity-count summary. Display only -- no key or action here
    lets a user approve, fix, skip, or abort a finding (see docs/GLOSSARY.md's "Action";
    Milestone 7's fix/approval loop is a later ticket)."""

    def __init__(
        self,
        output: ReviewOutput,
        *,
        id: str | None = None,  # noqa: A002 -- matches Textual's own Widget.__init__ shape
        classes: str | None = None,
    ) -> None:
        super().__init__(render_findings(output), id=id, classes=classes)
        self.border_title = "Findings"

    def update_findings(self, output: ReviewOutput) -> None:
        """Replace the displayed findings with `output`'s, re-rendered."""

        self.update(render_findings(output))


class StatusBox(_BorderedBox):
    """A bordered box shown once the pipeline run finishes, successfully or not (see
    `app.py`'s `_render_status`/`state.py`'s `final_status_message`): a one-line outcome
    plus the reminder that "e" now closes the app. Mounted dynamically, only once the run
    is done -- a still-running pipeline shows no Status box at all, mirroring
    `FindingsBox`'s own dynamic mount pattern (`app.py`'s `_render_findings`)."""

    def __init__(
        self,
        message: str,
        *,
        id: str | None = None,  # noqa: A002 -- matches Textual's own Widget.__init__ shape
        classes: str | None = None,
    ) -> None:
        super().__init__(message, id=id, classes=classes)
        self.border_title = "Status"

    def update_status(self, message: str) -> None:
        """Replace the displayed status message with `message`."""

        self.update(message)
