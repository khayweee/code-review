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

import colorsys
import time
from collections.abc import Sequence

from rich.spinner import Spinner
from rich.table import Table
from rich.text import Text
from textual.widgets import Static

from code_review.pipeline.findings import Finding
from code_review.steps.review import ReviewOutput
from code_review.tui.state import ActivityRow, Status, StepRow

# One glyph per status in the deterministic text fallback. The live pipeline view uses
# a Rich spinner renderable for the running state so it can animate without any manual
# frame cycling in this module.
_STATUS_ICONS: dict[Status, str] = {
    "pending": "◌",  # ◌ hollow ring: not started yet
    "running": "◔",  # ◔ quarter-filled glyph: fallback only; live view uses Spinner
    "completed": "✔",  # ✔ check mark: finished successfully
    "failed": "✘",  # ✘ cross mark: raised before it could complete
}


def format_duration(duration: float) -> str:
    """Render a duration in seconds as `M:SS` once it reaches a minute, else `0.3s`."""

    if duration < 60:
        return f"{duration:.1f}s"
    minutes, seconds = divmod(int(duration), 60)
    return f"{minutes}:{seconds:02d}"


def format_row(row: StepRow) -> str:
    """Render one `StepRow` as plain text for tests and non-animated fallbacks."""

    icon = _STATUS_ICONS[row.status]
    duration = "" if row.duration is None else f"  {format_duration(row.duration)}"
    return f"{icon} {row.name}{duration}"


def format_activity_row(activity: ActivityRow) -> str:
    """Render one `ActivityRow` as an indented plain-text line, mirroring `format_row`'s
    icon/duration conventions -- the two-space indent is what reads as "nested under its
    owning step" in the plain-text fallback (`render_rows`); `render_rows_live`'s Rich
    rendering (`_render_activity_row`) achieves the same nesting visually instead."""

    icon = _STATUS_ICONS[activity.status]
    duration = "" if activity.duration is None else f"  {format_duration(activity.duration)}"
    return f"  {icon} {activity.label}{duration}"


def render_rows(rows: Sequence[StepRow]) -> str:
    """Render every row as one line each, in order, with each row's own `activities` (issue
    #66) rendered as indented lines immediately beneath it."""

    lines = []
    for row in rows:
        lines.append(format_row(row))
        lines.extend(format_activity_row(activity) for activity in row.activities)
    return "\n".join(lines)


def gradient_text(label: str, phase: float) -> Text:
    """Pure per-character gradient color computation for the running step's name -- a
    "rendering..." shimmer distinct from the plain text a pending/completed row gets.
    Factored out of `_render_row` so the actual color math is unit-testable without
    Textual or timing flakiness (`tui/AGENTS.md`'s pure/impure split convention: this
    module is otherwise impure, but the color computation itself doesn't need to be).

    Each character gets its own hue stop, cycling once across the label
    (`index / len(label)`), then the whole cycle is shifted by `phase` -- the caller passes
    `time.monotonic()` so consecutive repaints visibly move (`PipelineBox` already
    refreshes at 60fps, so no new timer is needed here, just a phase-aware render). Fixed,
    high saturation/lightness (`colorsys.hls_to_rgb`) keeps every stop vividly colored
    rather than washing out toward black or white at the ends of the hue wheel.
    """

    text = Text()
    length = max(len(label), 1)
    for index, char in enumerate(label):
        hue = (index / length + phase) % 1.0
        red, green, blue = colorsys.hls_to_rgb(hue, 0.6, 0.85)
        color = f"#{int(red * 255):02x}{int(green * 255):02x}{int(blue * 255):02x}"
        text.append(char, style=color)
    return text


def _render_row(row: StepRow, spinners: dict[str, Spinner]) -> tuple[Spinner | Text, Text]:
    """Render one row as Rich renderables, using a live spinner for running rows.

    `spinners` caches one `Spinner` instance per running step name, keyed by `row.name`,
    and is shared across repeated calls (see `PipelineBox._spinners`). A `Spinner`'s
    animation clock (`start_time`) is set lazily on its own first `render()` call and
    never reset after that -- constructing a *fresh* `Spinner` on every re-render (as this
    used to do) would reset that clock to "now" every time, so the frame never advances
    far enough to look animated before being wiped out on the next render. Reusing the
    same instance across a step's whole "running" lifetime is what lets it actually spin.
    A row that is no longer running has its cached spinner evicted, so a later run of the
    same-named step starts its animation fresh rather than resuming a stale clock.

    A running row's name renders via `gradient_text` (phased by `time.monotonic()` at
    render time) instead of plain text -- the duration suffix stays plain, appended after,
    so only the name itself shimmers.
    """

    if row.status != "running":
        spinners.pop(row.name, None)
        icon: Spinner | Text = Text(_STATUS_ICONS[row.status])
        row_text = Text(row.name)
    else:
        icon = spinners.setdefault(row.name, Spinner("moon"))
        row_text = gradient_text(row.name, phase=time.monotonic())
    duration = "" if row.duration is None else f"  {format_duration(row.duration)}"
    row_text.append(duration)
    return icon, row_text


def _render_activity_row(activity: ActivityRow) -> tuple[Text, Text]:
    """Render one `ActivityRow` as an indented Rich line under its owning step's row.

    Unlike `_render_row`, this never uses a live `Spinner` for a running activity --
    reusing the plain `_STATUS_ICONS`/`format_duration` conventions (per issue #66's own
    acceptance criteria) keeps this simple and avoids growing a second per-activity
    spinner cache; "live" here comes from the duration number itself ticking on
    `PipelineBox`'s existing 60fps refresh, the same way a `StepRow`'s duration does before
    this method's icon distinction even matters.
    """

    icon = Text(f"  {_STATUS_ICONS[activity.status]}")
    duration = "" if activity.duration is None else f"  {format_duration(activity.duration)}"
    return icon, Text(f"{activity.label}{duration}")


def render_rows_live(rows: Sequence[StepRow], spinners: dict[str, Spinner]) -> Table:
    """Render every row as Rich renderables so the running row can animate itself, with
    each row's own `activities` (issue #66) rendered as indented lines immediately
    beneath it via `_render_activity_row`.

    `spinners` is the caller's cache (see `_render_row`) -- passed in rather than created
    here so it persists across repeated calls for the same `PipelineBox`.
    """

    table = Table.grid(padding=(0, 1), pad_edge=False, expand=False)
    for row in rows:
        table.add_row(*_render_row(row, spinners))
        for activity in row.activities:
            table.add_row(*_render_activity_row(activity))
    return table


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
        # One `Spinner` per currently-running step name, reused across `update_rows`
        # calls for as long as that step stays running -- see `_render_row`'s docstring
        # for why a fresh `Spinner` per render never animates.
        self._spinners: dict[str, Spinner] = {}
        super().__init__(render_rows_live(rows, self._spinners), id=id, classes=classes)
        self._rows = list(rows)
        self.border_title = "Pipeline"

    def on_mount(self) -> None:
        self.set_interval(1 / 60, self.refresh)

    def update_rows(self, rows: Sequence[StepRow]) -> None:
        """Replace the displayed rows with `rows`, re-rendered in order."""

        self._rows = list(rows)
        self.update(render_rows_live(rows, self._spinners))


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
