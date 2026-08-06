"""The Pipeline box: one line per registry step, live status icon, elapsed/final duration.

The Findings box (issue #42, widened for #61/#87, rebuilt as a widget tree by #91): the
most recently completed step's `ReviewOutput`, `TestSufficiencyOutput`, or bare
`list[Finding]`, one `Finding` row per finding plus a severity-count summary -- and, while
a step is parked (issue #87), a live inline approve/skip/abort/chat decision selector
replacing the old `ApprovalPromptScreen` modal.

Every widget here takes the data it displays as plain data (`StepRow`s for `PipelineBox`,
a `ReviewOutput`/`TestSufficiencyOutput`/`list[Finding]` for `FindingsList`, see
`state.py`) -- neither widget ever reads a `StepEvent` stream or a registry/agent output
itself. That split keeps row/finding rendering unit-testable via `render_rows`/the
finding-rendering helpers in isolation, and widget mounting/refresh/interaction testable
via Textual's `Pilot` (`tests/tui/test_widgets.py`), without needing a live event stream
either way.
"""

from __future__ import annotations

import asyncio
import colorsys
import time
from collections.abc import Sequence
from typing import Literal, cast

from rich.console import Group
from rich.spinner import Spinner
from rich.table import Table
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.css.query import NoMatches
from textual.widgets import Input, ListItem, ListView, Static

from code_review.pipeline.findings import Finding as FindingData
from code_review.pipeline.step import ApprovalDecision, ApprovalResponse
from code_review.steps.review import ReviewOutput
from code_review.steps.test_sufficiency import TestSufficiencyOutput
from code_review.tui.state import ActivityRow, Status, StepRow

# `FindingsList`'s per-finding decision cycle (issue #87, kept by #91), appended after that
# finding's own `suggestions` -- shared by every finding row, since the decision itself is
# step-scoped, not per-finding (see `FindingsList.await_decision`'s docstring). Plain
# strings, not `ApprovalDecision` values, because "Type something." is not itself a
# decision -- it opens the inline chat widget rather than resolving anything. Rendered as
# the numbered list's trailing free-text option (see `render_decision_cycle`), after every
# suggestion.
_CUSTOM_ENTRY = "Type something."
_DECISION_ENTRIES = ("approve", "skip", "abort")

# Short static UI copy for the four fixed decision-cycle entries above (issue #91), shown
# as an indented detail line beneath each in `render_decision_cycle` -- a suggestion's own
# text stays single-line, since it has no further data to split a detail line from.
_ENTRY_DETAILS: dict[str, str] = {
    _CUSTOM_ENTRY: "Open a free-text prompt for this finding.",
    "approve": "Accept this finding's outcome and continue.",
    "skip": "Leave this finding unresolved and continue.",
    "abort": "Stop the pipeline run entirely.",
}

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

# `FindingsList`'s per-finding risk indicator (issue #77): a colored `_DOT_ICON`, keyed by
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
    `FindingsList`, `StatusBox`). Textual resolves `DEFAULT_CSS` against a widget's whole
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


def format_finding(finding: FindingData) -> str:
    """Render one `Finding` as `<severity>: <description>`, with ` (<location>)` appended
    only when `finding.location` is not `None` -- a finding with no location renders with
    no trailing parenthetical at all, rather than an empty `()`."""

    location = "" if finding.location is None else f" ({finding.location})"
    return f"{finding.severity}: {finding.description}{location}"


def _findings_of(
    output: ReviewOutput | TestSufficiencyOutput | list[FindingData],
) -> list[FindingData]:
    """Extract the plain `list[Finding]` from whichever of `ReviewOutput`/
    `TestSufficiencyOutput`/bare `list[Finding]` `state.py`'s `latest_findings` picked
    (issue #87 widened it to accept `steps/rebase.py`'s bare-list shape too, see that
    module's docstring for why) -- the one place `FindingsList`'s helpers need to branch on
    shape, so nothing downstream does."""

    return output if isinstance(output, list) else output.findings


def _decision_entries(finding: FindingData) -> list[str]:
    """The full per-finding decision cycle a parked `FindingsList` cycles through (issue
    #87): that finding's own `suggestions`, then `_CUSTOM_ENTRY`, then the step-scoped
    `_DECISION_ENTRIES` -- one unified list, not two separate concerns, per the design
    call that landed #87 (confirming a suggestion or `_CUSTOM_ENTRY` is discussion-only
    and opens the inline chat widget; confirming a `_DECISION_ENTRIES` value resolves the
    whole step's park immediately, regardless of which finding's row it was confirmed
    from -- see `FindingsList.await_decision`). Rendered as a 1-based numbered list by
    `render_decision_cycle`, so a digit key (`_FindingsListView`'s `"1"`.."9"` bindings) can
    jump a row's `_decision_cursor` straight to any entry here by that same 1-based index."""

    return [*finding.suggestions, _CUSTOM_ENTRY, *_DECISION_ENTRIES]


