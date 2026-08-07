"""The Pipeline box: one line per registry step, live status icon, elapsed/final duration.

The Findings box (issue #42, widened for #61/#87, rebuilt as a widget tree by #91): the
most recently completed step's `ReviewOutput`, `TestSufficiencyOutput`, or bare
`list[Finding]`, one `Finding` row per finding plus a severity-count summary -- and, while
a step is parked (issue #87, reworked per-finding by #98), a live inline decision selector
replacing the old `ApprovalPromptScreen` modal: each finding's own `suggestions` plus a
single "Chat about it" entry that opens a free-text chat, always recording "fix" for
whichever row is highlighted. Approve is no longer a reachable per-finding menu entry
(issue #87 later simplified this -- just describe what you want in the chat instead).
Skip ("s") records "skip" for the highlighted row the same per-row way (issue #98 -- see
`FindingsList.await_decision`'s docstring); the park itself only resolves once every row
has a decision, aggregating them into one `ApprovalResponse`. Abort ("x") is the one
binding that stays a separate, global, step-scoped control -- it stops the whole run
outright regardless of how many rows are already decided, since "abort one finding" has no
coherent meaning (a human would just skip that finding instead).

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
from code_review.pipeline.findings import describe_finding_decisions
from code_review.pipeline.step import ApprovalDecision, ApprovalResponse
from code_review.steps.review import ReviewOutput
from code_review.steps.test_sufficiency import TestSufficiencyOutput
from code_review.tui.state import ActivityRow, Status, StepRow

# `FindingsList`'s per-finding decision cycle (issue #87, kept by #91, simplified to drop
# the fixed approve/skip/abort entries below), appended after that finding's own
# `suggestions` -- shared by every finding row, each row cycling through its own copy of
# this list independently (see `FindingsList.await_decision`'s docstring for the per-row
# decision model issue #98 introduced). A plain string, not an `ApprovalDecision` value,
# because "Chat about it" is not itself a decision -- it opens the inline chat widget
# rather than recording anything on its own; confirming it (or a suggestion) always records
# "fix" for the row it was confirmed on, whatever the human types being the `instructions`.
# Rendered as the numbered list's trailing free-text option (see `render_decision_cycle`),
# after every suggestion. Approve/skip/abort used to be listed here too (three more fixed
# entries, resolving the park directly with no chat involved) -- removed once the product
# call landed that a human can just describe what they want in the chat instead, with no
# separate intent-parsing needed for the common case. Abort survives as `_FindingsListView`'s
# own global "x" binding instead (`action_quick_abort`), not a per-finding menu entry -- a
# whole-run action with no coherent per-finding meaning (see `_quick_decision`'s own
# docstring). Skip survives as `_FindingsListView`'s "s" binding too (`action_quick_skip`),
# but -- as of issue #98 -- records a per-row "skip" decision rather than resolving the park
# directly; it remains a bare escape hatch for a finding the chat genuinely cannot resolve
# (e.g. a step that ignores the chat's `fix_round` instructions entirely -- `tui/AGENTS.md`'s
# "Findings box" section has a real example).
_CUSTOM_ENTRY = "Chat about it"

# Short static UI copy for `_CUSTOM_ENTRY`, shown as an indented detail line beneath it in
# `render_decision_cycle` -- a suggestion's own text stays single-line, since it has no
# further data to split a detail line from.
_ENTRY_DETAILS: dict[str, str] = {
    _CUSTOM_ENTRY: "Start typing to describe what you want.",
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

# `FindingsList`'s per-row decided marker (issue #98): rendered by `render_description` on
# every row, not just the highlighted one (unlike the decision cycle itself, `FindingsSuggestion`
# only ever shows for the highlighted row -- issue #88's own deliberate rule), since a human
# browsing away from a row they just decided still needs some visible confirmation that it
# was recorded. Reuses this module's existing icon vocabulary rather than inventing new
# glyphs: `_STATUS_ICONS["completed"]` (✔) for a "fix"-decided row and
# `_STATUS_ICONS["skipped"]` (⏭) for a "skip"-decided one, both already meaning roughly "this
# is settled, not still pending" for a `StepRow`/`ActivityRow` -- the same meaning they carry
# here, just one widget level down. Only "fix"/"skip" ever key these -- `Finding.
# record_decision`'s only two callers (`FindingsList._resolve_chat`/`_quick_decision`) never
# record "approve"/"abort" against a single row (see `_quick_decision`'s own docstring for why
# abort stays a whole-run action) -- but both dicts are typed by the full `ApprovalDecision`
# rather than a narrower alias so `render_description` can pass an `ApprovalDecision | None`
# straight through with no cast, silently rendering no marker for a key neither dict defines.
_DECISION_MARKER_ICONS: dict[ApprovalDecision, str] = {
    "fix": _STATUS_ICONS["completed"],
    "skip": _STATUS_ICONS["skipped"],
}
_DECISION_MARKER_STYLES: dict[ApprovalDecision, str] = {
    "fix": _STATUS_DOT_STYLES["completed"],
    "skip": _STATUS_DOT_STYLES["skipped"],
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
    #87): that finding's own `suggestions`, then a single trailing `_CUSTOM_ENTRY` -- every
    entry here is discussion-only, opening the inline chat widget rather than recording
    anything by itself (see `FindingsList.await_decision`); confirming any of them always
    records "fix" for whichever row it was confirmed on (issue #98 -- previously resolved
    the whole park directly; now records only that row's own decision), seeded with that
    entry's own text (empty for `_CUSTOM_ENTRY`). Approve/skip/abort used to also live in
    this list as three more fixed entries, resolving the park directly with no chat step --
    removed once the product call landed that describing what you want in the chat covers
    that ground with no separate intent-parsing needed, for most cases. Abort remains
    reachable only as a global key binding (`_FindingsListView`'s "x"), never a listed entry
    here -- a whole-run action with no per-finding meaning (see `_quick_decision`'s own
    docstring); approve has no equivalent escape hatch and stays gone. Skip remains
    reachable the same way (`_FindingsListView`'s "s"), but -- as of issue #98 -- records a
    per-row "skip" decision rather than resolving the whole park; it stays a bare escape
    hatch for a finding the chat genuinely cannot resolve, e.g. a step that ignores
    `ctx.fix_round` outright (see `tui/AGENTS.md`'s "Findings box" section). Rendered as a
    1-based numbered list by `render_decision_cycle`, so a
    digit key (`_FindingsListView`'s `"1"`.."9"` bindings) can jump a row's
    `_decision_cursor` straight to any entry here by that same 1-based index."""

    return [*finding.suggestions, _CUSTOM_ENTRY]


