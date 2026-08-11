"""The right column of one `Finding` row: suggestions, or a live decision cycle.

- Tri-state (hidden/plain/decision) -- only the highlighted row ever shows anything;
  every other row's `FindingsSuggestion` stays cleared.
- Mode switching is `Finding`'s job, not this widget's own -- it only knows how to render
  each of the three states, not when to be in one.
- In decision mode, the trailing "Chat about it" entry can be replaced in place by a live
  `Input`, seeded with whatever text a human is confirming.
- Standalone module: no dependency on any other widget in this package.
"""

from __future__ import annotations

from pathlib import Path

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Input, Static

from code_review.pipeline.findings import Finding as FindingData

# `FindingsList`'s per-finding decision cycle, appended after that finding's own
# `suggestions` -- a plain string, not an `ApprovalDecision`, since "Chat about it" opens
# the inline chat rather than recording anything on its own; confirming it (or a
# suggestion) always records "fix" for whichever row it was confirmed on.
_CUSTOM_ENTRY = "Chat about it"


def _decision_entries(finding: FindingData) -> list[str]:
    """The full per-finding decision cycle a parked row cycles through.

    - That finding's own `suggestions`, then a single trailing `_CUSTOM_ENTRY`.
    - Every entry is discussion-only: confirming any of them always records "fix" for
      whichever row it was confirmed on, seeded with that entry's own text.
    - Rendered as a 1-based numbered list by `render_decision_cycle`, so a digit key can
      jump a row's cursor straight to any entry here by that same 1-based index.
    """

    return [*finding.suggestions, _CUSTOM_ENTRY]


def render_suggestions_plain(finding: FindingData) -> Text:
    """`FindingsSuggestion`'s content outside a decision cycle: `finding.suggestions`, one
    per line, or an empty `Text` when there are none."""

    return Text("\n".join(finding.suggestions))


def _render_decision_entry(
    index: int, entry: str, decision_cursor: int, *, has_own_suggestions: bool
) -> Text:
    """One line of a decision cycle, with no trailing newline of its own.

    - Shared by `render_decision_cycle`/`render_decision_cycle_head`/
      `render_custom_entry_line`, so the numbering/marker rules stay defined in exactly
      one place.
    - Deliberately returns a bare, newline-free line -- callers join multiple entries with
      `"\\n"` as a *separator* rather than a trailing terminator, so the last entry in a
      multi-entry render never carries a dangling trailing blank line (see
      `render_decision_cycle_head`'s own docstring for why that distinction matters to
      `FindingsSuggestion`).
    """

    marker = "> " if index == decision_cursor else "  "
    recommended = " (Recommended)" if index == 0 and has_own_suggestions else ""
    return Text(f"{marker}{index + 1}. {entry}{recommended}")


def render_decision_cycle(finding: FindingData, decision_cursor: int) -> Text:
    """The full decision cycle: every `_decision_entries` entry, numbered from 1, one per
    line with no trailing blank line after the last one.

    - A leading `"> "` marks whichever index `decision_cursor` names.
    - Entry 0 is labeled `" (Recommended)"` when it came from `finding.suggestions` itself.
    - The simplest pure surface to unit-test the shared per-entry rules against; the live
      `FindingsSuggestion` widget instead uses `render_decision_cycle_head`/
      `render_custom_entry_line` so the trailing entry can be replaced by a live `Input`.
    """

    entries = _decision_entries(finding)
    text = Text()
    for index, entry in enumerate(entries):
        if index:
            text.append("\n")
        text.append(
            _render_decision_entry(
                index, entry, decision_cursor, has_own_suggestions=bool(finding.suggestions)
            )
        )
    return text


def render_decision_cycle_head(finding: FindingData, decision_cursor: int) -> Text:
    """Every `_decision_entries` entry except the trailing `_CUSTOM_ENTRY`, rendered exactly
    as `render_decision_cycle` would -- the trailing entry is drawn separately, by
    `render_custom_entry_line`/a live `Input`.

    No trailing newline after the last entry: `FindingsSuggestion.show_decision` renders
    this straight into `self._entries`, and a Rich `Text` ending in `"\\n"` renders an
    extra, invisible empty line beneath it. A real divider (`self._custom`'s own
    `border-top`) marks the boundary instead.
    """

    entries = _decision_entries(finding)
    text = Text()
    for index, entry in enumerate(entries[:-1]):
        if index:
            text.append("\n")
        text.append(
            _render_decision_entry(
                index, entry, decision_cursor, has_own_suggestions=bool(finding.suggestions)
            )
        )
    return text