def render_description(finding: FindingData) -> Text:
    """`FindingsDescription`'s content: a colored `_DOT_ICON` (`_SEVERITY_DOT_STYLES`,
    keyed by `finding.severity`) -- the per-finding risk indicator issue #77 asks for,
    reusing `severity` rather than a new field -- followed by `format_finding`'s existing
    severity/description/location text. No `no_wrap` -- `FindingsDescription`'s own
    `width: 1fr` (see that class's docstring) is a bounded column, not an auto-sized one, so
    a long description wraps within it rather than needing to stay on one physical line."""

    text = Text(_DOT_ICON, style=_SEVERITY_DOT_STYLES[finding.severity])
    text.append(f" {format_finding(finding)}")
    return text


def render_suggestions_plain(finding: FindingData) -> Text:
    """`FindingsSuggestion`'s content outside a decision cycle: `finding.suggestions`, one
    per line, or an empty `Text` when there are none -- never a placeholder string like
    `"None"` (a `no-op`/`auto-fix` finding has nothing for a human to choose between)."""

    return Text("\n".join(finding.suggestions))


def render_decision_cycle(finding: FindingData, decision_cursor: int) -> Text:
    """`FindingsSuggestion`'s content while parked and this row is the highlighted one
    (issue #87, kept by #91): every entry of `_decision_entries`, numbered from 1
    (matching `_FindingsListView`'s digit-key shortcuts), with a leading `"> "` marking
    whichever index `decision_cursor` names instead of a plain two-space indent.

    Entry 0 is additionally labeled `" (Recommended)"` when it came from
    `finding.suggestions` itself (i.e. this finding has at least one suggestion, so
    `_decision_entries`'s first entry is that suggestion rather than `_CUSTOM_ENTRY`) --
    styled after the Claude Code CLI's own interactive picker. The four fixed entries
    (`_CUSTOM_ENTRY`/`_DECISION_ENTRIES`) each get a short indented detail line of static
    UI copy (`_ENTRY_DETAILS`); a suggestion's own text stays single-line, since it has no
    further data to split a detail line from."""

    entries = _decision_entries(finding)
    text = Text()
    for index, entry in enumerate(entries):
        marker = "> " if index == decision_cursor else "  "
        recommended = " (Recommended)" if index == 0 and finding.suggestions else ""
        text.append(f"{marker}{index + 1}. {entry}{recommended}\n")
        detail = _ENTRY_DETAILS.get(entry)
        if detail is not None:
            text.append(f"      {detail}\n")
    return text


def _findings_summary(output: ReviewOutput | TestSufficiencyOutput | list[FindingData]) -> str:
    """Render `output`'s severity-count summary, e.g. `1 error, 2 warning, 0 info` --
    `FindingsList`'s own summary line."""

    counts = {severity: 0 for severity in ("error", "warning", "info")}
    for finding in _findings_of(output):
        counts[finding.severity] += 1
    return f"{counts['error']} error, {counts['warning']} warning, {counts['info']} info"


class FindingsDescription(Static):
    """The left column of one `Finding` row (issue #91): severity dot, description,
    location.

    `width: 1fr`, matching `FindingsSuggestion`'s own `1fr` -- an even, row-independent
    split (issue #92), superseding this class's earlier `width: auto`. That earlier version
    sized each row's description to its own content, which squeezed `FindingsSuggestion`
    by a different amount on every row depending on that row's own description length --
    since only one row is ever highlighted at a time (`FindingsSuggestion` is empty and
    `display: none` on every other), the split ratio a human actually saw while browsing
    findings changed from row to row rather than reading as one consistent grid. Matched
    `1fr` shares fix that: every row's `_FindingsListView` shares the same row width, so two
    equal `fr` columns land at the identical 50/50 boundary regardless of which row is
    highlighted or how long its own description happens to be.

    A long description now wraps within its half of the row instead of squeezing its
    sibling -- Textual's own default word-wrap on a bounded-width `Static`, no `no_wrap`
    override needed. No `border-right` divider either: `FindingsSuggestion` draws its own
    full border when visible (see that class's docstring), which already marks the split
    without a second, always-on line duplicating it for the common case (every row but the
    highlighted one) where there is nothing on the right to divide from."""

    DEFAULT_CSS = """
    FindingsDescription {
        width: 1fr;
        padding: 0 1;
    }
    """

    def __init__(
        self,
        finding: FindingData,
        *,
        id: str | None = None,  # noqa: A002 -- matches Textual's own Widget.__init__ shape
        classes: str | None = None,
    ) -> None:
        super().__init__(render_description(finding), id=id, classes=classes)

    def update_finding(self, finding: FindingData) -> None:
        self.update(render_description(finding))