def render_description(finding: FindingData, decision: ApprovalDecision | None = None) -> Text:
    """`FindingsDescription`'s content: a colored `_DOT_ICON` (`_SEVERITY_DOT_STYLES`,
    keyed by `finding.severity`) -- the per-finding risk indicator issue #77 asks for,
    reusing `severity` rather than a new field -- followed by `format_finding`'s existing
    severity/description/location text. No `no_wrap` -- `FindingsDescription`'s own
    `width: 1fr` (see that class's docstring) is a bounded column, not an auto-sized one, so
    a long description wraps within it rather than needing to stay on one physical line.

    `decision` (issue #98) is this row's own recorded park decision -- `None` by default,
    which renders byte-for-byte identical to this function's pre-#98 output, so every
    existing non-parked call site (`FindingsDescription.__init__`, and every direct test
    call that passes only `finding`) is unaffected. `FindingsSuggestion` only ever shows
    for the highlighted row (issue #88's own deliberate rule, unchanged) -- `_STATUS_ICONS`'s
    completed/skipped glyphs prefixed here instead (`_DECISION_MARKER_ICONS`/`_STYLES`) are
    what let a human tell a "fix"-decided row apart from a "skip"-decided or still-undecided
    one while browsing *any* row during a park, highlighted or not; an undecided row (while
    parked) renders with no marker at all, the same as `decision=None`, since "no marker" is
    already an unambiguous third state once at least one other row visibly has one. A
    `decision` value neither `_DECISION_MARKER_ICONS` nor `_STYLES` defines (i.e. anything
    other than "fix"/"skip" -- `Finding.record_decision`'s only two callers never pass
    anything else) also renders with no marker, matching this module's "no exceptions on
    data outside its documented shape" style elsewhere rather than raising `KeyError`."""

    text = Text()
    if decision is not None:
        icon = _DECISION_MARKER_ICONS.get(decision)
        if icon is not None:
            text.append(f"{icon} ", style=_DECISION_MARKER_STYLES[decision])
    text.append(_DOT_ICON, style=_SEVERITY_DOT_STYLES[finding.severity])
    text.append(f" {format_finding(finding)}")
    return text


def render_suggestions_plain(finding: FindingData) -> Text:
    """`FindingsSuggestion`'s content outside a decision cycle: `finding.suggestions`, one
    per line, or an empty `Text` when there are none -- never a placeholder string like
    `"None"` (a `no-op`/`auto-fix` finding has nothing for a human to choose between)."""

    return Text("\n".join(finding.suggestions))


def _render_decision_entry(
    index: int, entry: str, decision_cursor: int, *, has_own_suggestions: bool
) -> Text:
    """One line (plus, for `_CUSTOM_ENTRY`, its indented detail line) of a decision cycle --
    factored out of `render_decision_cycle` (issue #92) once `FindingsSuggestion` needed to
    render the trailing `_CUSTOM_ENTRY` line separately from every entry before it (see that
    class's docstring for why): `render_decision_cycle_head`/`render_custom_entry_line`
    below both call this same per-entry renderer, so the numbering/marker/"(Recommended)"/
    detail-line rules stay defined in exactly one place regardless of which of the three
    callers is asking for a given entry."""

    marker = "> " if index == decision_cursor else "  "
    recommended = " (Recommended)" if index == 0 and has_own_suggestions else ""
    text = Text(f"{marker}{index + 1}. {entry}{recommended}\n")
    detail = _ENTRY_DETAILS.get(entry)
    if detail is not None:
        text.append(f"      {detail}\n")
    return text


def render_decision_cycle(finding: FindingData, decision_cursor: int) -> Text:
    """The full decision cycle (issue #87, kept by #91): every entry of `_decision_entries`,
    numbered from 1 (matching `_FindingsListView`'s digit-key shortcuts), with a leading
    `"> "` marking whichever index `decision_cursor` names instead of a plain two-space
    indent. Entry 0 is additionally labeled `" (Recommended)"` when it came from
    `finding.suggestions` itself (i.e. this finding has at least one suggestion, so
    `_decision_entries`'s first entry is that suggestion rather than `_CUSTOM_ENTRY`) --
    styled after the Claude Code CLI's own interactive picker.

    No longer `FindingsSuggestion`'s own content while parked (issue #92 split that column's
    rendering into `render_decision_cycle_head` (entries before `_CUSTOM_ENTRY`) plus
    `render_custom_entry_line` (that trailing entry alone), so the last one can be replaced
    in place by a live `Input` -- see `FindingsSuggestion`'s docstring). Kept as the one
    function that renders the whole cycle in one call, since it's still the simplest pure
    surface to unit-test the shared per-entry rules (`_render_decision_entry`) against."""

    entries = _decision_entries(finding)
    text = Text()
    for index, entry in enumerate(entries):
        text.append(
            _render_decision_entry(
                index, entry, decision_cursor, has_own_suggestions=bool(finding.suggestions)
            )
        )
    return text


def render_decision_cycle_head(finding: FindingData, decision_cursor: int) -> Text:
    """`FindingsSuggestion`'s entries `Static` (issue #92): every `_decision_entries` entry
    except the trailing `_CUSTOM_ENTRY`, rendered exactly as `render_decision_cycle` would --
    the entry after this list is drawn separately, by `render_custom_entry_line`/a live
    `Input`, so it is deliberately excluded here rather than included and then hidden."""

    entries = _decision_entries(finding)
    text = Text()
    for index, entry in enumerate(entries[:-1]):
        text.append(
            _render_decision_entry(
                index, entry, decision_cursor, has_own_suggestions=bool(finding.suggestions)
            )
        )
    return text


