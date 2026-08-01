"""Step protocol, StepContext, StepOutcome (Milestone 2, see docs/ROADMAP.md).

`Step` is deliberately a one-method protocol -- no lifecycle hooks, no composition
helpers -- since there is only one real `Step` consuming this shape so far (the
round-trip test in `tests/pipeline/`); the first production step lands in Milestone 3.

`StepContext` is a bag of per-run dependencies a step needs: the working directory, the
`Agent` instance it calls through, and the diff being reviewed. It is not the place
fix-loop or approval state lives -- Milestone 6 extends this type once that loop exists.

`StepOutcome` carries `needs_approval`/`auto_fixable` now so Milestone 6 can act on them
without a breaking schema change, even though nothing branches on them yet. `findings` is
typed as `object` rather than the not-yet-built Milestone 4 `Finding`/`Findings` schema
(`pipeline/findings.py`) -- a step's own code narrows it back to whatever schema that step
validated its agent call against.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from code_review.agent import Agent


@dataclass(frozen=True, slots=True)
class StepContext:
    """Per-run dependencies and state a Step needs in order to run."""

    cwd: Path
    agent: Agent
    diff: str


@dataclass(frozen=True, slots=True)
class StepOutcome:
    """A Step's report back to the executor."""

    needs_approval: bool
    auto_fixable: bool
    findings: object


class Step(Protocol):
    """One unit of pipeline work: given a StepContext, produce a StepOutcome."""

    async def run(self, ctx: StepContext) -> StepOutcome:
        """Do this step's work against ``ctx`` and report what happened."""
        ...
