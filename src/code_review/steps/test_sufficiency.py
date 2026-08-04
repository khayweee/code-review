"""Test-sufficiency schema and `TestSufficiencyStep` -- Milestone 6 (see docs/ROADMAP.md),
specced as parent issue #58, sliced into #59 (schema, prompt, `TestSufficiencyStep` itself)
and #60/#61 (wiring into `steps/registry.py`'s `IMPLEMENTED_STEPS`/`cli.py` and the TUI,
respectively). `TestSufficiencyStep` is deliberately NOT added to `IMPLEMENTED_STEPS` or
wired into `cli.py`'s step list here -- #59 is scoped to proving the class directly, the
same way `ReviewStep` (#27) was proven before its own later wiring ticket, and the same way
`tests/pipeline/test_executor.py`'s `_ReviewStep`/`_OrderStep` are proven without ever being
registered anywhere.

This module holds only the schema (`TestSufficiencyOutput`, `TestArtifact`) and
step-orchestration code (`TestSufficiencyStep`), mirroring `steps/review.py`'s split.
Prompt-construction logic -- the decision-ladder/guardrail clause text and the
prompt-assembly function (`build_test_sufficiency_prompt`) -- lives in
`code_review.prompt.test_sufficiency` from the start (see that module's own docstring for
why it is not folded into `prompt/review.py`); `TestSufficiencyStep.run` imports and calls
it.

Unlike `ReviewStep`, `TestSufficiencyStep.run` does NOT call
`filter_pipeline_owned_delivery_findings`. That deterministic scope filter
(`pipeline/findings.py`) is Review-specific: it resets a `ReviewOutput`-only field
(`risk_level`) that `TestSufficiencyOutput` deliberately does not have (see this module's
schema docstring), so there is nothing on this step's output for it to operate on. Per
issue #58's Implementation Decisions, extracting a shared test-quality-rule constant used
by both steps' prompts is explicitly deferred, not part of this ticket -- this module's
guardrail text lives only in `prompt/test_sufficiency.py`, with no cross-step sharing yet.

`has_blocking_finding`/`action_or_default` (`pipeline/findings.py`) are reused here
unmodified -- both are explicitly documented in that module as shared across Milestone 5
(Review) and Milestone 6 (this step); see that module's own docstring.

**Fix mode (Milestone 7, issue #82)**: `TestSufficiencyStep` is the second step (after
`steps/review.py`'s `ReviewStep`, issue #81) to set `supports_fix_round: ClassVar[bool] =
True` (`pipeline/step.py`), opting into `pipeline/executor.py`'s bounded
auto-fix-before-park round and the uncapped human-"fix" park response. `TestSufficiencyStep.
run`'s own shape is unchanged by this: still exactly one `ctx.agent.run` call per
invocation (per round), still the same `has_blocking_finding`/`action_or_default`-based
`auto_fixable`/`needs_approval` post-processing applied to whatever `TestSufficiencyOutput`
comes back, and still no call to `filter_pipeline_owned_delivery_findings` (unaffected
either way, see above). The only branch this method adds is which prompt-assembly function
to call: `build_test_sufficiency_fix_prompt(ctx)` when `ctx.fix_round is not None`,
`build_test_sufficiency_prompt(ctx)` otherwise -- see `prompt/test_sufficiency.py`'s own
docstring for what a fix-mode prompt asks the agent to do differently, mirroring
`ReviewStep`'s identical shape exactly (see `steps/review.py`'s own "Fix mode" docstring
section).
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

# --- TestArtifact ------------------------------------------------------------------------


class TestArtifact(BaseModel):
    """One concrete piece of evidence backing this step's verdict on a changed behavior --
    the decision-ladder rung an agent actually landed on for that behavior (see
    `prompt/test_sufficiency.py`'s `_DECISION_LADDER`).
    """

    # Which decision-ladder rung this artifact represents: an already-existing test that
    # covers the behavior, a test the agent wrote or improved as part of this run, or a
    # manual-verification step performed in place of a test. There is no fourth "kind" for
    # the ladder's honest-failure rung -- an unverified behavior has no artifact at all and
    # is instead reported via `TestSufficiencyOutput.findings`, never fabricated here.
    # Consumer: rendered to the user (TUI findings/evidence display, once built) and the PR
    # body's evidence section (Milestone 8).
    kind: Literal["existing-test", "written-test", "manual-verification"]

    # Human-readable account of what this artifact shows: which behavior it covers, what
    # was run or observed, and why it counts as evidence. Consumer: same as `kind` above --
    # rendered wherever this step's evidence is shown to a human.
    description: str

    # Optional file/line locator for this artifact, e.g. "tests/test_foo.py:42" for an
    # existing or written test, or `None` when the artifact is a manual-verification step
    # with no single file to point at. Mirrors `Finding.location`'s optional file/line
    # locator convention (`pipeline/findings.py`). Consumer: same rendering sites as
    # `description`, when present.
    location: str | None = None


# --- TestSufficiencyOutput ----------------------------------------------------------------


class TestSufficiencyOutput(BaseModel):
    """The test-sufficiency step's schema: findings about verification gaps, plus a record
    of what was actually tested and the evidence behind it. Field order is deliberate --
    `findings` first, mirroring `ReviewOutput`'s (`steps/review.py`) chain-of-thought
    ordering rationale: an agent reasons about what is inadequately verified before it
    summarizes what it did verify, so this schema asks for findings first for the same
    reason.

    Deliberately has no `risk_level`/`risk_rationale` fields. Risk verdicts stay owned
    solely by `ReviewOutput` (`steps/review.py`) -- this is an intentional omission, not an
    oversight: this step assesses test sufficiency, not overall change risk, and
    duplicating a risk verdict across two steps would give two independent, possibly
    conflicting answers to the same question.
    """

    # This step's structured observations about verification gaps (see
    # `pipeline/findings.py`'s `Finding`). Consumer: `pipeline/findings.py`'s
    # `has_blocking_finding` decides whether the run needs a human from this list, the same
    # shared gate `ReviewStep` uses. Unlike `ReviewStep`, nothing here filters this list --
    # there is no test-sufficiency equivalent of the pipeline-owned-delivery scope filter
    # (see module docstring).
    findings: list[Finding]

    # Human-readable list naming each changed behavior this step judged as adequately
    # tested (by any decision-ladder rung short of the honest-failure one). Consumer:
    # rendered alongside `testing_summary` wherever this step's verdict is shown to a
    # human (TUI, PR body); no pipeline decision branches on this list's contents.
    tested: list[str]

    # Free-text overview of this step's overall verdict on test sufficiency for the diff as
    # a whole. Consumer: same rendering sites as `tested` above.
    testing_summary: str

    # The concrete evidence backing `tested`/`testing_summary` above -- one `TestArtifact`
    # per decision-ladder rung actually climbed (see `TestArtifact`'s own docstring).
    # Consumer: same rendering sites as `tested`/`testing_summary`; no pipeline decision
    # branches on this list today.
    artifacts: list[TestArtifact]


# --- TestSufficiencyStep ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TestSufficiencyStep(Step):
    """Milestone 6's single-pass test-sufficiency assessment (issue #59): one prompt built
    by `build_test_sufficiency_prompt` (`code_review.prompt.test_sufficiency`), exactly one
    `ctx.agent.run` call against `TestSufficiencyOutput`, and the shared blocking-findings
    gate (`pipeline/findings.py`) applied to the parsed answer to compute this step's
    `StepOutcome` -- no scope filter runs here (see module docstring).

    Single pass, single agent call *per round* -- no retry, no session persistence within
    one call (see docs/GLOSSARY.md's "Agent": one call in, one result out), mirroring
    `ReviewStep`'s own single-pass rationale. The fix/approval loop that decides whether and
    how to re-run this step lives in `pipeline/executor.py` (Milestone 7, issues #80/#81),
    not here -- this class has no idea it is on its second, third, or Nth round;
    `ctx.fix_round` is the only signal that differs between rounds, and it only changes
    which prompt-assembly function `run` calls (issue #82).
    """

    # Subprocess test seam threaded through to `RunOpts.executable`, mirroring
    # `ReviewStep.executable` (`steps/review.py`) for the same reason: defaults to "claude"
    # for production use; tests construct `TestSufficiencyStep(executable=...)` to point
    # the real `ClaudeCLI` backend at a fake CLI script. No current caller overrides it --
    # `TestSufficiencyStep` is not yet registered in `steps/registry.py`'s
    # `IMPLEMENTED_STEPS` or wired into `cli.py` (see module docstring); that wiring is
    # issue #60.
    executable: str | Path = "claude"

    # Opts into `pipeline/executor.py`'s fix-round loop (issue #82) -- see this class's own
    # "Fix mode" docstring section and `pipeline/step.py`'s `Step.supports_fix_round` for
    # why this must be an explicit per-step opt-in rather than following from
    # `StepOutcome.auto_fixable` alone. `TestSufficiencyStep` is the second step (after
    # `steps/review.py`'s `ReviewStep`, issue #81) to set this `True`.
    supports_fix_round: ClassVar[bool] = True

    async def run(self, ctx: StepContext) -> StepOutcome:
        # Fix mode (issue #82): a fix round needs the agent to actually act on
        # `ctx.fix_round.instructions` (write the missing test, perform the missing manual
        # verification, etc.) and re-assess its own result, not just assess a static diff --
        # see `build_test_sufficiency_fix_prompt`'s own docstring for the prompt-level
        # differences and why it does not rely solely on `ctx.diff`. Which prompt-assembly
        # function to call is the only thing that differs between an ordinary round and a
        # fix round; every other line below (the agent call shape, the post-processing) is
        # identical, mirroring `ReviewStep.run`'s identical shape (`steps/review.py`).
        prompt = (
            build_test_sufficiency_fix_prompt(ctx)
            if ctx.fix_round is not None
            else build_test_sufficiency_prompt(ctx)
        )

        result = await ctx.agent.run(
            RunOpts(
                prompt=prompt,
                cwd=ctx.cwd,
                output_schema=TestSufficiencyOutput,
                executable=self.executable,
            )
        )

        output = result.output

        # The shared blocking-findings gate (see docs/GLOSSARY.md's "Blocking finding"):
        # true iff a finding's resolved action is "ask-user". Reused unmodified from
        # `pipeline/findings.py` -- see that module's own docstring.
        blocking = has_blocking_finding(output.findings)
        # Auto-fixable iff at least one finding resolves to "auto-fix" and none resolve to
        # "ask-user", mirroring `ReviewStep`'s identical two-gate rationale (see
        # `steps/review.py`).
        has_auto_fix = any(action_or_default(f.action) == "auto-fix" for f in output.findings)
        auto_fixable = has_auto_fix and not blocking

        return StepOutcome(
            needs_approval=blocking,
            auto_fixable=auto_fixable,
            findings=output,
        )