class FindingsSuggestion(Static):
    """The right column of one `Finding` row (issue #91) -- tri-state (hidden/plain/
    decision), since only the highlighted row shows anything (issue #88, kept by #91):
    every other row's `FindingsSuggestion` stays cleared. Mode switching is `Finding`'s
    job (see `set_hidden`/`set_plain`/`set_decision` below), not this widget's own -- it
    only knows how to render each of the three states, not when to be in one.

    `display: none` while hidden, not just empty content (issue #92) -- an *always*-present
    `1fr` column would reserve half of every row's width even when there is nothing to show
    on the right, leaving the common case (every row but the highlighted one) with its
    description text squeezed into half the row for no reason. `display: none` drops this
    column out of `Finding`'s horizontal layout entirely, so `FindingsDescription` (also
    `1fr`) is the only sized child left and takes the whole row -- matching this package's
    "no box, not an empty box" instinct one level down.

    The `-visible` class -- added in `show_plain`/`show_decision`, removed in `clear` --
    both restores `display: block` and draws a full `border` around this column (issue #92):
    a `border-right` on `FindingsDescription` used to be the only visual seam between the
    two columns, drawn on every row regardless of whether there was anything on the right to
    divide from; a border around `FindingsSuggestion` itself only appears when this column
    actually has content, reading as "this is its own widget" rather than a shared divider
    line."""

    DEFAULT_CSS = """
    FindingsSuggestion {
        width: 1fr;
        padding: 0 1;
        display: none;
    }

    FindingsSuggestion.-visible {
        display: block;
        border: round $primary-darken-1;
    }
    """

    def __init__(
        self,
        *,
        id: str | None = None,  # noqa: A002 -- matches Textual's own Widget.__init__ shape
        classes: str | None = None,
    ) -> None:
        super().__init__("", id=id, classes=classes)

    def clear(self) -> None:
        self.remove_class("-visible")
        self.update("")

    def show_plain(self, finding: FindingData) -> None:
        self.add_class("-visible")
        self.update(render_suggestions_plain(finding))

    def show_decision(self, finding: FindingData, decision_cursor: int) -> None:
        self.add_class("-visible")
        self.update(render_decision_cycle(finding, decision_cursor))


