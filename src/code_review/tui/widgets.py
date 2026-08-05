"""The Pipeline box: one line per registry step, live status icon, elapsed/final duration.

The Findings box (issue #42, widened for #61 and #87): the most recently completed step's
`ReviewOutput`, `TestSufficiencyOutput`, or bare `list[Finding]`, one line per finding plus
a severity-count summary -- and, while a step is parked (issue #87), a live inline
approve/skip/abort/chat decision selector replacing the old `ApprovalPromptScreen` modal.

Every widget here takes the data it displays as plain data (`StepRow`s for `PipelineBox`,
a `ReviewOutput`/`TestSufficiencyOutput`/`list[Finding]` for `FindingsBox`, see `state.py`)
-- neither widget ever reads a `StepEvent` stream or a registry/agent output itself. That
split keeps row/finding rendering unit-testable via `render_rows`/the finding-rendering
helpers in isolation, and widget mounting/refresh/interaction testable via Textual's
`Pilot` (`tests/tui/test_widgets.py`), without needing a live event stream either way.
"""

from __future__ import annotations

import asyncio
import colorsys
import time
from collections.abc import Sequence
from typing import cast

from rich.console import Group
from rich.spinner import Spinner
from rich.table import Table
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.css.query import NoMatches
from textual.widgets import Input, OptionList, Static
from textual.widgets.option_list import Option

from code_review.pipeline.findings import Finding
from code_review.pipeline.step import ApprovalDecision, ApprovalResponse
from code_review.steps.review import ReviewOutput
from code_review.steps.test_sufficiency import TestSufficiencyOutput
from code_review.tui.state import ActivityRow, Status, StepRow

# `FindingsBox`'s per-finding decision cycle (issue #87), appended after that finding's own
# `suggestions` -- shared by every finding row, since the decision itself is step-scoped,
# not per-finding (see `FindingsBox.await_decision`'s docstring). Plain strings, not
# `ApprovalDecision` values, because "Type something." is not itself a decision -- it opens
# the inline chat widget rather than resolving anything. Rendered as the numbered list's
# trailing free-text option (see `_render_finding_row`), after every suggestion.
_CUSTOM_ENTRY = "Type something."
_DECISION_ENTRIES = ("approve", "skip", "abort")

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


def _findings_of(output: ReviewOutput | TestSufficiencyOutput | list[Finding]) -> list[Finding]:
    """Extract the plain `list[Finding]` from whichever of `ReviewOutput`/
    `TestSufficiencyOutput`/bare `list[Finding]` `state.py`'s `latest_findings` picked
    (issue #87 widened it to accept `steps/rebase.py`'s bare-list shape too, see that
    module's docstring for why) -- the one place `FindingsBox`'s helpers need to branch on
    shape, so nothing downstream does."""

    return output if isinstance(output, list) else output.findings


def _decision_entries(finding: Finding) -> list[str]:
    """The full per-finding decision cycle a parked `FindingsBox` cycles through (issue
    #87): that finding's own `suggestions`, then `_CUSTOM_ENTRY`, then the step-scoped
    `_DECISION_ENTRIES` -- one unified list, not two separate concerns, per the design
    call that landed #87 (confirming a suggestion or `_CUSTOM_ENTRY` is discussion-only
    and opens the inline chat widget; confirming a `_DECISION_ENTRIES` value resolves the
    whole step's park immediately, regardless of which finding's row it was confirmed
    from -- see `FindingsBox.await_decision`). Rendered as a 1-based numbered list by
    `_render_finding_row`, so a digit key (`_FindingOptionList`'s `"1"`.."9"` bindings) can
    jump `_decision_cursor` straight to any entry here by that same 1-based index."""

    return [*finding.suggestions, _CUSTOM_ENTRY, *_DECISION_ENTRIES]


