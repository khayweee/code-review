"""Central list that defines which pipeline steps exists and in what order.

`STEP_REGISTRY` is the single source of truth for step identity, in pipeline order, whether
or not a step's class exists yet -- each entry matches the corresponding `IMPLEMENTED_STEPS`
class's `get_name()` (the class name, per `Step.get_name()`'s default), so `tui/` can match a
`StepEvent` back to its registry row without either side overriding `get_name()`.
`STEP_DISPLAY_NAMES` maps each `STEP_REGISTRY` entry to the friendly name `tui/` actually
renders (e.g. `"ReviewStep"` -> `"Review"`), kept as a separate mapping rather than changing
`get_name()` itself so step classes stay untouched and `StepEvent`/registry matching keeps
using the stable class-derived name. `cli.py` builds the real step list from
`IMPLEMENTED_STEPS`; `tui/` reads `STEP_REGISTRY` directly to render not-yet-implemented
steps as pending placeholders, translating through `STEP_DISPLAY_NAMES` only at render time.

Lives in `steps/`, not `pipeline/`, since `pipeline/` must never import `steps/`.
"""

from __future__ import annotations

from code_review.pipeline.step import Step
from code_review.steps.intent import IntentStep
from code_review.steps.pr import PRStep
from code_review.steps.rebase import RebaseStep
from code_review.steps.review import ReviewStep
from code_review.steps.test_sufficiency import TestSufficiencyStep
from code_review.steps.worktree import WorktreeStep

# Every step this pipeline will ever run, in fixed order, present or not-yet-written.
STEP_REGISTRY: tuple[str, ...] = (
    "WorktreeStep",
    "IntentStep",
    "RebaseStep",
    "ReviewStep",
    "TestSufficiencyStep",
    "PRStep",
)

# Ordered prefix of `STEP_REGISTRY` that has a class today; `get_name()` must match the
# corresponding position in `STEP_REGISTRY`.
IMPLEMENTED_STEPS: tuple[type[Step], ...] = (
    WorktreeStep,
    IntentStep,
    RebaseStep,
    ReviewStep,
    TestSufficiencyStep,
    PRStep,
)

# Friendly display name for every `STEP_REGISTRY` entry -- `tui/` shows these instead of the
# raw class name. Must have exactly one entry per `STEP_REGISTRY` entry; enforced by
# `tests/steps/test_registry.py`.
STEP_DISPLAY_NAMES: dict[str, str] = {
    "WorktreeStep": "Worktree",
    "IntentStep": "Intent",
    "RebaseStep": "Rebase",
    "ReviewStep": "Review",
    "TestSufficiencyStep": "Test Sufficiency",
    "PRStep": "Pull Request",
}
