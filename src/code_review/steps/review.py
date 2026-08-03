"""Correctness/alignment review + risk assessment schema, and `ReviewStep` itself --
Milestone 5 (see docs/ROADMAP.md), sliced as issue #26 (schema) and #27 (`ReviewStep`,
unblocked by #26 landing above). `ReviewStep` is deliberately NOT added to
`steps/registry.py`'s `IMPLEMENTED_STEPS` or wired into `cli.py`'s step list here -- issue
#27 is scoped to proving the class directly, the same way `tests/pipeline/test_executor.py`'s
`_ReviewStep`/`_OrderStep` are proven without ever being registered anywhere; that wiring
is a later ticket.

Single prompt, single schema: `ReviewOutput` carries findings *and* the required
`risk_level`/`risk_rationale` fields together (see docs/GLOSSARY.md's "Risk verdict") --
risk is not a separate step, and a required schema field means the agent structurally
cannot return a review without a risk assessment, the way prompt wording alone cannot
guarantee (see root AGENTS.md's design invariants).

This module holds only the schema (`ReviewOutput`) and step-orchestration code
(`ReviewStep`). Prompt-construction logic -- `intent_conformance_clause` and the
prompt-assembly function (`build_review_prompt`) -- moved to `code_review.prompt.review`
in a later structural refactor, and the deterministic pipeline-owned-delivery scope filter
(`filter_pipeline_owned_delivery_findings`) moved to `code_review.pipeline.findings`
alongside its `Finding`-processing siblings. `ReviewStep.run` imports and calls both.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from code_review.agent import RunOpts
from code_review.pipeline.findings import (
    Finding,
    action_or_default,
    filter_pipeline_owned_delivery_findings,
    has_blocking_finding,
)
from code_review.pipeline.step import Step, StepContext, StepOutcome
from code_review.prompt.review import build_review_prompt

# --- ReviewOutput ----------------------------------------------------------------------


class ReviewOutput(BaseModel):
    """The Review step's schema: correctness findings plus a required risk verdict on
    the same object (see docs/GLOSSARY.md's "Risk verdict"). Field order is deliberate --
    `findings` first, then the risk fields -- mirroring the reference implementation's
    chain-of-thought ordering: an agent reasons about what is wrong before it summarizes
    overall risk, so the schema asks for findings first for the same reason.
    """

    # The review's structured observations (see `pipeline/findings.py`'s `Finding`).
    # Consumer: `filter_pipeline_owned_delivery_findings` (`pipeline/findings.py`) strips
    # pipeline-owned-delivery-scoped entries; `pipeline/findings.py`'s
    # `has_blocking_finding` (Milestone 7's fix/approval loop) decides whether the run
    # needs a human from this list.
    findings: list[Finding]

    # Overall risk level of the change, required so the agent cannot return a review
    # without a risk verdict (see docs/GLOSSARY.md's "Risk verdict"). Consumer: Milestone
    # 7's fix/approval loop and, once filtering resets it, `pipeline/findings.py`'s own
    # scope filter.
    risk_level: Literal["low", "medium", "high"]

    # Written justification for `risk_level`, required for the same reason `risk_level`
    # is. Consumer: rendered alongside the risk-flavored PR body line (Milestone 8);
    # overwritten (not appended to) by `filter_pipeline_owned_delivery_findings` when it
    # resets `risk_level`, so the rationale always describes the risk level actually
    # returned, never a stale explanation for a level that no longer holds.
    risk_rationale: str

    # Which delivery scope the agent judges its own risk assessment to be about. Only the
    # enum shape is built here -- no code in this module reads this field yet (the scope
    # filter in `pipeline/findings.py` reasons from `Finding.review_scope`/
    # `Finding.severity` instead, not from this field). "source-or-external" covers the
    # common case of author-written or externally-delivered changes;
    # "pipeline-owned-delivery" is a reserved value for future use once something consumes
    # it. Optional (defaults to `None`) because, unlike `risk_level`/`risk_rationale`, no
    # pipeline decision depends on it being present yet (see docs/GLOSSARY.md's "Risk
    # verdict", which defines the required risk verdict as level plus rationale only).
    risk_scope: Literal["source-or-external", "pipeline-owned-delivery"] | None = None


# --- ReviewStep --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ReviewStep(Step):
    """Milestone 5's single-pass correctness/alignment review and risk assessment (issue
    #27): one prompt built by `build_review_prompt` (`code_review.prompt.review`), exactly
    one `ctx.agent.run` call against `ReviewOutput`, and the deterministic
    pipeline-owned-delivery scope filter (`code_review.pipeline.findings`) applied to the
    parsed answer before it becomes this step's `StepOutcome`.

    Single pass only -- no retry, no re-review, no session persistence (see
    docs/GLOSSARY.md's "Agent": one call in, one result out; a step that needs more calls
    makes more calls, this one needs exactly one). The fix/approval loop that would act on
    a parked run is Milestone 7's, not this step's.
    """

    # Subprocess test seam threaded through to `RunOpts.executable` (see `agent/base.py`'s
    # own field comment: "swap for a fake CLI in tests"). Defaults to "claude" for
    # production use; tests construct `ReviewStep(executable=...)` to point the real
    # `ClaudeCLI` backend at a fake CLI script, mirroring
    # `tests/pipeline/test_executor.py`'s `_OrderStep.executable` field for the same
    # reason. No current caller overrides it -- `ReviewStep` is not yet registered in
    # `steps/registry.py`'s `IMPLEMENTED_STEPS` or wired into `cli.py` (see module
    # docstring); that wiring is a later ticket.
    executable: str | Path = "claude"

    async def run(self, ctx: StepContext) -> StepOutcome:
        result = await ctx.agent.run(
            RunOpts(
                prompt=build_review_prompt(ctx),
                cwd=ctx.cwd,
                output_schema=ReviewOutput,
                executable=self.executable,
            )
        )

        filtered = filter_pipeline_owned_delivery_findings(result.output)

        # The shared blocking-findings gate (see docs/GLOSSARY.md's "Blocking finding"):
        # true iff a surviving finding's resolved action is "ask-user".
        blocking = has_blocking_finding(filtered.findings)
        # Auto-fixable iff at least one surviving finding resolves to "auto-fix" and none
        # resolve to "ask-user" -- the latter half is exactly `not blocking` above, kept
        # as its own gate rather than folded into one boolean expression so each half of
        # the acceptance rule ("at least one auto-fix" / "no ask-user") stays independently
        # readable and testable.
        has_auto_fix = any(
            action_or_default(finding.action) == "auto-fix" for finding in filtered.findings
        )
        auto_fixable = has_auto_fix and not blocking

        return StepOutcome(
            needs_approval=blocking,
            auto_fixable=auto_fixable,
            findings=filtered,
        )