def _render_finding_row(
    finding: Finding, *, show_suggestions: bool = True, decision_cursor: int | None = None
) -> tuple[Text, Text]:
    """Render one `Finding` as a `(left, right)` cell pair for `FindingsBox`'s grid.

    Left: a colored `_DOT_ICON` (`_SEVERITY_DOT_STYLES`, keyed by `finding.severity`) --
    the per-finding risk indicator issue #77 asks for, reusing `severity` rather than a
    new field -- followed by `format_finding`'s existing severity/description/location
    text.

    Right, when `decision_cursor` is `None`: `finding.suggestions`, one per line, or an
    empty `Text` when there are none -- never a placeholder string like `"None"` (a
    `no-op`/`auto-fix` finding has nothing for a human to choose between); forced empty
    when `show_suggestions=False` (issue #88's per-option rendering, unchanged).

    Right, when `decision_cursor` is not `None` (issue #87: this finding's row is under
    the cursor of a currently-parked `FindingsBox`): every entry of `_decision_entries`,
    one per line, numbered from 1 (matching the digit-key shortcuts on
    `_FindingOptionList`) with a leading `"> "` marking whichever index `decision_cursor`
    names instead of a plain two-space indent -- replaces the plain suggestions dump
    entirely, since a parked box's right column is now something to act on, not just
    read."""

    left = Text(_DOT_ICON, style=_SEVERITY_DOT_STYLES[finding.severity])
    left.append(f" {format_finding(finding)}")
    if decision_cursor is not None:
        right = Text()
        for index, entry in enumerate(_decision_entries(finding)):
            marker = "> " if index == decision_cursor else "  "
            right.append(f"{marker}{index + 1}. {entry}\n")
    else:
        right = Text("\n".join(finding.suggestions) if show_suggestions else "")
    return left, right


def _findings_summary(output: ReviewOutput | TestSufficiencyOutput | list[Finding]) -> str:
    """Render `output`'s severity-count summary, e.g. `1 error, 2 warning, 0 info` --
    `FindingsBox`'s own summary line."""

    counts = {severity: 0 for severity in ("error", "warning", "info")}
    for finding in _findings_of(output):
        counts[finding.severity] += 1
    return f"{counts['error']} error, {counts['warning']} warning, {counts['info']} info"


def _finding_option_prompt(
    finding: Finding, *, show_suggestions: bool, decision_cursor: int | None = None
) -> Table:
    """Render one `Finding` as an `OptionList.Option`'s `prompt` -- the same two-column
    `(left, right)` shape as `FindingsBox`'s grid, in its own small grid rather than a
    shared one (see `render_rows_live`'s docstring for why a per-row grid, not one grid
    spanning every option)."""

    row = Table.grid(padding=(0, 1), pad_edge=False, expand=False)
    # `no_wrap=True` on the left (description) column: without it, when the row overflows
    # the available width, Rich shrinks whichever column it finds easiest to shrink -- in
    # practice the left column, since a description is usually the longer of the two --
    # wrapping a finding's description across lines mid-word. `no_wrap` refuses to let the
    # description give up width, so overflow instead falls on the right column (a
    # suggestion or, while parked, the numbered decision list, including its
    # `_CUSTOM_ENTRY`/digit prefixes) which already renders one entry per line and reads
    # fine wrapped.
    row.add_column(no_wrap=True)
    row.add_column()
    row.add_row(
        *_render_finding_row(
            finding, show_suggestions=show_suggestions, decision_cursor=decision_cursor
        )
    )
    return row


def _finding_options(
    findings: Sequence[Finding], highlighted: int | None, *, decision_cursor: int | None = None
) -> list[Option]:
    """Build one `OptionList.Option` per finding (issue #88) -- only the option at index
    `highlighted` shows anything in the right column, every other index's right column is
    empty. `highlighted=None` (no findings, or before Textual has highlighted anything)
    shows nothing anywhere. `decision_cursor`, passed through to the highlighted row only
    (issue #87), turns that row's right column from a plain suggestions dump into the
    live decision cycle -- see `_render_finding_row`."""

    return [
        Option(
            _finding_option_prompt(
                finding,
                show_suggestions=index == highlighted,
                decision_cursor=decision_cursor if index == highlighted else None,
            )
        )
        for index, finding in enumerate(findings)
    ]


