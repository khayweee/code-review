"""Step protocol, StepContext, StepOutcome (Milestone 2, see docs/ROADMAP.md).

`Step` is deliberately a one-method protocol -- no lifecycle hooks, no composition
helpers -- since there is only one real `Step` consuming this shape so far (the
round-trip test in `tests/pipeline/`); the first production step (`IntentStep`) lands in
Milestone 3, see `src/code_review/steps/intent.py`.

`StepContext` is a bag of per-run dependencies a step needs: the working directory, the
`Agent` instance it calls through, the diff being reviewed, and the `Intent` supplied on
the command line. `Intent` is fully known before the pipeline starts -- it's a CLI flag,
not something discovered mid-run -- so `cli.py` constructs it once and every step gets it
off the same immutable `ctx`, rather than the first step handing it forward through its
`StepOutcome`. It is not the place fix-loop or approval state lives -- Milestone 6 extends
this type once that loop exists.

`StepOutcome` carries `needs_approval`/`auto_fixable` now so Milestone 6 can act on them
without a breaking schema change, even though nothing branches on them yet. `findings` is
typed as `object` rather than the not-yet-built Milestone 4 `Finding`/`Findings` schema
(`pipeline/findings.py`) -- a step's own code narrows it back to whatever schema that step
validated its agent call against.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from code_review.agent import Agent

if TYPE_CHECKING:
    # Import-direction note: `steps/` depends on `pipeline/`, never the reverse. A
    # top-level import of `Intent` here would be circular, since `steps/intent.py` needs
    # `StepContext`/`StepOutcome` at module level to construct real instances of them.
    # This module already has `from __future__ import annotations`, so the `intent:
    # Intent` field annotation below is a lazy string dataclass never evaluates at
    # runtime -- a type-checking-only import is sufficient and avoids the cycle.
    from code_review.steps.intent import Intent


@dataclass(frozen=True, slots=True)
class StepContext:
    """Per-run dependencies and state a Step needs in order to run."""

    cwd: Path
    agent: Agent
    diff: str
    intent: Intent


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
