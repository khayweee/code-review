"""Correctness/alignment review + risk assessment schema, and `ReviewStep` itself.

`ReviewStep` is not yet added to `steps/registry.py`'s `IMPLEMENTED_STEPS` or wired into
`cli.py`'s step list; that wiring is a later ticket.

Single prompt, single schema: `ReviewOutput` carries findings *and* required
`risk_level`/`risk_rationale` together, so the agent structurally cannot return a review
without a risk assessment.

This module holds only the schema (`ReviewOutput`) and step-orchestration
(`ReviewStep`). Prompt construction lives in `code_review.prompt.review`; the
pipeline-owned-delivery scope filter (`filter_pipeline_owned_delivery_findings`) lives in
`code_review.pipeline.findings`.

`ReviewStep` sets `supports_fix_round = True`, opting into `pipeline/executor.py`'s
fix-before-park round. `run`'s only branch on `ctx.fix_round` is which prompt-assembly
function to call (`build_review_fix_prompt` vs `build_review_prompt`); everything else is
identical between rounds.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, Literal

from pydantic import BaseModel

from code_review.agent import RunOpts
from code_review.pipeline.findings import (
    Finding,
    action_or_default,
    filter_pipeline_owned_delivery_findings,
    has_blocking_finding,
)
from code_review.pipeline.step import Step, StepContext, StepOutcome
from code_review.prompt.review import build_review_fix_prompt, build_review_prompt

# --- ReviewOutput ----------------------------------------------------------------------


class ReviewOutput(BaseModel):
    """The Review step's schema: correctness findings plus a required risk verdict.
    `findings` is ordered first so the agent reasons about issues before summarizing risk.
    """

    # Structured observations (see `pipeline/findings.py`'s `Finding`).
    findings: list[Finding]

    # Overall risk level; required so the agent cannot skip a risk verdict.
    risk_level: Literal["low", "medium", "high"]

    # Justification for risk_level. Overwritten (not appended to) by
    # filter_pipeline_owned_delivery_findings when it resets risk_level.
    risk_rationale: str

    # Delivery scope the agent judges its risk assessment to be about. Reserved: no code
    # reads this field yet.
    risk_scope: Literal["source-or-external", "pipeline-owned-delivery"] | None = None


# --- ReviewStep --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ReviewStep(Step):
    """Single-pass correctness/alignment review and risk assessment: one prompt built by
    `build_review_prompt`, exactly one `ctx.agent.run` call against `ReviewOutput`, and the
    pipeline-owned-delivery scope filter applied to the parsed answer.

    Single agent call per round -- no retry, no session persistence. The fix/approval loop
    that decides whether to re-run this step lives in `pipeline/executor.py`; this class
    only reacts to `ctx.fix_round` by choosing which prompt-assembly function to call.

    The agent call is wrapped in one coarse `ctx.report_activity(...)` span (the `Agent`
    protocol has no finer-grained progress channel).
    """

    # Subprocess test seam for RunOpts.executable; tests point this at a fake CLI script.
    executable: str | Path = "claude"

    # Opts into pipeline/executor.py's fix-round loop.
    supports_fix_round: ClassVar[bool] = True

    async def run(self, ctx: StepContext) -> StepOutcome:
        prompt = (
            build_review_fix_prompt(ctx) if ctx.fix_round is not None else build_review_prompt(ctx)
        )

        # Static label ("via claude"), not self.executable -- that field is a test seam,
        # this names the production backend. Reports "finished" even if the call raises.
        async with ctx.report_activity("Agent: reviewing diff via claude"):
            result = await ctx.agent.run(
                RunOpts(
                    prompt=prompt,
                    cwd=ctx.cwd,
                    output_schema=ReviewOutput,
                    executable=self.executable,
                )
            )

        filtered = filter_pipeline_owned_delivery_findings(result.output)

        # True iff a surviving finding's resolved action is "ask-user".
        blocking = has_blocking_finding(filtered.findings)
        has_auto_fix = any(
            action_or_default(finding.action) == "auto-fix" for finding in filtered.findings
        )
        auto_fixable = has_auto_fix and not blocking

        return StepOutcome(
            needs_approval=blocking,
            auto_fixable=auto_fixable,
            findings=filtered,
        )
