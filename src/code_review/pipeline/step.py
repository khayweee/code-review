"""Step abstract base class, StepContext, StepOutcome (Milestone 2, see docs/ROADMAP.md).

`Step` is deliberately a one-method abstract base class -- no lifecycle hooks, no
composition helpers -- since there is only one real `Step` consuming this shape so far
(the round-trip test in `tests/pipeline/`); the first production step (`IntentStep`) lands
in Milestone 3, see `src/code_review/steps/intent.py`.

`StepContext` is a bag of per-run dependencies a step needs: the working directory, the
`Agent` instance it calls through, the diff being reviewed, and the `Intent` supplied on
the command line. `Intent` is fully known before the pipeline starts -- it's a CLI flag,
not something discovered mid-run -- so `cli.py` constructs it once and every step gets it
off the same immutable `ctx`, rather than the first step handing it forward through its
`StepOutcome`. It is not the place fix-loop or approval state lives -- Milestone 7 extends
this type once that loop exists.

`StepContext.on_input_needed` (issue #41) carries the same interactive-input relay
`RunOpts.on_input_needed` (see `agent/base.py`) is shaped for, so a future step can pass
`ctx.on_input_needed` through to its own `RunOpts` without needing a live reference to the
TUI itself. No step consumes it yet -- none sets a non-default `permission_mode`, the only
thing that makes a backend subprocess reach for it. `cli.py` wires it to a real
`tui.input_relay.InputRelay.request_input`; tests that don't exercise it can leave it at
its default `None`.

`StepOutcome` carries `needs_approval`/`auto_fixable` now so Milestone 7 can act on them
without a breaking schema change, even though nothing branches on them yet. `findings` is
typed as `object` rather than the not-yet-built Milestone 5 `Finding`/`Findings` schema
(`pipeline/findings.py`) -- a step's own code narrows it back to whatever schema that step
validated its agent call against.

`StepEvent` (Milestone 13, issue #39) is `executor.run_steps`'s progress unit: one per
"running" and one per "completed" per step. It gets `step_name` by calling `step.get_name()`
-- a concrete method `Step` provides by default (`type(self).__name__`), overridable by a
step that needs a name distinct from its class (e.g. multiple instances of the same step
class in one run). `Step` is now a nominal `ABC`, not a structural `Protocol`, so every real
implementation (`IntentStep`, and the test fakes in `tests/pipeline/test_executor.py`)
explicitly subclasses it. `started_at`/`duration` use `time.monotonic()`, not wall-clock
time, since nothing here needs to correlate against an external clock -- only measure
elapsed time within this process.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

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
    # Reserved for a future step that pins a non-default `permission_mode` on its own
    # `RunOpts` (see `agent/base.py`'s `RunOpts.on_input_needed`) and needs to pass this
    # through so a blocked-on-stdin subprocess can relay its prompt to a human. No step
    # consumes this yet -- see the module docstring. `cli.py` wires it to
    # `tui.input_relay.InputRelay.request_input` for interactive runs.
    on_input_needed: Callable[[str], Awaitable[str]] | None = None


@dataclass(frozen=True, slots=True)
class StepOutcome:
    """A Step's report back to the executor."""

    needs_approval: bool
    auto_fixable: bool
    findings: object


class Step(ABC):
    """One unit of pipeline work: given a StepContext, produce a StepOutcome."""

    # Empty, matching `abc.ABC`'s own `__slots__ = ()`: without this, subclassing `Step`
    # would give every `@dataclass(slots=True)` implementation a `__dict__` back, silently
    # defeating the memory-layout guarantee `slots=True` exists for.
    __slots__ = ()

    @abstractmethod
    async def run(self, ctx: StepContext) -> StepOutcome:
        """Do this step's work against ``ctx`` and report what happened."""

    def get_name(self) -> str:
        """Return this step's display name, used by ``executor.run_steps`` for events.

        Defaults to the concrete class's name; a step needing a different name (e.g.
        multiple instances of the same step class in one run) overrides this.
        """

        return type(self).__name__


@dataclass(frozen=True, slots=True)
class StepEvent:
    """One progress event from ``executor.run_steps``: a step entering "running", or a
    step's "completed" report (its ``StepOutcome`` plus timing)."""

    step_name: str
    status: Literal["running", "completed"]
    outcome: StepOutcome | None
    started_at: float
    duration: float | None
