"""The right column of one `Finding` row: suggestions, or a live decision cycle.

Tri-state (hidden/plain/decision); only the highlighted row shows anything, and mode
switching is `Finding`'s job -- this widget only knows how to render each state. In
decision mode, the trailing "Chat about it" entry can be replaced in place by a live
`Input`.
"""

from __future__ import annotations

from pathlib import Path

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Input, Static

from code_review.pipeline.findings import Finding as FindingData

# Trailing entry appended after a finding's own suggestions; opens the inline chat
# rather than recording anything by itself.
_CUSTOM_ENTRY = "Chat about it"


def _decision_entries(finding: FindingData) -> list[str]:
    """The full per-finding decision cycle: `finding.suggestions` then `_CUSTOM_ENTRY`.

    Confirming any entry records "fix", seeded with that entry's text.
    """

    return [*finding.suggestions, _CUSTOM_ENTRY]


def render_suggestions_plain(finding: FindingData) -> Text:
    """`FindingsSuggestion`'s content outside a decision cycle: `finding.suggestions`, one
    per line, or an empty `Text` when there are none."""

    return Text("\n".join(finding.suggestions))


def _render_decision_entry(
    index: int, entry: str, decision_cursor: int, *, has_own_suggestions: bool
) -> Text:
    """One line of a decision cycle, with no trailing newline.

    Shared by `render_decision_cycle`/`render_decision_cycle_head`/
    `render_custom_entry_line`. Callers join entries with `"\\n"` as a separator, not a
    terminator, so the last line never carries a dangling trailing blank line.
    """

    marker = "> " if index == decision_cursor else "  "
    recommended = " (Recommended)" if index == 0 and has_own_suggestions else ""
    return Text(f"{marker}{index + 1}. {entry}{recommended}")


def render_decision_cycle(finding: FindingData, decision_cursor: int) -> Text:
    """The full decision cycle: every `_decision_entries` entry, numbered from 1, one per
    line. A leading `"> "` marks `decision_cursor`'s index; entry 0 is labeled
    `" (Recommended)"` when it came from `finding.suggestions`.
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
    """Every `_decision_entries` entry except the trailing `_CUSTOM_ENTRY` (drawn
    separately by `render_custom_entry_line`/a live `Input`).

    No trailing newline: a Rich `Text` ending in `"\\n"` renders an extra invisible blank
    line beneath it in `self._entries`. A `border-top` divider marks the boundary instead.
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
    """The trailing `_CUSTOM_ENTRY`'s own line; shown instead of a live `Input` whenever
    that `Input` isn't (yet) mounted."""

    entries = _decision_entries(finding)
    index = len(entries) - 1
    return _render_decision_entry(
        index, entries[index], decision_cursor, has_own_suggestions=bool(finding.suggestions)
    )


class FindingsSuggestion(Vertical):
    """The right column of one `Finding` row -- suggestions, or a live decision cycle.

    A `Vertical` composing two `Static`s: `self._entries` for every entry before
    `_CUSTOM_ENTRY`, `self._custom` for that entry's own line, since decision mode needs
    to replace the trailing line with a live `Input` in place. Always occupies its `1fr`
    column, hidden or not (see `.tcss`'s own comment) -- the `-visible` class only toggles
    the border; `clear()`/`show_plain()`/`show_decision()` are what actually toggle the
    text content.
    """

    DEFAULT_CSS = (
        (Path(__file__).parent.parent / "tokens.tcss").read_text()
        + "\n"
        + Path(__file__).with_suffix(".tcss").read_text()
    )

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
        # Set only once a human deliberately opens the chat; None renders self._custom's
        # plain text instead.
        self._input: Input | None = None
        # Escape-binding reminder, mounted/removed in lockstep with self._input.
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
        """Render decision mode: entries before `_CUSTOM_ENTRY` in `self._entries`; the
        trailing slot shows `self._custom`'s plain text unless a chat is already open on
        this row, in which case that `Input` is left untouched. Never opens the chat
        itself -- only a deliberate confirm/cycle/jump does that, via `ensure_input`. Safe
        to call repeatedly from a periodic re-render.
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
            # Cursor moved off _CUSTOM_ENTRY while its Input was open; clean up the stale Input.
            self._remove_input()
        self._custom.update(render_custom_entry_line(finding, decision_cursor))

    def ensure_input(self, prefill: str) -> Input:
        """Mount (if not already mounted) this row's live `Input` for `_CUSTOM_ENTRY`,
        seeded with `prefill`, and return it. Idempotent: if one is already mounted, it's
        returned untouched and `prefill` is ignored."""

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
        down and re-renders `_CUSTOM_ENTRY`'s plain text. Typed text is discarded."""

        self._remove_input()
        self.show_decision(finding, decision_cursor)
