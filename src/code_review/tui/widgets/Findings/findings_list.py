"""The list of finding rows: one `Finding` per finding, hosted by a `_FindingsListView`.

Together these two classes constitute "the list of findings" inside a `FindingBox`.
`Finding` is one row -- a `ListItem` composing `FindingsDescription`/`FindingsSuggestion`
in a horizontal split, owning this row's display mode (`hidden`/`plain`/`decision`),
browsing cursor, and recorded park decision. Its mode transitions
(`set_hidden`/`set_plain`/`set_decision`) drive BOTH columns' highlight-only content --
`FindingsSuggestion`'s suggestion/decision-cycle text and `FindingsDescription`'s own
`FindingExpandedDescription` child -- from the same call sites, not two independently
tracked focus mechanisms. It carries no key bindings of its own -- all parked-mode
bindings live on `_FindingsListView`, the focusable `ListView` that hosts one `Finding`
per finding (`can_focus_children=False`; `Finding` rows are never individually focused)
and delegates every parked-mode binding beyond up/down/enter to the owning `FindingBox`,
which no-ops them while not parked. `Finding` shadows `pipeline.findings.Finding`
(imported here as `FindingData`) deliberately, since this widget's identity is "one
finding, rendered".
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Literal

from textual.app import ComposeResult
from textual.binding import Binding
from textual.css.query import NoMatches
from textual.widgets import Input, ListItem, ListView

from code_review.pipeline.findings import Finding as FindingData
from code_review.pipeline.schemas import ApprovalResponse
from code_review.tui.widgets.Findings.findings_description import FindingsDescription
from code_review.tui.widgets.Findings.findings_suggestion import (
    FindingsSuggestion,
    _decision_entries,
)

if TYPE_CHECKING:
    from code_review.tui.widgets.Findings.finding import FindingBox


class Finding(ListItem):
    """One row per finding inside `_FindingsListView`.

    Confirming this row's chat, or pressing "s" while highlighted, records this row's own
    decision only (see `FindingBox.await_decision` for the per-row-then-aggregate
    model). Abort ("x") is the exception: it resolves the whole park directly with no
    per-row recording.
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
        self.finding = finding
        self._decision_cursor = 0
        self._mode: Literal["hidden", "plain", "decision"] = "hidden"
        # None until confirmed ("fix") or skipped ("s"); reset at the start of every park.
        self._row_decision: ApprovalResponse | None = None

    def compose(self) -> ComposeResult:
        description = FindingsDescription(self.finding)
        yield description
        # A set_hidden/set_plain/set_decision/update_finding call may have landed before
        # compose() ran, updating state with no FindingsDescription yet to apply it to --
        # same reasoning as the suggestion._apply_mode() call below, applied directly on
        # the pre-yield reference rather than via a query.
        description.set_expanded(self._mode != "hidden")
        suggestion = FindingsSuggestion()
        yield suggestion
        self._apply_mode(suggestion)

    def set_hidden(self) -> None:
        self._mode = "hidden"
        self._render_suggestion()
        self._render_description_detail()

    def set_plain(self) -> None:
        self._mode = "plain"
        self._render_suggestion()
        self._render_description_detail()

    def set_decision(self) -> None:
        self._mode = "decision"
        self._render_suggestion()
        self._render_description_detail()

    def _apply_mode(self, suggestion: FindingsSuggestion) -> None:
        if self._mode == "hidden":
            suggestion.clear()
        elif self._mode == "plain":
            suggestion.show_plain(self.finding)
        else:
            suggestion.show_decision(self.finding, self._decision_cursor)

    def _render_suggestion(self) -> None:
        """Apply the current `_mode` to this row's `FindingsSuggestion`; no-ops if
        `compose()` hasn't run yet (it applies `_mode` itself once it does)."""

        try:
            suggestion = self.query_one(FindingsSuggestion)
        except NoMatches:
            return
        self._apply_mode(suggestion)

    def _render_description_detail(self) -> None:
        """Show/hide this row's `FindingExpandedDescription` in lockstep with `_mode` --
        visible whenever this row is highlighted (`plain`/`decision`), hidden while
        `hidden`. Reuses the exact same mode-transition call sites `_render_suggestion`
        already uses, rather than tracking highlight state a second time; no-ops if
        `compose()` hasn't run yet (`compose()` applies the initial expanded state itself,
        directly on the pre-yield `FindingsDescription` reference)."""

        try:
            description = self.query_one(FindingsDescription)
        except NoMatches:
            return
        description.set_expanded(self._mode != "hidden")

    def reset_decision(self) -> None:
        """Reset the browsing cursor to 0, unless this row has a recorded "fix" decision
        -- then the cursor stays where it was confirmed, so revisiting the row shows what
        was actually chosen instead of jumping back to entry 0."""

        if self._row_decision is None or self._row_decision.decision != "fix":
            self._decision_cursor = 0

    def is_decided(self) -> bool:
        """True once this row has a recorded decision since the last `clear_decision`."""

        return self._row_decision is not None

    @property
    def row_decision(self) -> ApprovalResponse | None:
        """This row's own recorded decision, or `None` while undecided."""

        return self._row_decision

    def record_decision(self, response: ApprovalResponse) -> None:
        """Record `response` as this row's decision, overwriting any previous one, and
        re-render `FindingsDescription`'s decided marker immediately."""

        self._row_decision = response
        self._render_description()

    def clear_decision(self) -> None:
        """Reset this row back to undecided."""

        self._row_decision = None
        self._render_description()

    def _render_description(self) -> None:
        """Apply this row's decision marker to `FindingsDescription`; no-ops if this row
        hasn't composed yet."""

        try:
            description = self.query_one(FindingsDescription)
        except NoMatches:
            return
        marker = None if self._row_decision is None else self._row_decision.decision
        description.update_finding(self.finding, marker)

    def update_finding(self, finding: FindingData) -> None:
        """Data changed in place, same list position -- refresh every child, preserving
        this row's display mode and decision marker."""

        self.finding = finding
        self._render_description()
        self._render_suggestion()

    def cycle_decision(self, delta: int) -> None:
        entries = _decision_entries(self.finding)
        self._decision_cursor = (self._decision_cursor + delta) % len(entries)
        self.set_decision()

    def jump_decision(self, index: int) -> None:
        """Jump the cursor straight to `index` (0-based). No-ops past this finding's
        entry count."""

        entries = _decision_entries(self.finding)
        if not 0 <= index < len(entries):
            return
        self._decision_cursor = index
        self.set_decision()

    def confirmed_entry(self) -> str:
        return _decision_entries(self.finding)[self._decision_cursor]

    def open_chat(self, prefill: str) -> Input | None:
        """Open the chat on this row, moving the cursor to the trailing entry. Returns
        the `Input` once this row has composed, `None` otherwise."""

        entries = _decision_entries(self.finding)
        self._decision_cursor = len(entries) - 1
        self.set_decision()
        try:
            suggestion = self.query_one(FindingsSuggestion)
        except NoMatches:
            return None
        return suggestion.ensure_input(prefill)

    def close_chat(self) -> None:
        """Cancel a chat open on this row without resolving anything. Leaves
        `_decision_cursor` unchanged; a no-op if there's no live `Input` open."""

        try:
            suggestion = self.query_one(FindingsSuggestion)
        except NoMatches:
            return
        suggestion.cancel_input(self.finding, self._decision_cursor)

    def restore_chat_preview(self) -> None:
        """Re-open this row's chat, pre-filled with its recorded "fix" instructions, if
        the decision cursor is sitting on `_CUSTOM_ENTRY` and this row has such a decision
        recorded -- so revisiting a chat-decided row shows what was typed instead of the
        bare "Chat about it" label.

        Call only from highlight-transition sites, never a redundant re-render, so a
        human's own Escape (`close_chat`) isn't immediately undone on the next tick.
        No-op if the cursor isn't on `_CUSTOM_ENTRY`, there's no recorded "fix" decision,
        or an `Input` is already mounted."""

        entries = _decision_entries(self.finding)
        if self._decision_cursor != len(entries) - 1:
            return
        if self._row_decision is None or self._row_decision.decision != "fix":
            return
        try:
            suggestion = self.query_one(FindingsSuggestion)
        except NoMatches:
            return
        suggestion.ensure_input(self._row_decision.instructions or "")


class _FindingsListView(ListView):
    """Hosts one `Finding` row per finding; owns every parked-mode key binding.

    left/right cycle decision entries; "s" skips the highlighted row; "x" aborts the run
    regardless of cursor position; "f" opens the inline chat; digits "1".."9" jump to
    that 1-based entry.
    """

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

    def __init__(self, *items: Finding, owner: FindingBox) -> None:
        # ListView indexes children unfiltered; a non-Finding child would silently
        # corrupt that indexing rather than raise.
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
        self._owner._open_chat()

    def action_close_chat(self) -> None:
        self._owner._close_chat()

    def action_jump_decision(self, digit: int) -> None:
        self._owner._jump_decision(digit - 1)