def render_custom_entry_line(finding: FindingData, decision_cursor: int) -> Text:
    """The trailing `_CUSTOM_ENTRY`'s own line (issue #92), rendered exactly as
    `render_decision_cycle` would -- `FindingsSuggestion` shows this instead of a live
    `Input` whenever that `Input` isn't (yet) mounted, i.e. whenever the cursor hasn't been
    deliberately moved onto this entry via a confirm/cycle/jump that opens the chat (see
    `FindingsSuggestion.show_decision`'s docstring)."""

    entries = _decision_entries(finding)
    index = len(entries) - 1
    return _render_decision_entry(
        index, entries[index], decision_cursor, has_own_suggestions=bool(finding.suggestions)
    )


def _findings_summary(output: ReviewOutput | TestSufficiencyOutput | list[FindingData]) -> str:
    """Render `output`'s severity-count summary, e.g. `1 error, 2 warning, 0 info` --
    `FindingsList`'s own summary line."""

    counts = {severity: 0 for severity in ("error", "warning", "info")}
    for finding in _findings_of(output):
        counts[finding.severity] += 1
    return f"{counts['error']} error, {counts['warning']} warning, {counts['info']} info"


class FindingsDescription(Static):
    """The left column of one `Finding` row (issue #91): severity dot, description,
    location -- and, while parked (issue #98), a decided-marker prefix (`render_description`'s
    `decision` parameter), since this column is the only one visible on every row regardless
    of highlight state (`FindingsSuggestion` -- see below -- only ever shows for the
    highlighted row, issue #88).

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

    def update_finding(
        self, finding: FindingData, decision: ApprovalDecision | None = None
    ) -> None:
        self.update(render_description(finding, decision))


class FindingsSuggestion(Vertical):
    """The right column of one `Finding` row (issue #91) -- tri-state (hidden/plain/
    decision), since only the highlighted row shows anything (issue #88, kept by #91):
    every other row's `FindingsSuggestion` stays cleared. Mode switching is `Finding`'s
    job (see `set_hidden`/`set_plain`/`set_decision` below), not this widget's own -- it
    only knows how to render each of the three states, not when to be in one.

    A `Vertical` composing two children, not a plain `Static` (issue #92, superseding #91's
    single-`Static` shape) -- decision mode needs to replace its trailing `_CUSTOM_ENTRY`
    line with a live `Input` in place, and a `Static`'s `renderable` has no way to host a
    real child widget. `self._entries` (a `Static`) renders every entry before
    `_CUSTOM_ENTRY` (`render_decision_cycle_head`/`render_suggestions_plain`); `self._custom`
    (another `Static`) renders `_CUSTOM_ENTRY`'s own line (`render_custom_entry_line`) --
    swapped out for `self._input`, a real `Input`, the moment a human actually opens the chat
    (see `show_decision`'s docstring for exactly when that is). This replaces issue #87's
    `_InlineApprovalChat`, which used to mount as a sibling of `_FindingsListView` below the
    whole box instead of inside this column -- see `tui/AGENTS.md`'s "Findings box" section
    for the full rationale.

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
    line. `border_title = "Suggestion"` is set directly in `__init__`, the same way
    `PipelineBox`/`FindingsList`/`StatusBox` set theirs -- it only actually renders once a
    border is present (i.e. once `-visible` is added), so it costs nothing while hidden and
    labels the column the instant it draws its own border."""

    DEFAULT_CSS = """
    FindingsSuggestion {
        width: 1fr;
        padding: 0 1;
        display: none;
        height: auto;
    }

    FindingsSuggestion.-visible {
        display: block;
        border: round $primary-darken-1;
    }

    FindingsSuggestion Input {
        border: none !important;
        height: 1 !important;
        padding: 0 1;
    }

    FindingsSuggestion .-chat-hint {
        color: $text-disabled;
        height: 1;
        padding: 0 1;
    }
    """

    def __init__(
        self,
        *,
        id: str | None = None,  # noqa: A002 -- matches Textual's own Widget.__init__ shape
        classes: str | None = None,
    ) -> None:
        super().__init__(id=id, classes=classes)
        self.border_title = "Suggestion"
        self._entries = Static("")
        self._custom = Static("")
        # Set only once a human deliberately opens the chat (`ensure_input`) -- `None`
        # covers both "not parked"/"plain mode" and "parked, cursor on `_CUSTOM_ENTRY`, but
        # nobody has opened it yet", both of which render `self._custom`'s plain text
        # instead (see `show_decision`).
        self._input: Input | None = None
        # Mounted/removed in lockstep with `self._input` (issue #95) -- a one-line reminder
        # of the Escape binding, shown only while the chat is actually open.
        self._hint: Static | None = None

    def compose(self) -> ComposeResult:
        yield self._entries
        yield self._custom

    def clear(self) -> None:
        self.remove_class("-visible")
        self._entries.update("")
        self._custom.update("")
        self._remove_input()

    def show_plain(self, finding: FindingData) -> None:
        self.add_class("-visible")
        self._entries.update(render_suggestions_plain(finding))
        self._custom.update("")
        self._remove_input()

    def show_decision(self, finding: FindingData, decision_cursor: int) -> None:
        """Render decision mode: `self._entries` always gets every entry before
        `_CUSTOM_ENTRY` (`render_decision_cycle_head`); the trailing `_CUSTOM_ENTRY` slot
        itself shows `self._custom`'s plain text (`render_custom_entry_line`) UNLESS a chat
        is already open on this row (`self._input is not None`, set by `ensure_input`), in
        which case that live `Input` stays exactly as it is -- untouched, not rebuilt -- and
        `self._custom` is left empty so it contributes nothing while hidden behind it.

        Deliberately never opens the chat itself, even when `decision_cursor` already points
        at `_CUSTOM_ENTRY` (e.g. a finding with no suggestions of its own, whose entry 0 is
        `_CUSTOM_ENTRY`, or a redundant `update_findings` re-render while parked) -- only a
        human's deliberate confirm/cycle/jump onto that entry does that, via `ensure_input`
        (see `Finding.open_chat`/`tui/AGENTS.md`'s "Findings box" section for why merely
        landing on it via a plain highlight must never yank focus into a chat box). This is
        also what makes a redundant `show_decision` call at the same cursor safe to call
        over and over from `update_findings`' every-tick re-render: with no already-open
        `Input` to touch, it just re-renders the same plain text again; with one already
        open, it renders `self._entries` fresh but leaves that `Input` -- and whatever a
        human has typed into it -- completely alone."""

        self.add_class("-visible")
        self._entries.update(render_decision_cycle_head(finding, decision_cursor))
        entries = _decision_entries(finding)
        on_custom_entry = decision_cursor == len(entries) - 1
        if self._input is not None:
            if on_custom_entry:
                self._custom.update("")
                return
            # The cursor moved off `_CUSTOM_ENTRY` while its `Input` was still open (not
            # reachable via this row's own left/right/digit bindings today, since those keys
            # go to the focused `Input` itself rather than bubbling to `_FindingsListView`
            # once the chat has focus -- but a defensive cleanup regardless, so a stale
            # `Input` never lingers pointed at the wrong entry).
            self._remove_input()
        self._custom.update(render_custom_entry_line(finding, decision_cursor))

    def ensure_input(self, prefill: str) -> Input:
        """Mount (if not already mounted) this row's live `Input` for `_CUSTOM_ENTRY`,
        seeded with `prefill`, and return it -- a human deliberately opening the chat
        (`Finding.open_chat`, in turn `FindingsList._open_chat`), never `show_decision`'s own
        redundant re-renders. Idempotent: if one is already mounted, it is returned
        untouched, `prefill` ignored -- this is the load-bearing half of "an already-open
        chat's typed value must survive a redundant `update_findings` tick" (see
        `FindingsList.await_decision`'s docstring): re-entering this path (whether from a
        second explicit open, or a fresh `show_decision` call after a same-cursor tick) must
        never reconstruct the `Input`, which would wipe out whatever a human has typed so
        far. Placeholder text is the literal `_CUSTOM_ENTRY` string ("Chat about it") --
        issue #92 dropped `_InlineApprovalChat`'s separate `"What should this step do?"`
        prompt `Static` entirely, so the placeholder is now the only copy explaining what
        this field is for, shown only while it's empty (Textual's own `Input.placeholder`
        behavior)."""

        if self._input is not None:
            return self._input
        self._custom.update("")
        self._input = Input(value=prefill, placeholder=_CUSTOM_ENTRY)
        self.mount(self._input)
        self._hint = Static("Press esc to cancel", classes="-chat-hint")
        self.mount(self._hint)
        return self._input

    def _remove_input(self) -> None:
        if self._input is not None:
            self._input.remove()
            self._input = None
        if self._hint is not None:
            self._hint.remove()
            self._hint = None

    def cancel_input(self, finding: FindingData, decision_cursor: int) -> None:
        """Cancel this row's live chat (issue #95) without resolving anything -- tears
        the `Input` down and re-renders `_CUSTOM_ENTRY`'s plain text in its place, exactly
        as `show_decision` already does whenever no `Input` is mounted. Whatever had been
        typed is discarded, matching Escape's "cancel", not "save draft"."""

        self._remove_input()
        self.show_decision(finding, decision_cursor)


