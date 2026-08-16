"""Passive plumbing data types shared across `pipeline/` and its callers: a step's
progress event, a human's approval response, fix-round instructions, and one finding paired
with its decision.

Every type here is a plain `@dataclass(frozen=True, slots=True)`, not pydantic -- none of
these ever cross an LLM-output boundary the way `pipeline.findings.Finding` does (see that
class's own docstring for why it's the deliberate exception, not the rule these follow).

This module must never import `code_review.tui` -- directly or transitively (see
`pipeline/step.py`'s module docstring and `pipeline/AGENTS.md`). That constraint is exactly
why these types live in a `pipeline/`-owned module instead of a single shared schema file:
`tui/schemas.py` is free to depend on this module, but never the other way around.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    # steps/ depends on pipeline/, never the reverse; step.py never imports this module at
    # top level, so a top-level import back from here would be circular either way (step.py
    # imports FixRound/ApprovalResponse from here at runtime -- see its own docstring).
    # Lazy annotations (from __future__ import annotations) make TYPE_CHECKING-only
    # sufficient for both.
    from code_review.pipeline.findings import Finding
    from code_review.pipeline.step import StepOutcome


@dataclass(frozen=True, slots=True)
class FixRound:
    """What to fix in a fix-mode re-run, as free text.

    Carries either the auto-fix findings' rendered description
    (`pipeline/findings.py`'s `describe_auto_fix_findings`) or a human's typed "fix"
    instructions, collapsed to one `instructions: str` so a step's prompt only ever
    branches on `ctx.fix_round is not None`.
    """

    instructions: str


# Standalone alias (not inlined) so tui/approval_relay.py and tui/screens.py can import the
# same type instead of each defining an overlapping Literal.
ApprovalDecision = Literal["approve", "skip", "abort", "fix"]


@dataclass(frozen=True, slots=True)
class ApprovalResponse:
    """A human's answer to a parked step's approval request.

    `instructions` is set only when `decision == "fix"` (the human's typed text), `None`
    otherwise. `executor.run_steps` turns a "fix" response into a `FixRound` and re-runs
    the step; "approve"/"skip" continue to the next step; "abort" raises
    `executor.RunAbortedError`.
    """

    decision: ApprovalDecision
    instructions: str | None = None


@dataclass(frozen=True, slots=True)
class StepEvent:
    """One progress event from ``executor.run_steps``: a step entering "running", or a
    step's "completed" report (its ``StepOutcome`` plus timing)."""

    step_name: str
    status: Literal["running", "completed"]
    outcome: StepOutcome | None
    started_at: float
    duration: float | None


@dataclass(frozen=True, slots=True)
class FindingDecision:
    """One finding paired with the human's `ApprovalResponse` decided against it: the
    building block `tui.widgets.FindingBox._resolve_park` accumulates one per row, in
    `self._rows` order, before folding all of them into `describe_finding_decisions`.

    A plain frozen dataclass, not a pydantic `BaseModel`, matching every other
    internal-plumbing type here (`ApprovalResponse`, `FixRound`, `StepEvent`): nothing
    about this pairing crosses an LLM-output boundary the way `Finding` itself does (see
    `Finding`'s own docstring), so there's nothing for pydantic validation to buy here. A
    dataclass field holding a `Finding` (a `BaseModel`) needs no special config either way.
    """

    finding: Finding
    response: ApprovalResponse
