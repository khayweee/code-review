"""The Pipeline box: one line per registry step, live status icon, elapsed/final duration.

The Findings box (issue #42, widened for #61): the most recently completed step's
`ReviewOutput` or `TestSufficiencyOutput`, one line per finding plus a severity-count
summary.

Rendering-only. Every widget here takes the data it displays as plain data (`StepRow`s for
`PipelineBox`, a `ReviewOutput`/`TestSufficiencyOutput` for `FindingsBox`, see `state.py`) --
neither widget ever reads a `StepEvent` stream or a registry/agent output itself. That split
keeps row/finding rendering unit-testable via `render_rows`/`render_findings` in isolation,
and widget mounting/refresh testable via Textual's `Pilot` (`tests/tui/test_widgets.py`),
without needing a live event stream either way.
"""

from __future__ import annotations

import colorsys
import time
from collections.abc import Sequence

from rich.console import Group
from rich.spinner import Spinner
from rich.table import Table
from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.css.query import NoMatches
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option

from code_review.pipeline.findings import Finding
from code_review.steps.review import ReviewOutput
from code_review.steps.test_sufficiency import TestSufficiencyOutput
from code_review.tui.state import ActivityRow, Status, StepRow

# One glyph per status in the deterministic text fallback. The live pipeline view uses
# a Rich spinner renderable for the running state so it can animate without any manual
# frame cycling in this module.
_STATUS_ICONS: dict[Status, str] = {
    "pending": "◌",  # ◌ hollow ring: not started yet
    "running": "◔",  # ◔ quarter-filled glyph: fallback only; live view uses Spinner
    "completed": "✔",  # ✔ check mark: finished successfully
    "failed": "✘",  # ✘ cross mark: raised before it could complete
    "parked": "⏸",  # ⏸ pause: needs_approval=True, waiting on a human approve/skip/abort
    "skipped": "⏭",  # ⏭ skip-forward: a human chose "skip" -- bypassed, not an error
}

# Mid-gray for activity lines, so they read as subordinate detail beneath their step.
_ACTIVITY_STYLE = "grey58"

# Live pipeline view only: a completed/failed row's icon renders as this solid dot, colored
# by status, instead of the fallback ✔/✘ glyph above -- the plain-text fallback keeps ✔/✘
# since it has no color to lean on for the completed/failed distinction.
_DOT_ICON = "●"
_STATUS_DOT_STYLES: dict[Status, str] = {
    "completed": "#5fafff",  # blue
    "failed": "#bb6400",  # orange
    "parked": "#d7af00",  # amber -- waiting on a human decision
    "skipped": "#8a8a8a",  # gray -- deliberately bypassed by a human, not an error
}