class Finding(ListItem):
    """One row per finding inside `_FindingsListView` (issue #91, superseding the old
    `FindingsBox`'s single `OptionList` of Rich-rendered options) -- composes
    `FindingsDescription`/`FindingsSuggestion` in a horizontal split, and owns this row's
    own display mode (`hidden`/`plain`/`decision`), its own `_decision_cursor` (while
    parked, a purely per-row *browsing* position within this finding's own suggestion list,
    not itself a decision), and, as of issue #98, its own recorded park decision
    (`_row_decision`) -- `None` while undecided, or the `ApprovalResponse` a human confirmed
    for this row specifically (`record_decision`). Confirming the chat or pressing "s" while
    this row is highlighted records *this row's own* decision, not the whole park's -- see
    `FindingsList.await_decision`'s docstring for the full per-row-then-aggregate model
    issue #98 introduced, superseding issue #87's original step-scoped design (every row
    used to resolve the one pending park identically, regardless of which row's cursor
    confirmed it). Abort ("x") is the one exception: it stays `_FindingsListView`'s own
    separate global binding, resolving the whole park directly with no per-row recording
    step at all -- deliberately unchanged by issue #98 (see `FindingsList._quick_decision`'s
    own docstring for why abort alone stays a whole-run action).

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
        # This row's own recorded park decision (issue #98) -- `None` until a human
        # confirms this row's chat ("fix", `record_decision`) or presses "s" while it's
        # highlighted ("skip", also `record_decision`); reset back to `None` at the start
        # of every `FindingsList.await_decision()` park (`clear_decision`, called for every
        # row in `self._rows`) so a fix-round's re-park never carries over the previous
        # round's per-row decisions onto a fresh one. Distinct from `_decision_cursor`
        # above, which is purely a per-row *browsing* position within this finding's own
        # suggestion list, not a decision at all.
        self._row_decision: ApprovalResponse | None = None

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
        left it on. Not to be confused with `clear_decision` below (issue #98) -- this
        resets the *browsing* cursor, not the row's recorded decision."""

        self._decision_cursor = 0

    def is_decided(self) -> bool:
        """True once this row has a recorded decision (issue #98) -- `record_decision` has
        been called at least once since the last `clear_decision`/park start.
        `FindingsList._record_decision` checks this across every row in `self._rows` to
        decide whether to resolve the whole park yet, or just advance the highlighted
        cursor on to the next undecided row."""

        return self._row_decision is not None

    @property
    def row_decision(self) -> ApprovalResponse | None:
        """This row's own recorded decision, or `None` while undecided -- read by
        `FindingsList._resolve_park` once every row is decided, to build the combined
        `ApprovalResponse` (or, for a single-row park, returned completely unwrapped -- see
        that method's own docstring)."""

        return self._row_decision

    def record_decision(self, response: ApprovalResponse) -> None:
        """Record `response` as this row's own decision (issue #98) --
        `FindingsList._resolve_chat` ("fix", from this row's own inline chat `Input`) and
        `_quick_decision("skip")` are this method's only two callers, each only ever acting
        on the currently *highlighted* row. Overwrites whatever this row's previous
        decision was, if any -- a human revisiting an already-decided row (plain up/down
        browsing, already unrestricted) and reconfirming it is meant to overwrite, not be
        rejected, so this deliberately carries no "already decided" guard of its own.
        Re-renders this row's `FindingsDescription` (`_render_description`) so the
        fix/skip marker every row shows regardless of highlight state (`render_description`)
        reflects the change immediately, not just whenever some other, unrelated re-render
        happens to reach this row next."""

        self._row_decision = response
        self._render_description()

    def clear_decision(self) -> None:
        """Reset this row back to undecided (issue #98) -- called for every row in
        `FindingsList.await_decision`'s `self._rows` at the very start of each park, so a
        fix-round's re-park (the same step parking again because a human's typed
        instructions didn't resolve it -- `steps/rebase.py`'s issue #24 guard is exactly
        this case) never starts with the previous round's per-row decisions already
        carried over."""

        self._row_decision = None
        self._render_description()

    def _render_description(self) -> None:
        """Apply this row's current decision marker to `FindingsDescription` (issue #98) --
        guarded the same "skip now, the eventual real `compose()` reflects the
        already-updated state anyway" way `_render_suggestion` already is for
        `FindingsSuggestion`, since `record_decision`/`clear_decision` can equally land on
        a row before its own `compose()` has run."""

        try:
            description = self.query_one(FindingsDescription)
        except NoMatches:
            return
        marker = None if self._row_decision is None else self._row_decision.decision
        description.update_finding(self.finding, marker)

    def update_finding(self, finding: FindingData) -> None:
        """Data changed in place, same list position (`FindingsList.update_findings`'s
        in-place path) -- refresh every child, preserving whichever display mode this row
        is currently in rather than forcing one, and this row's own decision marker (issue
        #98) rather than resetting it -- a periodic re-render tick must never silently
        clear an in-progress per-row decision. Skipped, like `_render_suggestion`/
        `_render_description` themselves, when this row hasn't composed yet -- `self.
        finding` is still updated below regardless, so the eventual real `compose()`
        reflects this call's data anyway."""

        self.finding = finding
        self._render_description()
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

    def open_chat(self, prefill: str) -> Input | None:
        """A human deliberately opened the chat on this row (issue #92) -- via Enter/"f",
        or `_cycle_decision`/`_jump_decision` landing the cursor on `_CUSTOM_ENTRY` (see
        `FindingsList._open_chat`, the sole caller). Moves `_decision_cursor` straight to
        `_CUSTOM_ENTRY` (always the last entry, per `_decision_entries`) regardless of where
        it already was -- confirming a plain suggestion opens the chat *seeded with that
        suggestion's own text*, which only ever has somewhere to render once the cursor
        itself is on the one entry `FindingsSuggestion` can turn into a live `Input` (see
        that class's docstring); confirming `_CUSTOM_ENTRY` directly is already a no-op
        move, landing on the same index it started at. Returns the `FindingsSuggestion`'s
        `Input` (so the caller can focus it) once this row's `FindingsSuggestion` exists, or
        `None` when this row hasn't composed yet -- the same "skip now, the eventual real
        render reflects the already-updated cursor/mode anyway" guard `_render_suggestion`
        already uses, since `_decision_cursor`/`_mode` are updated unconditionally above."""

        entries = _decision_entries(self.finding)
        self._decision_cursor = len(entries) - 1
        self.set_decision()
        try:
            suggestion = self.query_one(FindingsSuggestion)
        except NoMatches:
            return None
        return suggestion.ensure_input(prefill)

    def close_chat(self) -> None:
        """Cancel a chat open on this row (issue #95) -- the Escape counterpart to
        `open_chat`. Leaves `_decision_cursor` exactly where it was (still on
        `_CUSTOM_ENTRY`) and resolves nothing; a no-op if this row hasn't composed yet or
        has no live `Input` open (`FindingsSuggestion.cancel_input`/`_remove_input` are
        both already idempotent)."""

        try:
            suggestion = self.query_one(FindingsSuggestion)
        except NoMatches:
            return
        suggestion.cancel_input(self.finding, self._decision_cursor)


