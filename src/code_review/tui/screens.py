"""`InputPromptScreen`: a modal collecting one line of human input for a relayed prompt.

Split out from `app.py` (rather than defined inline) so `ReviewApp`'s worker logic and
this screen's presentation stay separately readable -- see `tui/AGENTS.md` for how this
fits into the `InputRelay` seam (issue #41).
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, Static


class InputPromptScreen(ModalScreen[str]):
    """Shows `prompt` and one `Input` field; dismisses with the submitted line."""

    DEFAULT_CSS = """
    InputPromptScreen {
        align: center middle;
    }

    InputPromptScreen > Vertical {
        width: 60;
        height: auto;
        border: round $primary;
        padding: 1 2;
        background: $surface;
    }

    InputPromptScreen Static {
        margin-bottom: 1;
    }
    """

    def __init__(self, prompt: str) -> None:
        super().__init__()
        self._prompt = prompt

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(self._prompt)
            yield Input()

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value)
