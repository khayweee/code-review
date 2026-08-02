"""The canonical, ordered step registry (Milestone 13, issue #40).

`STEP_REGISTRY` is the single source of truth for step display names, in pipeline order --
every entry matches what `Step.get_name()` returns for that step's class (the concrete
class's `__name__` by default; see `pipeline/step.py`), whether or not that class has been
written yet. Two callers read it for two different reasons:

- `cli.py` builds the real, executable step list from `IMPLEMENTED_STEPS` (an ordered
  prefix of classes, not a second list of names) so the step-name string lives exactly
  once, in `STEP_REGISTRY`.
- `tui/` reads `STEP_REGISTRY` directly to backfill not-yet-implemented steps as pending
  placeholders (see `tui/state.py`'s `backfill`), so a step that hasn't landed yet still
  renders in the live pipeline-progress view without any `tui/` code change once its class
  exists and is added to `IMPLEMENTED_STEPS`.

This module lives in `steps/`, not `pipeline/`, so that `pipeline/` never has to import
`steps/` -- this repo's fixed dependency direction is "steps depends on pipeline, never the
reverse" (see root `AGENTS.md`), and a registry of concrete step classes belongs on the
`steps/` side of that boundary.
"""

from __future__ import annotations

from code_review.pipeline.step import Step
from code_review.steps.intent import IntentStep

# Ordered, canonical display-identity list: every step this pipeline will ever run, present
# or not-yet-written, in the fixed order `docs/ROADMAP.md`'s milestones define. Adding a
# step here before its class exists is what lets it render as a pending placeholder.
STEP_REGISTRY: tuple[str, ...] = (
    "IntentStep",
    "RebaseStep",
    "ReviewStep",
    "TestSufficiencyStep",
    "PRStep",
)

# Ordered prefix of `STEP_REGISTRY` that actually has a class today. `cli.py` builds the
# real step list as `[cls() for cls in IMPLEMENTED_STEPS]`. Each entry's `get_name()` must
# match the corresponding position in `STEP_REGISTRY` -- see `tests/steps/test_registry.py`.
IMPLEMENTED_STEPS: tuple[type[Step], ...] = (IntentStep,)
