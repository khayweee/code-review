"""Passive plumbing data types owned by `tui/`: one reported sub-step activity transition,
plus the two queued-request shapes `ApprovalRelay`/`InputRelay` hand between a parked/
blocked pipeline call and `ReviewApp`'s workers.

Every type here is a plain `@dataclass(frozen=True, slots=True)`, not pydantic -- see
`pipeline/schemas.py`'s own module docstring for why (same rule, same reasoning, mirrored
here since nothing in this module crosses an LLM-output boundary either).

This module may depend on `pipeline/schemas.py` (one-directional); `pipeline/` must never
import `code_review.tui` back (see `pipeline/step.py`'s module docstring).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    # Lazy annotations (from __future__ import annotations) make TYPE_CHECKING-only
    # sufficient -- neither ApprovalResponse nor StepOutcome is constructed here, only
    # referenced in a field's type.
    from code_review.pipeline.schemas import ApprovalResponse
    from code_review.pipeline.step import StepOutcome

ActivityStatus = Literal["started", "finished"]


@dataclass(frozen=True, slots=True)
class ActivityEvent:
    """One reported activity transition: an `activity()` block starting or finishing."""

    activity_id: int
    # Derived from whichever activity was open when this one started; None if top-level.
    parent_id: int | None
    label: str
    status: ActivityStatus
    timestamp: float  # time.monotonic()
    # Set only on a "finished" event whose block called ActivityHandle.fail(detail); None
    # on every "started" event and every successful "finished" one.
    error: str | None = None


@dataclass(frozen=True, slots=True)
class ApprovalRequest:
    """One parked step's request for a human decision, queued by `ApprovalRelay`.

    `step_name`/`outcome` are what the request is about; `pending_response` is where the
    eventual `ApprovalResponse` goes once a human answers (see `ApprovalRelay`/`ReviewApp`).
    """

    step_name: str
    outcome: StepOutcome
    pending_response: asyncio.Future[ApprovalResponse]


@dataclass(frozen=True, slots=True)
class InputRequest:
    """One blocked backend call's prompt, queued by `InputRelay`, paired with where the
    human's typed answer eventually goes."""

    prompt: str
    pending_answer: asyncio.Future[str]
