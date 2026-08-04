"""`InputPromptScreen`/`ApprovalPromptScreen`: modals collecting human input for a relayed
request.

Split out from `app.py` (rather than defined inline) so `ReviewApp`'s worker logic and
these screens' presentation stay separately readable -- see `tui/AGENTS.md` for how
`InputPromptScreen` fits into the `InputRelay` seam (issue #41) and `ApprovalPromptScreen`
into the `ApprovalRelay` seam (issue #80).
"""

from __future__ import annotations

from rich.console import RenderableType
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Static

from code_review.pipeline.findings import Finding
from code_review.pipeline.step import ApprovalDecision, StepOutcome
from code_review.steps.review import ReviewOutput
from code_review.steps.test_sufficiency import TestSufficiencyOutput
from code_review.tui.widgets import format_finding, render_findings


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


def _format_outcome(outcome: StepOutcome) -> RenderableType:
    """Render a parked `StepOutcome.findings` for display on `ApprovalPromptScreen`.

    `findings` is untyped `object` at the `pipeline/` layer (see `pipeline/step.py`'s
    `StepOutcome` docstring); the real park producers seen so far carry either a plain
    `list[Finding]` (`steps/rebase.py`'s two `needs_approval=True` returns) or a
    `ReviewOutput`/`TestSufficiencyOutput` (`steps/review.py`/`steps/test_sufficiency.py`,
    whenever `has_blocking_finding` is true) -- both rendered via the exact same functions
    `FindingsBox` already uses for a completed step's findings (`widgets.render_findings`/
    `format_finding`), so a parked step's findings look the same here as they would once
    completed. Returns `RenderableType`, not `str`, since `render_findings` itself returns
    a Rich `Group` (issue #77's two-column grid) rather than a plain string -- `Static`
    accepts either. Anything else (an empty list, or a future producer whose findings fit
    neither shape) falls back to `str(...)` rather than guessing at a schema this module
    has no business assuming.
    """

    findings = outcome.findings
    if isinstance(findings, (ReviewOutput, TestSufficiencyOutput)):
        return render_findings(findings)
    if isinstance(findings, list) and findings and all(isinstance(f, Finding) for f in findings):
        return "\n".join(format_finding(f) for f in findings)
    return str(findings)


class ApprovalPromptScreen(ModalScreen[ApprovalDecision]):
    """Shows `step_name`/`outcome` and an approve/skip/fix/abort choice; dismisses with the
    chosen `ApprovalDecision`.

    A distinct screen from `InputPromptScreen`, not a reuse of it (see `approval_relay.py`'s
    module docstring) -- the response here is a four-way choice, not free text. Offers both
    a mouse path (`Button`s) and a keyboard path (single-key `BINDINGS`, mirroring this
    app's existing single-key convention, e.g. `app.py`'s own exit binding) so a script-
    driven pty test (no mouse available) can answer it the same way a `Pilot`-driven test
    or a real interactive user would.

    "fix" (issue #81) dismisses with just the `ApprovalDecision` string, not a full
    `ApprovalResponse` -- this screen has no way to collect free-text instructions itself
    (it would need a second widget, and `InputPromptScreen` already does exactly that job).
    `ReviewApp._relay_approval` (`app.py`) is what pushes `InputPromptScreen` next when it
    sees "fix" come back from here, and only then builds the full `ApprovalResponse`.
    """

    DEFAULT_CSS = """
    ApprovalPromptScreen {
        align: center middle;
    }

    ApprovalPromptScreen > Vertical {
        width: 70;
        height: auto;
        border: round $primary;
        padding: 1 2;
        background: $surface;
    }

    ApprovalPromptScreen Static {
        margin-bottom: 1;
    }

    ApprovalPromptScreen Horizontal {
        height: auto;
        align: center middle;
    }

    ApprovalPromptScreen Button {
        margin: 0 1;
    }
    """

    BINDINGS = [
        ("a", "choose_approve", "Approve"),
        ("s", "choose_skip", "Skip"),
        ("f", "choose_fix", "Fix"),
        ("x", "choose_abort", "Abort"),
    ]

    def __init__(self, step_name: str, outcome: StepOutcome) -> None:
        super().__init__()
        self._step_name = step_name
        self._outcome = outcome

    def compose(self) -> ComposeResult:
        # `markup=False` on every Static here: `_format_outcome`'s text ultimately embeds
        # agent-produced `Finding.description` text (untrusted), which can legitimately
        # contain `[...]`-shaped substrings Rich's default markup parsing would try (and
        # sometimes fail) to interpret as style tags -- see this class's own regression
        # test, which reproduces exactly that crash against a real `ReviewOutput` finding.
        with Vertical():
            yield Static(f"{self._step_name} needs approval:", markup=False)
            yield Static(_format_outcome(self._outcome), markup=False)
            yield Static("[a] Approve   [s] Skip   [f] Fix   [x] Abort", markup=False)
            with Horizontal():
                yield Button("Approve", id="approve", variant="success")
                yield Button("Skip", id="skip", variant="warning")
                yield Button("Fix", id="fix", variant="primary")
                yield Button("Abort", id="abort", variant="error")

    def action_choose_approve(self) -> None:
        self.dismiss("approve")

    def action_choose_skip(self) -> None:
        self.dismiss("skip")

    def action_choose_fix(self) -> None:
        self.dismiss("fix")

    def action_choose_abort(self) -> None:
        self.dismiss("abort")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "approve":
            self.action_choose_approve()
        elif event.button.id == "skip":
            self.action_choose_skip()
        elif event.button.id == "fix":
            self.action_choose_fix()
        elif event.button.id == "abort":
            self.action_choose_abort()
