"""The canonical, ordered step registry.

`STEP_REGISTRY` is the single source of truth for step display names, in pipeline order,
whether or not a step's class exists yet. `cli.py` builds the real step list from
`IMPLEMENTED_STEPS`; `tui/` reads `STEP_REGISTRY` directly to render not-yet-implemented
steps as pending placeholders.

Lives in `steps/`, not `pipeline/`, since `pipeline/` must never import `steps/`.
"""

from __future__ import annotations

from code_review.pipeline.step import Step
from code_review.steps.intent import IntentStep
from code_review.steps.rebase import RebaseStep
from code_review.steps.review import ReviewStep
from code_review.steps.test_sufficiency import TestSufficiencyStep

# Every step this pipeline will ever run, in fixed order, present or not-yet-written.
STEP_REGISTRY: tuple[str, ...] = (
    "IntentStep",
    "RebaseStep",
    "ReviewStep",
    "TestSufficiencyStep",
    "PRStep",
)

# Ordered prefix of `STEP_REGISTRY` that has a class today; `get_name()` must match the
# corresponding position in `STEP_REGISTRY`.
IMPLEMENTED_STEPS: tuple[type[Step], ...] = (
    IntentStep,
    RebaseStep,
    ReviewStep,
    TestSufficiencyStep,
)