class Finding(ListItem):
    """One row per finding inside `_FindingsListView` (issue #91, superseding the old
    `FindingsBox`'s single `OptionList` of Rich-rendered options) -- composes
    `FindingsDescription`/`FindingsSuggestion` in a horizontal split, and owns this row's
    own display mode (`hidden`/`plain`/`decision`) and, while parked, its own
    `_decision_cursor`. The cursor is purely a per-row browsing aid, not itself a decision
    -- see `FindingsList.await_decision`'s docstring: confirming approve/skip/abort
    resolves the whole step's park regardless of which row's cursor it came from.

    Named `Finding`, shadowing `pipeline.findings.Finding` (imported into this module as
    `FindingData`) -- deliberate: this widget's identity in `widgets.py` *is* "one finding,
    rendered", the same way `PipelineBox`'s rows aren't given a separate `StepRowWidget`
    name of their own. `ListItem.can_focus=False`, so this class carries no key bindings of
    its own -- all of #87's parked-mode bindings live on `_FindingsListView` instead (the
    only focusable node in this whole subtree; see that class's docstring)."""

    DEFAULT_CSS = """
    Finding {
        layout: horizontal;
        height: auto;
    }
    """

    def __init__(
        self,
        finding: FindingData,
        *,
        id: str | None = None,  # noqa: A002 -- matches Textual's own Widget.__init__ shape
        classes: str | None = None,
    ) -> None:
        super().__init__(id=id, classes=classes)
        self.finding = finding
        self._decision_cursor = 0
        self._mode: Literal["hidden", "plain", "decision"] = "hidden"

    def compose(self) -> ComposeResult:
        yield FindingsDescription(self.finding)
        suggestion = FindingsSuggestion()
        yield suggestion
        # Prime it from whatever `_mode`/`_decision_cursor` already are -- a `set_hidden`/
        # `set_plain`/`set_decision`/`update_finding` call can legitimately land on this
        # row before this `compose()` has actually run (see `_render_suggestion`'s
        # docstring), in which case those calls updated `_mode`/`_decision_cursor` but
        # could not reach a `FindingsSuggestion` that didn't exist yet -- this is what
        # makes that state show up correctly once it finally does. A fresh `Finding`, never
        # touched by any of those, is `_mode="hidden"` by construction, matching
        # `FindingsSuggestion`'s own already-empty default -- so this is a no-op for the
        # ordinary "just mounted, nothing has happened yet" case.
        self._apply_mode(suggestion)

    def set_hidden(self) -> None:
        self._mode = "hidden"
        self._render_suggestion()

    def set_plain(self) -> None:
        self._mode = "plain"
        self._render_suggestion()

    def set_decision(self) -> None:
        self._mode = "decision"
        self._render_suggestion()

    def _apply_mode(self, suggestion: FindingsSuggestion) -> None:
        if self._mode == "hidden":
            suggestion.clear()
        elif self._mode == "plain":
            suggestion.show_plain(self.finding)
        else:
            suggestion.show_decision(self.finding, self._decision_cursor)

    def _render_suggestion(self) -> None:
        """Apply the current `_mode` to this row's `FindingsSuggestion`, unless this row's
        own `compose()` (yielding `FindingsDescription`/`FindingsSuggestion`) hasn't
        actually run yet -- a freshly mounted `Finding` (e.g. from `FindingsList.
        update_findings`'s own growth path, or the highlighted row `FindingsList.
        await_decision`/`on_list_view_highlighted` prime the instant they're highlighted)
        can receive a `set_hidden`/`set_plain`/`set_decision` call before that happens, the
        same "mount attaches immediately, composing its children is a later, async step"
        gap `FindingsList.update_findings` already guards against one level up. `_mode` is
        already updated by the caller regardless, so skipping the render here isn't lossy
        -- `compose()`'s own `_apply_mode` call picks it up once it does run."""

        try:
            suggestion = self.query_one(FindingsSuggestion)
        except NoMatches:
            return
        self._apply_mode(suggestion)

    def reset_decision(self) -> None:
        """Reset `_decision_cursor` back to 0 -- called whenever this row becomes the
        highlighted one under a park (issue #87), so each finding's own decision cycle
        always starts fresh rather than carrying over whatever index a previous highlight
        left it on."""

        self._decision_cursor = 0

    def update_finding(self, finding: FindingData) -> None:
        """Data changed in place, same list position (`FindingsList.update_findings`'s
        in-place path) -- refresh both children, preserving whichever display mode this
        row is currently in rather than forcing one. Skipped, like `_render_suggestion`,
        when this row hasn't composed yet -- `self.finding` is still updated below
        regardless, so the eventual real `compose()` reflects this call's data anyway."""

        self.finding = finding
        try:
            description = self.query_one(FindingsDescription)
        except NoMatches:
            return
        description.update_finding(finding)
        self._render_suggestion()

    def cycle_decision(self, delta: int) -> None:
        entries = _decision_entries(self.finding)
        self._decision_cursor = (self._decision_cursor + delta) % len(entries)
        self.set_decision()

    def jump_decision(self, index: int) -> None:
        """Jump `_decision_cursor` straight to `index` (0-based) -- the digit-key
        counterpart to `cycle_decision`'s relative left/right step. A no-op when `index`
        is past this finding's own `_decision_entries` (a finding with fewer suggestions
        than another has a shorter list, so not every digit is valid for every row)."""

        entries = _decision_entries(self.finding)
        if not 0 <= index < len(entries):
            return
        self._decision_cursor = index
        self.set_decision()

    def confirmed_entry(self) -> str:
        return _decision_entries(self.finding)[self._decision_cursor]


class _FindingsListView(ListView):
    """Direct successor to `FindingsBox`'s old private `_FindingOptionList` (issue #91).
    `ListView(can_focus=True, can_focus_children=False)`: the `ListView` itself holds
    keyboard focus, its `ListItem` children can never be individually focused, and its own
    `action_cursor_up/down`/`watch_index`/`highlighted_child` all index/assert against
    `self._nodes` unfiltered -- so every one of #87's parked-mode key bindings on top of
    up/down/enter (which `ListView`'s own `BINDINGS` already give this for free) lives
    here, never on `Finding` itself: left/right cycle the highlighted finding's
    `_decision_entries`, single-key "a"/"s"/"x"/"f" shortcuts jump straight to
    Approve/Skip/Abort/free-text regardless of cursor position, and digit keys "1".."9"
    jump straight to that 1-based entry (matching `render_decision_cycle`'s numbering).
    All of these delegate to the owning `FindingsList`, which no-ops them while not parked
    -- this class holds no decision state of its own."""

    BINDINGS = [
        Binding("left", "cycle_prev", "Previous suggestion", show=False),
        Binding("right", "cycle_next", "Next suggestion", show=False),
        Binding("a", "quick_approve", "Approve", show=False),
        Binding("s", "quick_skip", "Skip", show=False),
        Binding("x", "quick_abort", "Abort", show=False),
        Binding("f", "open_chat", "Type something", show=False),
        *(
            Binding(str(digit), f"jump_decision({digit})", f"Option {digit}", show=False)
            for digit in range(1, 10)
        ),
    ]

    def __init__(self, *items: Finding, owner: FindingsList) -> None:
        # `ListView` assumes every mounted child is a `ListItem` and indexes into
        # `self._nodes` unfiltered -- a summary/footer `Static` (or any other non-`Finding`
        # widget) mounted as a *child of this* would silently corrupt that indexing, not
        # raise anywhere near the mistake. Asserted here, at the one place every row this
        # class will ever host is constructed, rather than trusted implicitly.
        assert all(isinstance(item, Finding) for item in items), (
            "_FindingsListView only ever hosts Finding rows."
        )
        super().__init__(*items)
        self._owner = owner

    def action_cycle_prev(self) -> None:
        self._owner._cycle_decision(-1)

    def action_cycle_next(self) -> None:
        self._owner._cycle_decision(1)

    def action_quick_approve(self) -> None:
        self._owner._quick_decision("approve")

    def action_quick_skip(self) -> None:
        self._owner._quick_decision("skip")

    def action_quick_abort(self) -> None:
        self._owner._quick_decision("abort")

    def action_open_chat(self) -> None:
        self._owner._open_chat("")

    def action_jump_decision(self, digit: int) -> None:
        self._owner._jump_decision(digit - 1)