class _FindingOptionList(OptionList):
    """`OptionList` subclass adding issue #87's parked-mode key bindings on top of
    #88's plain up/down/enter navigation: left/right cycle the highlighted finding's
    `_decision_entries`, single-key "a"/"s"/"x"/"f" shortcuts jump straight to
    Approve/Skip/Abort/free-text regardless of cursor position, and digit keys "1".."9"
    jump straight to that 1-based entry in `_decision_entries` (matching the numbered
    rendering `_render_finding_row` gives that list) -- letter shortcuts mirror
    `ApprovalPromptScreen`'s old bindings (the issue's own sketch explicitly asks for
    "cyclable via arrow keys or their letter keybindings"), so existing muscle memory
    (and most of the existing approval-flow tests) keep working unchanged. All of these
    delegate to the owning `FindingsBox`, which no-ops them while not parked -- this
    class holds no decision state of its own."""

    BINDINGS = [
        Binding("left", "cycle_prev", "Previous suggestion", show=False),
        Binding("right", "cycle_next", "Next suggestion", show=False),
        Binding("a", "quick_approve", "Approve", show=False),
        Binding("s", "quick_skip", "Skip", show=False),
        Binding("x", "quick_abort", "Abort", show=False),
        Binding("f", "open_chat", "Type something", show=False),
        *(Binding(str(digit), f"jump_decision({digit})", f"Option {digit}", show=False) for digit in range(1, 10)),
    ]

    def __init__(self, *options: Option, owner: FindingsBox) -> None:
        super().__init__(*options)
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
    """A small inline free-text widget (issue #87), mounted into a parked `FindingsBox`
    on demand -- when a human confirms a suggestion (seeded with that suggestion's own
    text) or `_CUSTOM_ENTRY` (seeded empty). Replaces `ApprovalPromptScreen`'s "fix" path
    pushing `InputPromptScreen`: there is no modal host for an `Input` in this design
    (issue #87 removes the modal entirely), so this widget plays that role directly,
    mounted alongside the `OptionList` rather than on a separate screen."""

    DEFAULT_CSS = """
    _InlineApprovalChat {
        height: auto;
        margin-top: 1;
    }
    """

    def __init__(self, prefill: str, *, owner: FindingsBox) -> None:
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


