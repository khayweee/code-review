"""Test-sufficiency schema and `TestSufficiencyStep`.

Registered in `steps/registry.py`'s `IMPLEMENTED_STEPS`, which `cli.py` builds its step
list from directly.

Holds only the schema (`TestSufficiencyOutput`, `TestArtifact`) and step-orchestration
code, mirroring `steps/review.py`'s split. Prompt construction lives in
`code_review.prompt.test_sufficiency`.

Unlike `ReviewStep`, `run` does NOT call `filter_pipeline_owned_delivery_findings` --
that filter resets `ReviewOutput.risk_level`, which `TestSufficiencyOutput` doesn't have.

`TestSufficiencyStep` sets `supports_fix_round = True`, mirroring `ReviewStep`'s
fix-round shape: `run` only branches on `ctx.fix_round` to choose the prompt-assembly
function (`build_test_sufficiency_fix_prompt` vs `build_test_sufficiency_prompt`).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, Literal

from pydantic import BaseModel

from code_review.agent import RunOpts
from code_review.pipeline.findings import Finding, action_or_default, has_blocking_finding
from code_review.pipeline.step import Step, StepContext, StepOutcome
from code_review.prompt.test_sufficiency import (
    build_test_sufficiency_fix_prompt,
    build_test_sufficiency_prompt,
)
from code_review.steps.tool_activity import tool_stream_relay

# --- TestArtifact ------------------------------------------------------------------------


class TestArtifact(BaseModel):
    """One piece of evidence backing this step's verdict on a changed behavior -- the
    decision-ladder rung an agent landed on (see `prompt/test_sufficiency.py`'s
    `_DECISION_LADDER`).
    """

    # Which decision-ladder rung this represents. An unverified behavior has no artifact
    # at all (reported via TestSufficiencyOutput.findings instead), never fabricated here.
    kind: Literal["existing-test", "written-test", "manual-verification"]

    # What this artifact shows and why it counts as evidence.
    description: str

    # Optional file/line locator, e.g. "tests/test_foo.py:42". None for manual verification.
    location: str | None = None


# --- TestSufficiencyOutput ----------------------------------------------------------------


class TestSufficiencyOutput(BaseModel):
    """The test-sufficiency step's schema: findings about verification gaps, plus a record
    of what was tested and the evidence behind it. `findings` is ordered first, mirroring
    `ReviewOutput`.

    Deliberately has no `risk_level`/`risk_rationale` -- risk verdicts stay owned solely by
    `ReviewOutput`; this step assesses test sufficiency, not overall change risk.
    """

    # Structured observations about verification gaps. No scope filter runs on this list
    # (unlike ReviewOutput.findings).
    findings: list[Finding]

    # Changed behaviors judged as adequately tested.
    tested: list[str]

    # Free-text overview of test sufficiency for the diff as a whole.
    testing_summary: str

    # Evidence backing tested/testing_summary, one per decision-ladder rung climbed.
    artifacts: list[TestArtifact]


# --- TestSufficiencyStep ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TestSufficiencyStep(Step):
    """Single-pass test-sufficiency assessment: one prompt built by
    `build_test_sufficiency_prompt`, exactly one `ctx.agent.run` call against
    `TestSufficiencyOutput`, and the shared blocking-findings gate applied to compute
    `StepOutcome`. No scope filter runs here (see module docstring).

    Single agent call per round -- no retry, no session persistence. The fix/approval loop
    lives in `pipeline/executor.py`; this class only reacts to `ctx.fix_round` by choosing
    which prompt-assembly function to call.

    The agent call is wrapped in one coarse `ctx.report_activity(...)` span, and each tool
    call it streams reported via `tool_stream_relay` (`steps/tool_activity.py`) -- mirrors
    `ReviewStep.run`'s identical shape, sharing that relay rather than a copy.
    """

    # Subprocess test seam for RunOpts.executable; tests point this at a fake CLI script.
    executable: str | Path = "claude"

    # Opts into pipeline/executor.py's fix-round loop.
    supports_fix_round: ClassVar[bool] = True

    async def run(self, ctx: StepContext) -> StepOutcome:
        prompt = (
            build_test_sufficiency_fix_prompt(ctx)
            if ctx.fix_round is not None
            else build_test_sufficiency_prompt(ctx)
        )

        # Static label ("via claude"), not self.executable -- that field is a test seam,
        # this names the production backend. Reports "finished" even if the call raises.
        # Mirrors ReviewStep.run's identical outer span.
        async with ctx.report_activity("Agent: assessing test sufficiency via claude"):
            # None with no reporter attached (rather than a relay that's a no-op at
            # runtime) so the call stays on claude_cli.py's legacy --output-format json
            # path when there's nothing to stream tool calls to -- e.g. every test that
            # runs TestSufficiencyStep against a fake CLI without a
            # StepContext.activity_reporter. Mirrors ReviewStep.run's identical gating.
            on_stream_event = (
                tool_stream_relay(ctx.activity_reporter)
                if ctx.activity_reporter is not None
                else None
            )
            result = await ctx.agent.run(
                RunOpts(
                    prompt=prompt,
                    cwd=ctx.cwd,
                    output_schema=TestSufficiencyOutput,
                    executable=self.executable,
                    on_stream_event=on_stream_event,
                )
            )

        output = result.output

        # True iff a finding's resolved action is "ask-user".
        blocking = has_blocking_finding(output.findings)
        has_auto_fix = any(action_or_default(f.action) == "auto-fix" for f in output.findings)
        auto_fixable = has_auto_fix and not blocking

        return StepOutcome(
            needs_approval=blocking,
            auto_fixable=auto_fixable,
            payload=output,
        )