class _FindingsListView(ListView):
    """Direct successor to `FindingsBox`'s old private `_FindingOptionList` (issue #91).
    `ListView(can_focus=True, can_focus_children=False)`: the `ListView` itself holds
    keyboard focus, its `ListItem` children can never be individually focused, and its own
    `action_cursor_up/down`/`watch_index`/`highlighted_child` all index/assert against
    `self._nodes` unfiltered -- so every one of #87's parked-mode key bindings on top of
    up/down/enter (which `ListView`'s own `BINDINGS` already give this for free) lives
    here, never on `Finding` itself: left/right cycle the highlighted finding's
    `_decision_entries`, the single-key "s" shortcut records "skip" for the highlighted row
    specifically (issue #98's per-finding decision model -- see `FindingsList.
    await_decision`'s docstring -- superseding this binding's original step-scoped/global
    resolve), a bare escape hatch for a finding the chat genuinely cannot resolve, e.g. a
    step that ignores `ctx.fix_round` entirely (see `tui/AGENTS.md`'s "Findings box"
    section), "x" jumps straight to abort regardless of cursor position (the one binding
    that stays global and step-scoped -- see `FindingsList._quick_decision`'s own docstring
    for why abort alone never became per-finding), "f" jumps straight to the inline chat the
    same way "s" does, and digit keys "1".."9" jump straight to that 1-based entry (matching
    `render_decision_cycle`'s numbering). All of these delegate to the owning `FindingsList`,
    which no-ops them while not parked -- this class holds no decision state of its own
    (every row's own recorded decision lives on `Finding` itself, see that class's
    docstring)."""

    BINDINGS = [
        Binding("left", "cycle_prev", "Previous suggestion", show=False),
        Binding("right", "cycle_next", "Next suggestion", show=False),
        Binding("s", "quick_skip", "Skip", show=False),
        Binding("x", "quick_abort", "Abort", show=False),
        Binding("f", "open_chat", "Chat", show=False),
        Binding("escape", "close_chat", "Cancel chat", show=False),
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

    def action_quick_skip(self) -> None:
        self._owner._quick_decision("skip")

    def action_quick_abort(self) -> None:
        self._owner._quick_decision("abort")

    def action_open_chat(self) -> None:
        self._owner._open_chat("")

    def action_close_chat(self) -> None:
        self._owner._close_chat()

    def action_jump_decision(self, digit: int) -> None:
        self._owner._jump_decision(digit - 1)


# Reworded by issue #98 -- "Enter to confirm" used to resolve the whole park from any row;
# now Enter (via the chat) or "s" only records *this finding's* decision, and the park only
# actually submits once every row has one. `FindingsList._set_footer_hint` appends a live
# "N/M decided" progress count after this fixed copy while parked (see that method's own
# docstring), recomputed on every recorded decision, not just at park start/end.
_FOOTER_HINT = (
    "Enter to confirm this finding  |  left/right or 1-9 browse options  |  f to chat"
    "  |  s to skip this finding  |  x to abort the run"
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
    `_FindingsListView`/`FindingsSuggestion` above -- replacing `ApprovalPromptScreen`'s
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

    `on_input_submitted` handles `Input.Submitted` here, not on `FindingsSuggestion`/
    `Finding` (issue #92) -- Textual messages bubble up the DOM from wherever they're
    posted, so a handler defined at this level still catches the event fired by whichever
    row's `FindingsSuggestion` currently hosts the live `Input`, exactly the same as
    #87's now-removed `_InlineApprovalChat` handled it on itself before the `Input` moved
    inside the row tree -- only the handler's *location*, not its reach, needed to move.
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
        at the `_FindingsListView` level at all, so `_FindingsListView.index` and every
        row's own `_decision_cursor`/`_mode` survive completely untouched. `update_finding`
        does still reach every row's own `FindingsSuggestion.show_decision` on each such
        tick (issue #92 moved the live chat `Input` inside that column, so it is no longer
        a sibling this method simply never touches) -- but `show_decision` is specifically
        built to leave an already-open `Input` (and whatever a human has typed into it)
        completely alone on a same-cursor re-render; see that method's and
        `FindingsSuggestion.ensure_input`'s docstrings for how. Only the finding count
        actually growing or shrinking mounts or removes rows, and only the ones beyond the
        overlap with the old list -- every retained row, including the highlighted one, is
        still updated in place first.

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
        binding here. Every entry `_decision_entries` can produce is now discussion-only
        (a suggestion, or `_CUSTOM_ENTRY`), so this always opens the inline chat, seeded
        with that entry's own text (empty for `_CUSTOM_ENTRY`) -- a redundant-but-harmless
        second path once `_cycle_decision`/`_jump_decision` already auto-open it the moment
        the cursor arrives at `_CUSTOM_ENTRY`, kept because Enter must still work regardless
        of how the cursor got there (e.g. a fresh row highlight, never cycled at all)."""

        if not self._parked:
            return
        item = event.item
        assert isinstance(item, Finding)
        entry = item.confirmed_entry()
        if entry == _CUSTOM_ENTRY:
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
        """Move the highlighted row's `_decision_cursor` by `delta` (left/right), then open
        the inline chat the moment it *lands* on `_CUSTOM_ENTRY` -- so browsing onto "Chat
        about it" already puts the human straight into typing, no extra Enter/"f" needed.
        Checked here, at `FindingsList` level, rather than inside `Finding.cycle_decision`
        itself: that method stays a pure cursor move with no Textual side effect of its own
        (matching this module's pure/impure split), and `_open_chat` already needs
        `FindingsList`'s own `self._parked` guard, which `Finding` has no access to."""

        if not self._parked:
            return
        item = self._highlighted_finding()
        if item is None:
            return
        item.cycle_decision(delta)
        if item.confirmed_entry() == _CUSTOM_ENTRY:
            self._open_chat("")

    def _jump_decision(self, index: int) -> None:
        """Digit-key counterpart to `_cycle_decision` -- same "open the chat the instant the
        cursor lands on `_CUSTOM_ENTRY`" behavior, including when `Finding.jump_decision`
        itself no-ops (an out-of-range digit): `confirmed_entry()` is checked after the call
        regardless, so an already-open chat (cursor already on `_CUSTOM_ENTRY` before this
        no-op jump) is simply re-confirmed rather than skipped -- harmless, since
        `_open_chat` is itself a no-op once one is already mounted."""

        if not self._parked:
            return
        item = self._highlighted_finding()
        if item is None:
            return
        item.jump_decision(index)
        if item.confirmed_entry() == _CUSTOM_ENTRY:
            self._open_chat("")

    def _quick_decision(self, decision: ApprovalDecision) -> None:
        """ "s"/"x"'s shared entry point (`_FindingsListView.action_quick_skip`/
        `action_quick_abort`) -- the two diverge here as of issue #98. "abort" still
        resolves `self._pending` directly and immediately, regardless of how many rows are
        already decided: a whole-run action with no coherent per-finding meaning ("abort one
        finding" doesn't mean anything -- a human would just skip that finding instead;
        question 4 of the design discussion linked from issue #98). Every other value
        reaching this method today is "skip", recorded against the highlighted row only via
        `_record_decision` -- the same per-row-then-aggregate path `_resolve_chat`'s "fix"
        already goes through."""

        if not self._parked or self._pending is None:
            return
        if decision == "abort":
            self._pending.set_result(ApprovalResponse(decision="abort", instructions=None))
            return
        self._record_decision(decision, None)

    def _open_chat(self, prefill: str) -> None:
        """Open the highlighted row's chat, seeded with `prefill` (issue #92: in place,
        inside that row's own `FindingsSuggestion`, rather than mounting a sibling
        `_InlineApprovalChat` below the whole box). Delegates the actual cursor move/`Input`
        creation to `Finding.open_chat` -- idempotent the same way the old sibling widget
        was (`Finding.open_chat` → `FindingsSuggestion.ensure_input` both no-op once a
        chat is already open on this row), so calling this twice in a row, or once via a
        cycle/jump auto-open and again via a redundant Enter/"f", never stacks or resets
        anything; see `FindingsSuggestion.ensure_input`'s docstring. Focuses the returned
        `Input` here rather than in `Finding`/`FindingsSuggestion` themselves, matching
        `await_decision`'s own `list_view.focus()` -- moving focus is this class's job, the
        row/column below only build the widget to focus."""

        if not self._parked:
            return
        item = self._highlighted_finding()
        if item is None:
            return
        input_widget = item.open_chat(prefill)
        if input_widget is not None:
            input_widget.focus()

    def _close_chat(self) -> None:
        """Escape counterpart to `_open_chat` (issue #95): cancel the highlighted row's
        live chat without resolving `self._pending` -- the park stays open exactly as it
        was before the chat opened. Refocuses `_FindingsListView` explicitly, matching
        `_open_chat`'s own symmetric focus call, since removing the focused `Input` leaves
        Textual with nothing focused otherwise."""

        if not self._parked:
            return
        item = self._highlighted_finding()
        if item is None:
            return
        item.close_chat()
        list_view = self._list_view()
        if list_view is not None:
            list_view.focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Handle the highlighted row's live chat `Input` being submitted (issue #92) --
        wherever it currently lives in the row tree, this `Message` bubbles up to
        `FindingsList` regardless (see this class's own docstring for why the handler moved
        here rather than staying on a since-removed `_InlineApprovalChat`). Delegating to
        `_resolve_chat` is enough on its own either way (issue #98): once every row is
        decided, `await_decision`'s `finally` clause reverts the (still) highlighted row
        back to `set_plain()`, which tears the `Input` down (`FindingsSuggestion.show_plain`
        → `_remove_input`); until then, `_record_decision`'s own
        `_advance_to_next_undecided` moves the highlighted cursor off this row instead,
        which reaches that exact same teardown via `on_list_view_highlighted`'s ordinary
        hide-on-leave path. No explicit cleanup needed in this handler itself either way."""

        self._resolve_chat(event.value)

    def _resolve_chat(self, instructions: str) -> None:
        """The highlighted row's chat `Input` was submitted -- records "fix" for that row
        only (issue #98), via `_record_decision`; see that method's docstring for what
        happens next (either the whole park resolves, or the cursor advances to the next
        undecided row)."""

        self._record_decision("fix", instructions)

    def _record_decision(self, decision: ApprovalDecision, instructions: str | None) -> None:
        """Record `decision`/`instructions` (issue #98) against the currently highlighted
        row only -- `_resolve_chat` ("fix", from the inline chat) and `_quick_decision`
        ("skip") are this method's only two callers; "x" (abort) never reaches it, since it
        stays a whole-run action resolved directly by `_quick_decision` itself.

        Once every row in `self._rows` has its own decision, aggregates them into the one
        final `ApprovalResponse` and resolves the pending park (`_resolve_park`); otherwise
        leaves the park open and moves the highlighted cursor on to the next undecided row
        (`_advance_to_next_undecided`), so a human can act on the step's remaining findings
        one at a time -- the interaction this issue exists to build (see `FindingsList.
        await_decision`'s docstring for the full model). The footer's decided/total progress
        count (`_set_footer_hint`) is recomputed either way, since it must reflect this
        row's just-recorded decision regardless of which branch runs next."""

        if self._pending is None:
            return
        item = self._highlighted_finding()
        if item is None:
            return
        item.record_decision(ApprovalResponse(decision=decision, instructions=instructions))
        self._set_footer_hint(True)
        if all(row.is_decided() for row in self._rows):
            self._resolve_park()
        else:
            self._advance_to_next_undecided()

    def _advance_to_next_undecided(self) -> None:
        """After `_record_decision` records a decision but the park is not yet fully
        decided: move the highlighted cursor to the next undecided row, searching forward
        from the current index and wrapping past the end back to the start (issue #98).
        Reuses `on_list_view_highlighted`'s existing reset-cursor/`set_decision()` plumbing
        for free, rather than duplicating it here, by only ever moving
        `_FindingsListView.index` itself -- assigning it posts a `Highlighted` message that
        handler already reacts to.

        Explicitly refocuses `_FindingsListView` afterward, mirroring `_open_chat`/
        `_close_chat`'s own symmetric focus calls -- the row just decided may have resolved
        via its own chat `Input`, which held keyboard focus while open; that `Input` is torn
        down (asynchronously, once the `Highlighted` message above is actually dispatched)
        by `on_list_view_highlighted`'s ordinary hide-on-leave path, but nothing else claims
        focus on its own once that happens, and every one of this class's own key bindings
        ("s"/"x"/"f"/left/right/digits) lives on `_FindingsListView`, not the `Input`."""

        list_view = self._list_view()
        if list_view is None or not self._rows:
            return
        current = list_view.index if list_view.index is not None else 0
        total = len(self._rows)
        for offset in range(1, total + 1):
            candidate = (current + offset) % total
            if not self._rows[candidate].is_decided():
                list_view.index = candidate
                list_view.focus()
                return

    def _resolve_park(self) -> None:
        """Aggregate every row's now-final decision into the one `ApprovalResponse` that
        resolves `self._pending` (issue #98) -- called by `_record_decision` the moment
        every row in `self._rows` has a decision; never called directly by a key binding.

        A park with exactly one row (e.g. `steps/rebase.py`'s issue #24 guard, which always
        emits exactly one `Finding` -- see the design discussion linked from issue #98's own
        body) is a deliberate special case: resolving with that row's own `ApprovalResponse`,
        completely unwrapped, reproduces this class's pre-#98 immediate-resolve behavior
        byte for byte (pinned by `tests/tui/test_app.py`'s
        `test_review_app_choosing_fix_prompts_for_instructions_then_resolves_with_them`,
        which predates this issue and asserts the exact typed text with no formatting
        applied). There is only one finding in that case, so there is nothing for a
        combined, per-finding-attributed instructions blob to usefully distinguish --
        `describe_finding_decisions`'s `"- [severity] description: instructions"`
        attribution only earns its keep once there are two or more rows for a human reading
        the resulting fix-round prompt to tell apart.

        Otherwise (two or more rows): every `fix`-decided row's instructions are combined
        via `pipeline.findings.describe_finding_decisions` into one `instructions` string,
        resolved with `decision="fix"`; if every row chose `skip` instead
        (`describe_finding_decisions` returns `""` when it has no `fix`-decided row to
        render), resolves with `decision="skip", instructions=None` -- identical in shape to
        this class's own pre-#98 step-level skip."""

        assert self._pending is not None
        decided: list[tuple[FindingData, ApprovalResponse]] = []
        for row in self._rows:
            response = row.row_decision
            if response is not None:
                decided.append((row.finding, response))

        if len(self._rows) == 1:
            resolution = decided[0][1]
        else:
            combined = describe_finding_decisions(decided)
            resolution = (
                ApprovalResponse(decision="fix", instructions=combined)
                if combined
                else ApprovalResponse(decision="skip", instructions=None)
            )
        self._pending.set_result(resolution)

    def _set_footer_hint(self, parked: bool) -> None:
        """Show/clear `#findings-footer`'s bound-key copy (issue #87) -- while parked, also
        appends a live "N/M decided" progress count (issue #98), so a human can tell at a
        glance how many of this park's findings still need a decision. Called both at park
        start/end (`await_decision`) and after every single recorded decision
        (`_record_decision`), since the count must move the instant a decision is recorded,
        not just once at the very start and end of the whole park."""

        try:
            footer = self.query_one("#findings-footer", Static)
        except NoMatches:
            return
        if not parked:
            footer.update("")
            return
        decided = sum(1 for row in self._rows if row.is_decided())
        footer.update(f"{_FOOTER_HINT}  |  {decided}/{len(self._rows)} decided")

    async def await_decision(self) -> ApprovalResponse:
        """Turn the highlighted row's `FindingsSuggestion` into a live decision selector
        until every row in `self._rows` has its own decision and the park resolves -- via
        the inline chat widget (confirming a suggestion or "Chat about it" and submitting
        records "fix" for the highlighted row, see `_resolve_chat`), or
        `_FindingsListView`'s "s" binding (records "skip" for the highlighted row, see
        `_quick_decision`) -- or, for the whole run at once regardless of per-row progress,
        "x" (abort, stopping the run outright) -- the inline replacement for
        `ReviewApp._relay_approval`'s old `push_screen_wait(ApprovalPromptScreen(...))`
        (issue #87), reworked by issue #98 to resolve per finding rather than the instant
        any one row is confirmed.

        Each row's own decision is per-finding, not step-scoped (issue #98, superseding
        #87's original design, where confirming the chat, skipping, or aborting from *any*
        row resolved the one pending park immediately, regardless of which row the cursor
        was on): `_record_decision` records whichever of "fix"/"skip" the highlighted row
        just confirmed onto that `Finding`'s own `_row_decision` (`Finding.record_decision`/
        `is_decided`), then either resolves the park -- once every row has a decision -- or
        moves the highlighted cursor on to the next undecided row
        (`_advance_to_next_undecided`), leaving the park open so a human can act on the
        step's remaining findings one at a time. `self._pending` is still a single future,
        not a queue or one future per row: aggregation into one final `ApprovalResponse`
        (`_resolve_park`, combining every `fix`-decided row's instructions via
        `pipeline.findings.describe_finding_decisions`, or resolving `decision="skip"` if
        every row chose that) happens entirely *before* `self._pending.set_result(...)` is
        ever called, so by the time it is called there is exactly one response to hand back
        -- the same reasoning issue #87 originally gave for a single future, just realized
        one step later in the flow. A park with exactly one row (e.g. `steps/rebase.py`'s
        issue #24 guard, which always emits exactly one `Finding`) degrades to resolving on
        that row's very first decision, with its `ApprovalResponse` passed through
        completely unwrapped -- see `_resolve_park`'s own docstring for why that case is
        special-cased rather than run through the combined-instructions renderer.

        Resets every row's decision back to undecided (`Finding.clear_decision`) at the very
        start of this method, before anything else -- a step's fix-round loop can re-park
        the same `FindingsList` on a fresh round after a human's "fix" instructions didn't
        resolve it (`steps/rebase.py`'s issue #24 guard is exactly this case, since it never
        reads `ctx.fix_round`), and that fresh round must never start with the previous
        round's per-row decisions already carried over.

        Resets the highlighted row's `_decision_cursor` to 0 and switches it into decision
        mode before waiting, so a park always starts that finding's cycle fresh, then
        restores `self._parked = False` and the (possibly since-moved) highlighted row back
        to `set_plain()` in a `finally` -- once resolved, the box reverts to #88's plain
        read-only display for whatever `update_findings` call comes next, as if it had never
        been parked. The `finally` clause reverts `self._last_highlighted`, not the row
        captured when this method started -- issue #98's per-row advance means the
        highlighted row can legitimately change mid-park, and every row it moves away from
        along the way is already reverted to plain by `on_list_view_highlighted`'s own
        hide-on-leave path, so only the row still highlighted when the park actually
        resolves needs this final revert.

        Also focuses `_FindingsListView` -- unlike the old `ApprovalPromptScreen`, becoming
        the active screen on `push_screen` and so implicitly receiving every keypress, this
        box is just one mounted widget among others; without an explicit `.focus()` here, a
        real run's "s"/"x"/"f"/left/right/digit keypresses would land on whatever (if
        anything) last had focus instead of `_FindingsListView`'s bindings, and nothing
        would happen at all.
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
        every other caller in this class uses, correct for a merely stale render) -- but the
        only thing that reliably calls it again for the rest of this park is
        `_record_decision`, on every recorded decision (so its decided/total progress count
        stays current), not a timer, so calling it before the box has settled here would
        otherwise leave the footer hint blank for the entire park, not just one frame.
        """

        self._parked = True
        for row in self._rows:
            row.clear_decision()
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
            if self._last_highlighted is not None:
                self._last_highlighted.set_plain()
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
