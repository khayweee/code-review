"""The focusable `ListView` hosting one `Finding` per finding.

Holds keyboard focus itself (`can_focus_children=False`); `Finding` rows are never
individually focused. Every parked-mode binding beyond up/down/enter delegates to the
owning `FindingsList`, which no-ops them while not parked -- this class holds no
decision state of its own.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from textual.binding import Binding
from textual.widgets import ListView

from code_review.tui.widgets.finding import Finding

if TYPE_CHECKING:
    from code_review.tui.widgets.findings_list import FindingsList


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

    def __init__(self, *items: Finding, owner: FindingsList) -> None:
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