class FindingsBox(Vertical):
    """A bordered box showing the most recently completed step's findings (see
    `state.py`'s `latest_findings`, which recognizes a `ReviewOutput` (`ReviewStep`), a
    `TestSufficiencyOutput` (`TestSufficiencyStep`), or a bare `list[Finding]`
    (`RebaseStep`, issue #87's widening) equally): each finding's severity, description,
    location when it has one, and risk indicator in a left column, that finding's
    suggestions in a right column -- but only for the finding currently under the cursor
    (issue #88); arrow keys move the cursor between findings via a child `OptionList`. A
    trailing severity-count summary line sits below the list, unchanged in wording from
    #77. `border_title` names the owning step (issue #74, e.g. `"Findings -- ReviewStep"`)
    so it's clear which step's output is on display.

    No longer display-only (issue #87, superseding #42/#61's "no key or action here lets
    a user approve, fix, skip, or abort a finding" -- see docs/GLOSSARY.md's "Action"):
    while a step is actually parked, `await_decision` turns the right column into a live
    decision selector -- see that method's docstring, and `_FindingOptionList`/
    `_InlineApprovalChat` above -- replacing `ApprovalPromptScreen`'s modal entirely.
    Outside a park (the ordinary "most recently completed step's findings" display), the
    box behaves exactly as #88 already shipped: read-only, only the highlighted finding's
    suggestions shown, no key here does anything.

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
        output: ReviewOutput | TestSufficiencyOutput | list[Finding],
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
        self._decision_cursor = 0
        self._pending: asyncio.Future[ApprovalResponse] | None = None

    def compose(self) -> ComposeResult:
        yield _FindingOptionList(
            *_finding_options(_findings_of(self._output), highlighted=0), owner=self
        )
        yield Static(_findings_summary(self._output))

    def update_findings(
        self, output: ReviewOutput | TestSufficiencyOutput | list[Finding], step_name: str
    ) -> None:
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

        Since that same periodic timer calls this on every tick regardless of whether
        `output` actually changed, the rebuilt `OptionList` preserves whatever index is
        already `highlighted` rather than resetting to 0 -- otherwise a human arrowing
        down to browse a later finding's suggestions would see them snap back to finding
        0 on the very next tick, before they had a chance to read them.
        """

        self._output = output
        self.border_title = f"Findings -- {step_name}"
        try:
            option_list = self.query_one(OptionList)
            summary = self.query_one(Static)
        except NoMatches:
            return
        highlighted = option_list.highlighted if option_list.highlighted is not None else 0
        option_list.clear_options()
        option_list.add_options(_finding_options(_findings_of(output), highlighted=highlighted))
        # `clear_options()` drops the option list's own highlighted-index cursor, so it must
        # be restored explicitly -- passing `highlighted` into `_finding_options` above only
        # controls which row's prompt renders its suggestions, not Textual's own cursor.
        if _findings_of(output):
            option_list.highlighted = min(highlighted, len(_findings_of(output)) - 1)
        summary.update(_findings_summary(output))

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        """Rebuild every option's prompt so only `event.option_index`'s row shows
        anything -- see the module docstring's rationale for a full rebuild over diffing
        old-vs-new highlighted index. Resets `_decision_cursor` back to 0 (issue #87) --
        moving to a different finding always starts its own decision cycle fresh, rather
        than carrying over whatever index the previous row happened to be on."""

        self._decision_cursor = 0
        self._rebuild_options(event.option_list, highlighted=event.option_index)

    def on_option_list_option_selected(self, _event: OptionList.OptionSelected) -> None:
        """Confirm whatever `_decision_cursor` currently points at for the highlighted
        finding (issue #87) -- a no-op outside a park, matching every other interactive
        binding here."""

        if not self._parked:
            return
        finding = self._highlighted_finding()
        if finding is None:
            return
        entry = _decision_entries(finding)[self._decision_cursor]
        if entry in _DECISION_ENTRIES:
            self._quick_decision(cast(ApprovalDecision, entry))
        elif entry == _CUSTOM_ENTRY:
            self._open_chat("")
        else:
            self._open_chat(entry)

    def _rebuild_options(self, option_list: OptionList, *, highlighted: int) -> None:
        for index, finding in enumerate(_findings_of(self._output)):
            option_list.replace_option_prompt_at_index(
                index,
                _finding_option_prompt(
                    finding,
                    show_suggestions=index == highlighted,
                    decision_cursor=(
                        self._decision_cursor if self._parked and index == highlighted else None
                    ),
                ),
            )

    def _highlighted_finding(self) -> Finding | None:
        option_list = self.query_one(OptionList)
        findings = _findings_of(self._output)
        if option_list.highlighted is None or not findings:
            return None
        return findings[option_list.highlighted]

    def _cycle_decision(self, delta: int) -> None:
        if not self._parked:
            return
        finding = self._highlighted_finding()
        if finding is None:
            return
        entries = _decision_entries(finding)
        self._decision_cursor = (self._decision_cursor + delta) % len(entries)
        option_list = self.query_one(OptionList)
        self._rebuild_options(option_list, highlighted=option_list.highlighted or 0)

    def _jump_decision(self, index: int) -> None:
        """Jump `_decision_cursor` straight to `index` (0-based) -- the digit-key
        counterpart to `_cycle_decision`'s relative left/right step. A no-op, like every
        other decision binding, outside a park or when `index` is past the highlighted
        finding's own `_decision_entries` (a finding with fewer suggestions than another
        has a shorter list, so not every digit is valid for every row)."""

        if not self._parked:
            return
        finding = self._highlighted_finding()
        if finding is None or not 0 <= index < len(_decision_entries(finding)):
            return
        self._decision_cursor = index
        option_list = self.query_one(OptionList)
        self._rebuild_options(option_list, highlighted=option_list.highlighted or 0)

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

    async def await_decision(self) -> ApprovalResponse:
        """Turn this box's right column into a live decision selector until a human
        confirms Approve/Skip/Abort (directly, or via the inline chat widget resolving
        with `decision="fix"`) -- the inline replacement for
        `ReviewApp._relay_approval`'s old `push_screen_wait(ApprovalPromptScreen(...))`
        (issue #87).

        The decision is step-scoped, not per-finding (an explicit design call, since a
        park is a single step-level event): confirming Approve/Skip/Abort from *any*
        finding's row resolves the one pending future here, regardless of which row the
        cursor was on -- `_decision_cursor`/left-right cycling is purely a per-row
        browsing aid, not separate state per finding. Only one `await_decision` call can
        be pending at a time in practice (`ReviewApp._relay_approval` awaits one park at
        a time), so a single `self._pending` future, not a queue, is enough.

        Resets `_decision_cursor` to 0 and re-renders before waiting, so a park always
        starts each finding's cycle fresh, then restores `self._parked = False` in a
        `finally` -- once resolved, the box reverts to #88's plain read-only display for
        whatever `update_findings` call comes next, as if it had never been parked.

        Also focuses the `OptionList` -- unlike the old `ApprovalPromptScreen`, becoming
        the active screen on `push_screen` and so implicitly receiving every keypress,
        this box is just one mounted widget among others; without an explicit `.focus()`
        here, a real run's "a"/"s"/"x"/"f" keypresses would land on whatever (if anything)
        last had focus instead of `_FindingOptionList`'s bindings, and nothing would
        happen at all.
        """

        self._parked = True
        self._decision_cursor = 0
        option_list = self.query_one(OptionList)
        self._rebuild_options(option_list, highlighted=option_list.highlighted or 0)
        option_list.focus()
        self._pending = asyncio.get_running_loop().create_future()
        try:
            return await self._pending
        finally:
            self._parked = False
            self._pending = None


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
