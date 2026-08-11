"""One row per finding inside `_FindingsListView`.

- Composes `FindingsDescription`/`FindingsSuggestion` in a horizontal split.
- Owns this row's own display mode (`hidden`/`plain`/`decision`), its own per-row browsing
  cursor within this finding's own suggestion list, and its own recorded park decision.
- `ListItem.can_focus=False`: this class carries no key bindings of its own -- all
  parked-mode bindings live on `_FindingsListView`, the only focusable node in this subtree.
- Named `Finding`, shadowing `pipeline.findings.Finding` (imported here as `FindingData`) --
  deliberate, since this widget's identity *is* "one finding, rendered".
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from textual.app import ComposeResult
from textual.css.query import NoMatches
from textual.widgets import Input, ListItem

from code_review.pipeline.findings import Finding as FindingData
from code_review.pipeline.step import ApprovalResponse
from code_review.tui.widgets.findings_description import FindingsDescription
from code_review.tui.widgets.findings_suggestion import FindingsSuggestion, _decision_entries


class Finding(ListItem):
    """One row per finding inside `_FindingsListView`.

    - Confirming this row's chat or pressing "s" while it's highlighted records *this
      row's own* decision, not the whole park's -- see `FindingsList.await_decision`'s
      docstring for the per-row-then-aggregate model.
    - Abort ("x") is the one exception: it stays `_FindingsListView`'s own separate global
      binding, resolving the whole park directly with no per-row recording step.
    """

    DEFAULT_CSS = Path(__file__).with_suffix(".tcss").read_text()

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
        # This row's own recorded park decision -- `None` until a human confirms this
        # row's chat ("fix") or presses "s" while it's highlighted ("skip"); reset back to
        # `None` at the start of every `FindingsList.await_decision()` park, so a
        # fix-round's re-park never carries over the previous round's decision. Distinct
        # from `_decision_cursor` above, which is purely a per-row browsing position.
        self._row_decision: ApprovalResponse | None = None

    def compose(self) -> ComposeResult:
        yield FindingsDescription(self.finding)
        suggestion = FindingsSuggestion()
        yield suggestion
        # Prime it from whatever `_mode`/`_decision_cursor` already are -- a `set_hidden`/
        # `set_plain`/`set_decision`/`update_finding` call can land on this row before
        # `compose()` has run, in which case those calls updated state but could not reach
        # a `FindingsSuggestion` that didn't exist yet.
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
        own `compose()` hasn't run yet -- not lossy, since `compose()`'s own `_apply_mode`
        call picks up `_mode` once it does run."""

        try:
            suggestion = self.query_one(FindingsSuggestion)
        except NoMatches:
            return
        self._apply_mode(suggestion)

    def reset_decision(self) -> None:
        """Reset the browsing cursor to 0 -- called whenever this row becomes highlighted,
        so each finding's decision cycle starts fresh. Distinct from `clear_decision`,
        which resets the recorded decision, not the cursor."""

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
        """Reset this row back to undecided -- called for every row at the start of each
        park, so a fix-round's re-park never carries over the previous round's decision."""

        self._row_decision = None
        self._render_description()

    def _render_description(self) -> None:
        """Apply this row's decision marker to `FindingsDescription`, guarded like
        `_render_suggestion` for a row that hasn't composed yet."""

        try:
            description = self.query_one(FindingsDescription)
        except NoMatches:
            return
        marker = None if self._row_decision is None else self._row_decision.decision
        description.update_finding(self.finding, marker)

    def update_finding(self, finding: FindingData) -> None:
        """Data changed in place, same list position -- refresh every child, preserving
        this row's display mode and decision marker. `self.finding` is updated regardless
        of whether this row has composed yet."""

        self.finding = finding
        self._render_description()
        self._render_suggestion()

    def cycle_decision(self, delta: int) -> None:
        entries = _decision_entries(self.finding)
        self._decision_cursor = (self._decision_cursor + delta) % len(entries)
        self.set_decision()

    def jump_decision(self, index: int) -> None:
        """Jump the cursor straight to `index` (0-based) -- the digit-key counterpart to
        `cycle_decision`'s relative left/right step. No-ops past this finding's entry
        count."""

        entries = _decision_entries(self.finding)
        if not 0 <= index < len(entries):
            return
        self._decision_cursor = index
        self.set_decision()

    def confirmed_entry(self) -> str:
        return _decision_entries(self.finding)[self._decision_cursor]

    def open_chat(self, prefill: str) -> Input | None:
        """A human deliberately opened the chat on this row -- via Enter/"f", or a
        cycle/jump landing the cursor on `_CUSTOM_ENTRY`. Moves the cursor to the trailing
        entry regardless of where it was, so a confirmed suggestion's text has somewhere
        live to render. Returns the `Input` once this row has composed, `None` otherwise.
        """

        entries = _decision_entries(self.finding)
        self._decision_cursor = len(entries) - 1
        self.set_decision()
        try:
            suggestion = self.query_one(FindingsSuggestion)
        except NoMatches:
            return None
        return suggestion.ensure_input(prefill)

    def close_chat(self) -> None:
        """Cancel a chat open on this row -- the Escape counterpart to `open_chat`. Leaves
        `_decision_cursor` exactly where it was and resolves nothing; a no-op if this row
        hasn't composed yet or has no live `Input` open."""

        try:
            suggestion = self.query_one(FindingsSuggestion)
        except NoMatches:
            return
        suggestion.cancel_input(self.finding, self._decision_cursor)