def render_custom_entry_line(finding: FindingData, decision_cursor: int) -> Text:
    """The trailing `_CUSTOM_ENTRY`'s own line, rendered exactly as `render_decision_cycle`
    would -- `FindingsSuggestion` shows this instead of a live `Input` whenever that
    `Input` isn't (yet) mounted."""

    entries = _decision_entries(finding)
    index = len(entries) - 1
    return _render_decision_entry(
        index, entries[index], decision_cursor, has_own_suggestions=bool(finding.suggestions)
    )


class FindingsSuggestion(Vertical):
    """The right column of one `Finding` row -- suggestions, or a live decision cycle.

    - A `Vertical` composing two `Static`s (`self._entries` for every entry before
      `_CUSTOM_ENTRY`, `self._custom` for that entry's own line), since decision mode needs
      to replace the trailing line with a live `Input` in place.
    - `display: none` while hidden, so `FindingsDescription` takes the whole row instead of
      an always-reserved, unused half. The `-visible` class restores `display: block` and
      draws a full border, so this column only reads as its own widget once it has content.
    - `self._custom` always carries `-custom-entry` (styled a muted gray) so "Chat about
      it" reads as "type your own", not another agent-generated suggestion. `-decision` is
      toggled alongside it (added in `show_decision`, removed in `show_plain`/`clear`) to
      draw a `border-top` divider above it, only while a "Chat about it" entry is showing.
    """

    DEFAULT_CSS = Path(__file__).with_suffix(".tcss").read_text()

    def __init__(
        self,
        *,
        id: str | None = None,  # noqa: A002 -- matches Textual's own Widget.__init__ shape
        classes: str | None = None,
    ) -> None:
        super().__init__(id=id, classes=classes)
        self.border_title = "Suggestion"
        self._entries = Static("")
        self._custom = Static("", classes="-custom-entry")
        # Set only once a human deliberately opens the chat (`ensure_input`) -- `None`
        # covers both "not parked"/"plain mode" and "cursor on `_CUSTOM_ENTRY` but not
        # opened yet", both of which render `self._custom`'s plain text instead.
        self._input: Input | None = None
        # Mounted/removed in lockstep with `self._input` -- a one-line reminder of the
        # Escape binding, shown only while the chat is actually open.
        self._hint: Static | None = None

    def compose(self) -> ComposeResult:
        yield self._entries
        yield self._custom

    def clear(self) -> None:
        self.remove_class("-visible")
        self._custom.remove_class("-decision")
        self._entries.update("")
        self._custom.update("")
        self._remove_input()

    def show_plain(self, finding: FindingData) -> None:
        self.add_class("-visible")
        self._custom.remove_class("-decision")
        self._entries.update(render_suggestions_plain(finding))
        self._custom.update("")
        self._remove_input()

    def show_decision(self, finding: FindingData, decision_cursor: int) -> None:
        """Render decision mode: `self._entries` gets every entry before `_CUSTOM_ENTRY`;
        the trailing slot shows `self._custom`'s plain text, unless a chat is already open
        on this row (`self._input is not None`), in which case that `Input` is left
        untouched. Never opens the chat itself, even when `decision_cursor` points at
        `_CUSTOM_ENTRY` -- only a deliberate confirm/cycle/jump does that, via
        `ensure_input`. Safe to call repeatedly from a periodic re-render: with no `Input`
        open it just re-renders the plain text; with one open, it leaves it -- and whatever
        a human has typed -- completely alone.
        """

        self.add_class("-visible")
        self._custom.add_class("-decision")
        self._entries.update(render_decision_cycle_head(finding, decision_cursor))
        entries = _decision_entries(finding)
        on_custom_entry = decision_cursor == len(entries) - 1
        if self._input is not None:
            if on_custom_entry:
                self._custom.update("")
                return
            # The cursor moved off `_CUSTOM_ENTRY` while its `Input` was still open --
            # defensive cleanup so a stale `Input` never lingers pointed at the wrong entry.
            self._remove_input()
        self._custom.update(render_custom_entry_line(finding, decision_cursor))

    def ensure_input(self, prefill: str) -> Input:
        """Mount (if not already mounted) this row's live `Input` for `_CUSTOM_ENTRY`,
        seeded with `prefill`, and return it. Called only when a human deliberately opens
        the chat, never by `show_decision`'s own redundant re-renders. Idempotent: if one
        is already mounted, it's returned untouched and `prefill` is ignored, so an
        already-open chat's typed value survives a redundant re-render."""

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
        """Cancel this row's live chat without resolving anything -- tears the `Input`
        down and re-renders `_CUSTOM_ENTRY`'s plain text in its place. Whatever had been
        typed is discarded, matching Escape's "cancel", not "save draft"."""

        self._remove_input()
        self.show_decision(finding, decision_cursor)