# `FindingsBox`'s per-finding risk indicator (issue #77): a colored `_DOT_ICON`, keyed by
# `Finding.severity` -- the same "reuse an existing field via a colored dot" shape as
# `_STATUS_DOT_STYLES` above, rather than inventing a new per-finding risk field (`Finding`
# carries no such field today; only a review's overall `risk_level` verdict does).
_SEVERITY_DOT_STYLES: dict[str, str] = {
    "error": "#bb6400",  # orange -- matches _STATUS_DOT_STYLES's "failed" color
    "warning": "#d7af00",  # amber -- matches _STATUS_DOT_STYLES's "parked" color
    "info": "#5fafff",  # blue -- matches _STATUS_DOT_STYLES's "completed" color
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


def format_activity_row(activity: ActivityRow, *, is_last: bool) -> str:
    """Render one `ActivityRow` as a directory-tree-style line, mirroring `format_row`'s
    icon/duration conventions. A plain two-space indent read as "some other top-level row"
    at a glance rather than "nested under the row above it" -- the `├─`/`└─` connector
    (`is_last` picks which) is what a `tree`/file-browser listing uses for exactly this
    "these lines belong to the row above" signal, so it is used here for the same reason.
    `is_last` is the caller's (`render_rows`/`render_rows_live`) job to compute, not this
    function's: it only knows about one `ActivityRow` at a time, not its position among
    its owning step's other activities."""

    connector = "└ " if is_last else "├ "
    icon = _STATUS_ICONS[activity.status]
    duration = "" if activity.duration is None else f"  {format_duration(activity.duration)}"
    return f"  {connector} {icon} {activity.label}{duration}"


def render_rows(rows: Sequence[StepRow]) -> str:
    """Render every row as one line each, in order, with each row's own `activities` (issue
    #66) rendered as tree-connected lines immediately beneath it (see
    `format_activity_row`'s docstring for why a connector, not a plain indent)."""

    lines = []
    for row in rows:
        lines.append(format_row(row))
        last_index = len(row.activities) - 1
        lines.extend(
            format_activity_row(activity, is_last=index == last_index)
            for index, activity in enumerate(row.activities)
        )
    return "\n".join(lines)


_SHIMMER_BASE_LIGHTNESS = 0.45
_SHIMMER_PEAK_LIGHTNESS = 0.90


def gradient_text(label: str, phase: float) -> Text:
    """Pure per-character gradient color computation for the running step's name -- a
    "rendering..." shimmer distinct from the plain text a pending/completed row gets.
    Factored out of `_render_row` so the actual color math is unit-testable without
    Textual or timing flakiness (`tui/AGENTS.md`'s pure/impure split convention: this
    module is otherwise impure, but the color computation itself doesn't need to be).

    A single grayscale highlight band sweeps across the label once per phase cycle
    (`index / len(label)`, shifted by `phase`), brightest at the band's center and fading
    to `_SHIMMER_BASE_LIGHTNESS` at its edges (triangular falloff) -- the caller passes
    `time.monotonic()` so consecutive repaints visibly move (`PipelineBox` already
    refreshes at 60fps, so no new timer is needed here, just a phase-aware render). Zero
    saturation (`colorsys.hls_to_rgb`) keeps every stop neutral gray/white rather than
    cycling through hues.
    """

    text = Text()
    length = max(len(label), 1)
    for index, char in enumerate(label):
        position = (index / length + phase) % 2.0
        distance_from_peak = abs(position - 0.5) * 2
        brightness = max(0.0, 1.0 - distance_from_peak)
        lightness = _SHIMMER_BASE_LIGHTNESS + brightness * (
            _SHIMMER_PEAK_LIGHTNESS - _SHIMMER_BASE_LIGHTNESS
        )
        red, green, blue = colorsys.hls_to_rgb(0.0, lightness, 0.0)
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

    A completed/failed row's icon renders as a colored `_DOT_ICON` (`_STATUS_DOT_STYLES`)
    rather than the fallback ✔/✘ glyph -- pending keeps its plain hollow-ring glyph, since
    only completed/failed need a status color here.
    """

    if row.status != "running":
        spinners.pop(row.name, None)
        dot_style = _STATUS_DOT_STYLES.get(row.status)
        icon: Spinner | Text = (
            Text(_DOT_ICON, style=dot_style) if dot_style else Text(_STATUS_ICONS[row.status])
        )
        row_text = Text(row.name)
    else:
        icon = spinners.setdefault(row.name, Spinner("moon"))
        row_text = gradient_text(row.name, phase=time.monotonic())
    duration = "" if row.duration is None else f"  {format_duration(row.duration)}"
    row_text.append(duration)
    return icon, row_text


def render_rows_live(rows: Sequence[StepRow], spinners: dict[str, Spinner]) -> Group:
    """Render every row as Rich renderables so the running row can animate itself, with
    each row's own `activities` (issue #66) rendered as tree-connected lines immediately
    beneath it.

    Returns a `Group`, not one shared `Table.grid` spanning every row -- an earlier version
    of the tree connectors below did exactly that (a single grid, an icon column, a text
    column) and regressed into the "step rows are too indented" report this shape fixes: a
    `Table.grid`'s columns are sized to the widest cell *anywhere in that column, across
    every row ever added to it*, so a step with no activities at all still got padded out
    to match however wide the longest connector among some *other* step's activities
    happened to be, even though the two have nothing to do with each other. Giving every
    step its own small `(icon, text)` grid -- sized only from that one row -- sidesteps the
    shared-column coupling entirely: every step's icon cell is exactly one glyph wide
    regardless of what any other step is doing.

    Activity lines don't need a grid at all: unlike a step, an activity never renders a
    live `Spinner` (see `format_activity_row`'s docstring for why), so there is no second
    renderable that needs its own aligned cell -- each is one already-fully-formed `Text`
    line, its `├─`/`└─` connector baked directly into the string by `format_activity_row`,
    with no column alignment of any kind at play.

    `spinners` is the caller's cache (see `_render_row`) -- passed in rather than created
    here so it persists across repeated calls for the same `PipelineBox`.
    """

    renderables: list[Table | Text] = []
    for row in rows:
        step_table = Table.grid(padding=(0, 1), pad_edge=False, expand=False)
        step_table.add_row(*_render_row(row, spinners))
        renderables.append(step_table)

        last_index = len(row.activities) - 1
        for index, activity in enumerate(row.activities):
            renderables.append(
                Text(
                    format_activity_row(activity, is_last=index == last_index),
                    style=_ACTIVITY_STYLE,
                )
            )
    return Group(*renderables)


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
        id: (str | None) = None,  # noqa: A002 -- matches Textual's own Widget.__init__ shape
        classes: str | None = None,
    ) -> None:
        # One `Spinner` per currently-running step name, reused across `update_rows`
        # calls for as long as that step stays running -- see `_render_row`'s docstring
        # for why a fresh `Spinner` per render never animates.
        self._spinners: dict[str, Spinner] = {}
        super().__init__(render_rows_live(rows, self._spinners), id=id, classes=classes)
        self._rows = list(rows)
        self.border_title = "Agentic Code Review Pipeline"

    def on_mount(self) -> None:
        self.set_interval(1 / 60, self._animate_shimmer)

    def _animate_shimmer(self) -> None:
        """Re-run `render_rows_live` every tick, not just `self.refresh()` -- `refresh()`
        alone only repaints whatever renderable is already stored, it does not call
        `gradient_text` again. A running row's shimmer color spans get baked into a plain
        `Text` once, at whatever moment `render_rows_live` last ran (i.e. a real
        `update_rows` call, driven by pipeline events, not by this timer), so without
        recomputing here the shimmer freezes between events and jumps -- the `Spinner`
        icon animates fine regardless because Rich re-invokes `Spinner.__rich_console__`
        itself on every repaint, but a `Text`'s spans are static once built. `layout=False`
        since this recompute never changes row count/line length, only per-character
        color, so the layout pass every real `update_rows` does would be wasted work here.

        Named `_animate_shimmer`, not `_animate` -- `Widget` already defines a private
        `_animate: BoundAnimator | None` attribute for its own animation system, and
        shadowing it with a same-named method broke mypy (`[override]`) with an unrelated
        signature.
        """

        self.update(render_rows_live(self._rows, self._spinners), layout=False)

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


def _render_finding_row(finding: Finding, *, show_suggestions: bool = True) -> tuple[Text, Text]:
    """Render one `Finding` as a `(left, right)` cell pair for `render_findings`'s grid.

    Left: a colored `_DOT_ICON` (`_SEVERITY_DOT_STYLES`, keyed by `finding.severity`) --
    the per-finding risk indicator issue #77 asks for, reusing `severity` rather than a
    new field -- followed by `format_finding`'s existing severity/description/location
    text. Right: `finding.suggestions`, one per line, or an empty `Text` when there are
    none -- never a placeholder string like `"None"` (a `no-op`/`auto-fix` finding has
    nothing for a human to choose between).

    `show_suggestions=False` (issue #88, `FindingsBox`'s per-option rendering) forces an
    empty right cell regardless of `finding.suggestions` -- used for every option except
    the one currently under the cursor, so only the focused finding's suggestions show."""

    left = Text(_DOT_ICON, style=_SEVERITY_DOT_STYLES[finding.severity])
    left.append(f" {format_finding(finding)}")
    right = Text("\n".join(finding.suggestions) if show_suggestions else "")
    return left, right


def _findings_summary(output: ReviewOutput | TestSufficiencyOutput) -> str:
    """Render `output.findings`' severity-count summary, e.g. `1 error, 2 warning, 0
    info` -- shared by `render_findings` and `FindingsBox`'s own summary line."""

    counts = {severity: 0 for severity in ("error", "warning", "info")}
    for finding in output.findings:
        counts[finding.severity] += 1
    return f"{counts['error']} error, {counts['warning']} warning, {counts['info']} info"


def render_findings(output: ReviewOutput | TestSufficiencyOutput) -> Group:
    """Render every finding in `output.findings` as a two-column grid (issue #77) --
    left column each finding's severity/description/location plus a per-finding risk
    indicator (`_render_finding_row`), right column that finding's `suggestions` -- then a
    blank line and a severity-count summary, e.g. `1 error, 2 warning, 0 info`, below the
    grid. `output` is whichever of `ReviewOutput`/`TestSufficiencyOutput` `state.py`'s
    `latest_findings` picked -- both schemas share an identical `findings: list[Finding]`
    shape, so nothing below needs to branch on which one it got.

    Uses `Table.grid`, one row per finding, mirroring `render_rows_live`'s per-row-grid
    pattern (see that function's own docstring for why a single shared grid across
    unrelated rows is the wrong shape: one finding's long text would otherwise widen every
    other finding's columns too). Returned as a `Group` so the grid's height -- and
    therefore `_BorderedBox`'s `height: auto` CSS -- tracks the number of findings actually
    shown, rather than reserving a fixed size."""

    grid = Table.grid(padding=(0, 1), pad_edge=False, expand=False)
    grid.add_column()
    grid.add_column()
    for finding in output.findings:
        grid.add_row(*_render_finding_row(finding))

    return Group(grid, "", _findings_summary(output))


def _finding_option_prompt(finding: Finding, *, show_suggestions: bool) -> Table:
    """Render one `Finding` as an `OptionList.Option`'s `prompt` -- the same two-column
    `(left, right)` shape `render_findings` uses per row, in its own small grid rather
    than a shared one (see `render_rows_live`'s docstring for why a per-row grid, not one
    grid spanning every option)."""

    row = Table.grid(padding=(0, 1), pad_edge=False, expand=False)
    row.add_column()
    row.add_column()
    row.add_row(*_render_finding_row(finding, show_suggestions=show_suggestions))
    return row


def _finding_options(findings: Sequence[Finding], highlighted: int | None) -> list[Option]:
    """Build one `OptionList.Option` per finding (issue #88) -- only the option at index
    `highlighted` shows its suggestions in the right column, every other index's right
    column is empty. `highlighted=None` (no findings, or before Textual has highlighted
    anything) shows no suggestions anywhere."""

    return [
        Option(_finding_option_prompt(finding, show_suggestions=index == highlighted))
        for index, finding in enumerate(findings)
    ]


class FindingsBox(Vertical):
    """A bordered box showing the most recently completed step's findings (see
    `state.py`'s `latest_findings`, which recognizes a `ReviewOutput` (`ReviewStep`) or a
    `TestSufficiencyOutput` (`TestSufficiencyStep`) equally): each finding's severity,
    description, location when it has one, and risk indicator in a left column, that
    finding's suggestions in a right column -- but only for the finding currently under
    the cursor (issue #88); arrow keys move the cursor between findings via a child
    `OptionList`. A trailing severity-count summary line sits below the list, unchanged
    in wording from #77. `border_title` names the owning step (issue #74, e.g.
    `"Findings -- ReviewStep"`) so it's clear which step's output is on display. Display
    only -- no key or action here lets a user approve, fix, skip, or abort a finding (see
    docs/GLOSSARY.md's "Action"; Milestone 7's fix/approval loop is a later ticket).

    A `Vertical`, not a `_BorderedBox` (`Static`) subclass like `PipelineBox`/`StatusBox`
    -- it needs two children (the `OptionList` and the summary `Static`), which a
    `Static` can't host. `_BorderedBox`'s border/padding rule is duplicated here rather
    than shared, since this widget can no longer extend that base alongside `Vertical`.
    """

    DEFAULT_CSS = """
    FindingsBox {
        border: round $primary;
        padding: 0 1;
        height: auto;
    }

    FindingsBox > OptionList {
        height: auto;
        border: none;
    }
    """

    def __init__(
        self,
        output: ReviewOutput | TestSufficiencyOutput,
        step_name: str,
        *,
        id: (str | None) = None,  # noqa: A002 -- matches Textual's own Widget.__init__ shape
        classes: str | None = None,
    ) -> None:
        super().__init__(id=id, classes=classes)
        self._output = output
        self.border_title = f"Findings -- {step_name}"

    def compose(self) -> ComposeResult:
        yield OptionList(*_finding_options(self._output.findings, highlighted=0))
        yield Static(_findings_summary(self._output))

    def update_findings(self, output: ReviewOutput | TestSufficiencyOutput, step_name: str) -> None:
        """Replace the displayed findings with `output`'s, re-rendered, and update
        `border_title` to name `step_name` (issue #74).

        `app.py`'s `_render` runs on a periodic timer, so a freshly `self.mount()`ed
        `FindingsBox` can receive an `update_findings` call before Textual has actually
        finished mounting its own `compose()`-yielded children -- `self.mount()` returns
        as soon as the widget is attached to the DOM, not once its subtree is fully
        mounted. `self._output`/`border_title` are always updated regardless (neither
        needs a mounted child), and the `OptionList`/`Static` rebuild is skipped rather
        than raised when they aren't there yet -- `compose()` already renders from
        `self._output`, so once mounting does finish it reflects this call's data anyway.
        """

        self._output = output
        self.border_title = f"Findings -- {step_name}"
        try:
            option_list = self.query_one(OptionList)
            summary = self.query_one(Static)
        except NoMatches:
            return
        option_list.clear_options()
        option_list.add_options(_finding_options(output.findings, highlighted=0))
        summary.update(_findings_summary(output))

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        """Rebuild every option's prompt so only `event.option_index`'s row shows its
        suggestions -- see the module docstring's rationale for a full rebuild over
        diffing old-vs-new highlighted index."""

        for index, finding in enumerate(self._output.findings):
            event.option_list.replace_option_prompt_at_index(
                index, _finding_option_prompt(finding, show_suggestions=index == event.option_index)
            )


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
        id: (str | None) = None,  # noqa: A002 -- matches Textual's own Widget.__init__ shape
        classes: str | None = None,
    ) -> None:
        super().__init__(message, id=id, classes=classes)
        self.border_title = "Status"

    def update_status(self, message: str) -> None:
        """Replace the displayed status message with `message`."""

        self.update(message)
