"""The focusable `ListView` hosting one `Finding` per finding.

- `ListView(can_focus=True, can_focus_children=False)`: the `ListView` itself holds
  keyboard focus, its `Finding` children never individually focused.
- Every parked-mode key binding beyond up/down/enter (which `ListView` gives for free)
  lives here, never on `Finding` itself, since `ListView`'s own action methods
  index/assert against `self._nodes` unfiltered.
- All bindings delegate to the owning `FindingsList`, which no-ops them while not parked
  -- this class holds no decision state of its own.
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

    - left/right cycle the highlighted finding's decision entries.
    - "s" records "skip" for the highlighted row; "x" jumps straight to abort regardless
      of cursor position (the one binding that stays global and step-scoped).
    - "f" jumps straight to the inline chat; digit keys "1".."9" jump straight to that
      1-based entry.
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
        # `ListView` assumes every mounted child is a `ListItem` and indexes into
        # `self._nodes` unfiltered -- any non-`Finding` child mounted here would silently
        # corrupt that indexing, not raise anywhere near the mistake.
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