class _InlineApprovalChat(Vertical):
    """A small inline free-text widget (issue #87), mounted into a parked `FindingsList`
    on demand -- when a human confirms a suggestion (seeded with that suggestion's own
    text) or `_CUSTOM_ENTRY` (seeded empty). Replaces `ApprovalPromptScreen`'s "fix" path
    pushing `InputPromptScreen`: there is no modal host for an `Input` in this design
    (issue #87 removes the modal entirely), so this widget plays that role directly,
    mounted as a sibling of `_FindingsListView` rather than on a separate screen."""

    DEFAULT_CSS = """
    _InlineApprovalChat {
        height: auto;
        margin-top: 1;
    }
    """

    def __init__(self, prefill: str, *, owner: FindingsList) -> None:
        super().__init__()
        self._prefill = prefill
        self._owner = owner

    def compose(self) -> ComposeResult:
        yield Static("What should this step do?")
        yield Input(value=self._prefill)

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._owner._resolve_chat(event.value)
        self.remove()


_FOOTER_HINT = (
    "Enter to confirm  |  left/right or 1-9 browse options  |  a/s/x approve/skip/abort"
    "  |  f to type"
)


class FindingsList(Vertical):
    """A bordered box showing the most recently completed step's findings (see
    `state.py`'s `latest_findings`, which recognizes a `ReviewOutput` (`ReviewStep`), a
    `TestSufficiencyOutput` (`TestSufficiencyStep`), or a bare `list[Finding]`
    (`RebaseStep`, issue #87's widening) equally): one `Finding` row per finding inside a
    child `_FindingsListView`, plus a trailing severity-count summary line, unchanged in
    wording from #77. `border_title` names the owning step (issue #74, e.g.
    `"Findings -- ReviewStep"`) so it's clear which step's output is on display.

    Issue #91 rebuilt this as a true widget-composition tree (`FindingsList` hosting
    `_FindingsListView` hosting one `Finding` per finding, each composing
    `FindingsDescription`/`FindingsSuggestion`) in place of the old `FindingsBox`'s single
    `OptionList` of Rich-rendered options -- see `tui/AGENTS.md`'s "Findings box" section
    for the full design rationale. Only the finding currently under the cursor shows
    anything in its `FindingsSuggestion` column (issue #88, deliberately preserved, not
    revisited by #91); arrow keys move the cursor via `_FindingsListView`'s own built-in
    up/down bindings.

    No longer display-only (issue #87, superseding #42/#61's "no key or action here lets
    a user approve, fix, skip, or abort a finding" -- see docs/GLOSSARY.md's "Action"):
    while a step is actually parked, `await_decision` turns the highlighted row's
    `FindingsSuggestion` into a live decision selector -- see that method's docstring, and
    `_FindingsListView`/`_InlineApprovalChat` above -- replacing `ApprovalPromptScreen`'s
    modal entirely. Outside a park, the box behaves exactly as #88 already shipped:
    read-only, only the highlighted finding's suggestions shown, no key here does
    anything. A `#findings-footer` `Static` beneath the summary line shows bound-key copy
    only while parked, matching this package's "no box, not an empty box" instinct applied
    at the sub-widget level.

    A `Vertical`, not a `_BorderedBox` (`Static`) subclass like `PipelineBox`/`StatusBox`
    -- it needs three children (`_FindingsListView`, the summary `Static`, the footer-hint
    `Static`), which a `Static` can't host. `_BorderedBox`'s border/padding rule is
    duplicated here rather than shared, since this widget can no longer extend that base
    alongside `Vertical`.
    """

    DEFAULT_CSS = """
    FindingsList {
        border: round $primary;
        padding: 0 1;
        height: auto;
    }

    FindingsList > _FindingsListView {
        height: auto;
    }

    FindingsList .footer-hint {
        color: $text-muted;
    }
    """

    def __init__(
        self,
        output: ReviewOutput | TestSufficiencyOutput | list[FindingData],
        step_name: str,
        *,
        id: (str | None) = None,  # noqa: A002 -- matches Textual's own Widget.__init__ shape
        classes: str | None = None,
    ) -> None:
        super().__init__(id=id, classes=classes)
        self._output = output
        self.border_title = f"Findings -- {step_name}"
        # Set only for the duration of `await_decision` -- see that method's docstring.
        self._parked = False
        self._pending: asyncio.Future[ApprovalResponse] | None = None
        # The `Finding` row `on_list_view_highlighted` most recently hid -- tracked so the
        # handler can un-highlight it without re-deriving it from `_FindingsListView`'s own
        # (already-advanced) `index`. `None` before anything has ever been highlighted.
        self._last_highlighted: Finding | None = None
        # This box's own authoritative list of `Finding` rows, in order -- `update_findings`
        # reconciles against *this*, never against `_FindingsListView.children` fresh each
        # call. See that method's docstring for why: a widget's constructor-supplied
        # children (here, these same `Finding` instances, passed to `_FindingsListView`
        # below) are only flushed into Textual's own `_nodes` once that widget's `Compose`
        # message is later processed by the message pump -- `update_findings` can run
        # before that happens, at which point `_FindingsListView.children` would (still,
        # briefly) undercount them.
        self._rows = [Finding(finding) for finding in _findings_of(output)]

    def compose(self) -> ComposeResult:
        yield _FindingsListView(*self._rows, owner=self)
        yield Static(_findings_summary(self._output), id="findings-summary")
        yield Static("", id="findings-footer", classes="footer-hint")

    def on_mount(self) -> None:
        # Safety net: `_FindingsListView`'s own initial `index=0` may or may not have
        # already posted `Highlighted` (and therefore reached `on_list_view_highlighted`
        # below) by the time this runs -- explicitly prime row 0 rather than depend on
        # message-delivery ordering between two independently-mounted widgets.
        self._prime_highlighted()

    def update_findings(
        self, output: ReviewOutput | TestSufficiencyOutput | list[FindingData], step_name: str
    ) -> None:
        """Replace the displayed findings with `output`'s, and update `border_title` to
        name `step_name` (issue #74).

        `app.py`'s `_render` runs on a periodic timer, so a freshly `self.mount()`ed
        `FindingsList` can receive an `update_findings` call before Textual has actually
        finished mounting its own `compose()`-yielded children -- `self.mount()` returns
        as soon as the widget is attached to the DOM, not once its subtree is fully
        mounted. `self._output`/`border_title` are always updated regardless (neither
        needs a mounted child), and the child rebuild is skipped rather than raised when
        `_FindingsListView` itself isn't there yet -- `compose()` already renders from
        `self._rows`, so once mounting does finish it reflects this call's data anyway.

        Since that same periodic timer calls this on every tick regardless of whether
        `output` actually changed, the common case (finding count unchanged) updates every
        existing `Finding` row in place via `update_finding` -- touching no DOM structure
        at all, so `_FindingsListView.index`, every row's own `_decision_cursor`/`_mode`,
        and any mounted `_InlineApprovalChat` (a sibling of `_FindingsListView`, untouched
        by this method regardless) all survive completely untouched. Only the finding
        count actually growing or shrinking mounts or removes rows, and only the ones
        beyond the overlap with the old list -- every retained row, including the
        highlighted one, is still updated in place first.

        Reconciles against `self._rows` (this box's own authoritative row list, extended/
        trimmed in step below), never against a fresh `_FindingsListView.children` read --
        two independent races rule that out, both confirmed empirically against this
        Textual version, not just reasoned about:

        - Removal is asynchronous (`ListItem.remove()` posts a `Prune` message, actually
          dropped from `self._nodes` only once that message is later dispatched), but this
          method runs synchronously from `app.py`'s non-async `_render()` with no `await`
          point to let that settle. The more obvious-looking "rebuild" shape --
          `_FindingsListView.clear()` followed by `.extend(...)` -- leaves `_nodes` briefly
          containing *both* the old (still-pruning) and new rows, so setting `.index` right
          after would highlight a stale, about-to-be-removed row instead of the intended
          new one. Reconciling the overlap/tail directly (update retained rows in place,
          mount/remove only the non-overlapping tail) sidesteps this, since it never asks
          `.index` to point past a row that might not have settled into `_nodes` yet.
        - Mounting is *also* asynchronous in the other direction: a widget's constructor-
          supplied children (here, `_FindingsListView(*self._rows, owner=self)` in
          `compose()`) are only flushed into its own `_nodes` once that widget's own
          `Compose` message is later processed -- `update_findings` can run before that
          happens (the same "freshly mounted, not yet composed" gap as the paragraph
          above, just one level deeper). Reading `_FindingsListView.children` at that
          moment would undercount `self._rows` and mount duplicate `Finding` widgets for
          rows that already exist, just not yet flushed. `self._rows` itself is immune to
          this, since it's a plain list this class alone owns and mutates, not a live view
          into Textual's own mount queue.
        """

        self._output = output
        self.border_title = f"Findings -- {step_name}"
        try:
            list_view = self.query_one(_FindingsListView)
            summary = self.query_one("#findings-summary", Static)
        except NoMatches:
            return

        new_findings = _findings_of(output)
        overlap = min(len(self._rows), len(new_findings))
        for item, finding in zip(self._rows[:overlap], new_findings[:overlap], strict=True):
            item.update_finding(finding)

        if len(new_findings) > overlap:
            added = [Finding(finding) for finding in new_findings[overlap:]]
            list_view.extend(added)
            self._rows.extend(added)
        elif len(self._rows) > overlap:
            removed = self._rows[overlap:]
            del self._rows[overlap:]
            for item in removed:
                item.remove()
            # If the row `on_list_view_highlighted` most recently hid is one of the rows
            # just removed, drop the reference now rather than leaving it to that handler:
            # `.index`'s reassignment below posts a fresh `Highlighted` message (dispatched
            # later, once this row has actually been pruned from the DOM), and calling
            # `.set_hidden()` on an already-unmounted `Finding` there raises `NoMatches`
            # instead of a merely stale render.
            if self._last_highlighted in removed:
                self._last_highlighted = None
            if list_view.index is not None and list_view.index >= len(new_findings):
                list_view.index = len(new_findings) - 1 if new_findings else None

        if new_findings and list_view.index is None:
            list_view.index = 0

        summary.update(_findings_summary(output))

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        """Hide the previously-highlighted row's suggestions and show the newly
        highlighted one's (issue #88, kept by #91) -- `plain` outside a park, or `decision`
        (with `_decision_cursor` reset to 0 first, issue #87) while parked, since moving to
        a different finding always starts its own decision cycle fresh rather than
        carrying over whatever index the previous row happened to be on."""

        if self._last_highlighted is not None:
            self._last_highlighted.set_hidden()
        item = event.item
        self._last_highlighted = item if isinstance(item, Finding) else None
        if self._last_highlighted is None:
            return
        if self._parked:
            self._last_highlighted.reset_decision()
            self._last_highlighted.set_decision()
        else:
            self._last_highlighted.set_plain()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        """Confirm whatever `_decision_cursor` currently points at for the selected row's
        finding (issue #87) -- a no-op outside a park, matching every other interactive
        binding here."""

        if not self._parked:
            return
        item = event.item
        assert isinstance(item, Finding)
        entry = item.confirmed_entry()
        if entry in _DECISION_ENTRIES:
            self._quick_decision(cast(ApprovalDecision, entry))
        elif entry == _CUSTOM_ENTRY:
            self._open_chat("")
        else:
            self._open_chat(entry)

    def _prime_highlighted(self) -> None:
        item = self._highlighted_finding()
        if item is None:
            return
        item.set_decision() if self._parked else item.set_plain()
        self._last_highlighted = item

    def _list_view(self) -> _FindingsListView | None:
        """This box's `_FindingsListView`, or `None` when it hasn't composed yet -- the
        same "freshly mounted, not yet composed" gap `update_findings`/`Finding`'s own
        guards defend against, here for the one level above those (`FindingsList` itself).
        Every caller below treats `None` as "nothing to do yet, will settle on its own"."""

        try:
            return self.query_one(_FindingsListView)
        except NoMatches:
            return None

    async def _await_list_view(self) -> _FindingsListView | None:
        """Give `_FindingsListView` a few event-loop turns to exist if it doesn't yet
        (`_list_view`'s docstring), rather than the fire-and-forget "skip, settles on its
        own" every synchronous caller of `_list_view()` uses -- `await_decision` (the only
        async caller) needs its `.focus()` call to actually land once `_FindingsListView`
        does exist, not silently never happen for the rest of that park just because this
        coroutine started running a beat before this box finished composing. Bounded, not
        indefinite: if it never turns up (box already torn down, or some future bug), give
        up and return `None` rather than hang the whole park."""

        for _ in range(10):
            list_view = self._list_view()
            if list_view is not None:
                return list_view
            await asyncio.sleep(0)
        return None

    def _highlighted_finding(self) -> Finding | None:
        list_view = self._list_view()
        if list_view is None:
            return None
        item = list_view.highlighted_child
        # `_FindingsListView.__init__` asserts every mounted child is a `Finding`, and this
        # module never mounts anything else into it -- see that class's docstring.
        return cast("Finding | None", item)

    def _cycle_decision(self, delta: int) -> None:
        if not self._parked:
            return
        item = self._highlighted_finding()
        if item is None:
            return
        item.cycle_decision(delta)

    def _jump_decision(self, index: int) -> None:
        if not self._parked:
            return
        item = self._highlighted_finding()
        if item is None:
            return
        item.jump_decision(index)

    def _quick_decision(self, decision: ApprovalDecision) -> None:
        if not self._parked or self._pending is None:
            return
        self._pending.set_result(ApprovalResponse(decision=decision, instructions=None))

    def _open_chat(self, prefill: str) -> None:
        if not self._parked or self.query(_InlineApprovalChat):
            return
        self.mount(_InlineApprovalChat(prefill, owner=self))

    def _resolve_chat(self, instructions: str) -> None:
        if self._pending is not None:
            self._pending.set_result(ApprovalResponse(decision="fix", instructions=instructions))

    def _set_footer_hint(self, parked: bool) -> None:
        try:
            footer = self.query_one("#findings-footer", Static)
        except NoMatches:
            return
        footer.update(_FOOTER_HINT if parked else "")

    async def await_decision(self) -> ApprovalResponse:
        """Turn the highlighted row's `FindingsSuggestion` into a live decision selector
        until a human confirms Approve/Skip/Abort (directly, or via the inline chat widget
        resolving with `decision="fix"`) -- the inline replacement for
        `ReviewApp._relay_approval`'s old `push_screen_wait(ApprovalPromptScreen(...))`
        (issue #87).

        The decision is step-scoped, not per-finding (an explicit design call, since a
        park is a single step-level event): confirming Approve/Skip/Abort from *any*
        finding's row resolves the one pending future here, regardless of which row the
        cursor was on -- each row's own `_decision_cursor`/left-right cycling is purely a
        per-row browsing aid, not separate state per finding. Only one `await_decision`
        call can be pending at a time in practice (`ReviewApp._relay_approval` awaits one
        park at a time), so a single `self._pending` future, not a queue, is enough.

        Resets the highlighted row's `_decision_cursor` to 0 and switches it into decision
        mode before waiting, so a park always starts that finding's cycle fresh, then
        restores `self._parked = False` and that row back to `set_plain()` in a `finally`
        -- once resolved, the box reverts to #88's plain read-only display for whatever
        `update_findings` call comes next, as if it had never been parked.

        Also focuses `_FindingsListView` -- unlike the old `ApprovalPromptScreen`, becoming
        the active screen on `push_screen` and so implicitly receiving every keypress, this
        box is just one mounted widget among others; without an explicit `.focus()` here, a
        real run's "a"/"s"/"x"/"f" keypresses would land on whatever (if anything) last had
        focus instead of `_FindingsListView`'s bindings, and nothing would happen at all.
        Awaits `_await_list_view()` first (rather than the plain `_list_view()` every other
        caller in this class uses) specifically so this focus call -- unlike a merely
        stale render elsewhere, a silently-skipped focus would leave every keypress for the
        rest of this park going nowhere -- actually lands once `_FindingsListView` exists,
        even if this coroutine started running while `FindingsList` was still composing.

        `_set_footer_hint(True)` runs *after* that same await, not before it -- the ordinary
        case in production is `_relay_approval` calling this immediately after
        `_render_findings` mounted a brand-new `FindingsList` for this step's first park,
        with no intervening `await` of its own, so `FindingsList.compose()` (which yields
        `#findings-footer`) has typically not run yet either. `_set_footer_hint` itself only
        guards against `NoMatches` and gives up silently (the plain, synchronous pattern
        every other caller in this class uses, correct for a merely stale render) -- but
        nothing else ever calls it again for the rest of this park, unlike `Finding`'s own
        `_apply_mode`-on-compose catch-up, so calling it before the box has settled would
        leave the footer hint blank for the entire park, not just one frame.
        """

        self._parked = True
        list_view = await self._await_list_view()
        self._set_footer_hint(True)
        item = self._highlighted_finding()
        if item is not None:
            item.reset_decision()
            item.set_decision()
            self._last_highlighted = item
        if list_view is not None:
            list_view.focus()
        self._pending = asyncio.get_running_loop().create_future()
        try:
            return await self._pending
        finally:
            self._parked = False
            self._pending = None
            if item is not None:
                item.set_plain()
            self._set_footer_hint(False)


class StatusBox(_BorderedBox):
    """A bordered box shown once the pipeline run finishes, successfully or not (see
    `app.py`'s `_render_status`/`state.py`'s `final_status_message`): a one-line outcome
    plus the reminder that "e" now closes the app. Mounted dynamically, only once the run
    is done -- a still-running pipeline shows no Status box at all, mirroring
    `FindingsList`'s own dynamic mount pattern (`app.py`'s `_render_findings`)."""

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
