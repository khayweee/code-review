"""One row per finding inside `_FindingsListView`.

Composes `FindingsDescription`/`FindingsSuggestion` in a horizontal split; owns this
row's display mode (`hidden`/`plain`/`decision`), browsing cursor, and recorded park
decision. Carries no key bindings of its own -- all parked-mode bindings live on
`_FindingsListView`. Named `Finding`, shadowing `pipeline.findings.Finding` (imported
here as `FindingData`) deliberately, since this widget's identity is "one finding,
rendered".
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from textual.app import ComposeResult
from textual.css.query import NoMatches
from textual.widgets import Input, ListItem

from code_review.pipeline.findings import Finding as FindingData
from code_review.pipeline.schemas import ApprovalResponse
from code_review.tui.widgets.findings_description import FindingsDescription
from code_review.tui.widgets.findings_suggestion import FindingsSuggestion, _decision_entries


class Finding(ListItem):
    """One row per finding inside `_FindingsListView`.

    Confirming this row's chat, or pressing "s" while highlighted, records this row's own
    decision only (see `FindingsList.await_decision` for the per-row-then-aggregate
    model). Abort ("x") is the exception: it resolves the whole park directly with no
    per-row recording.
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
        super().__init__(id=id, classes=classes)
        self.finding = finding
        self._decision_cursor = 0
        self._mode: Literal["hidden", "plain", "decision"] = "hidden"
        # None until confirmed ("fix") or skipped ("s"); reset at the start of every park.
        self._row_decision: ApprovalResponse | None = None

    def compose(self) -> ComposeResult:
        yield FindingsDescription(self.finding)
        suggestion = FindingsSuggestion()
        yield suggestion
        # A set_hidden/set_plain/set_decision/update_finding call may have landed before
        # compose() ran, updating state with no FindingsSuggestion yet to apply it to.
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
        """Apply the current `_mode` to this row's `FindingsSuggestion`; no-ops if
        `compose()` hasn't run yet (it applies `_mode` itself once it does)."""

        try:
            suggestion = self.query_one(FindingsSuggestion)
        except NoMatches:
            return
        self._apply_mode(suggestion)

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
