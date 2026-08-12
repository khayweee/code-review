"""Test-sufficiency prompt construction: assembles a fixed prompt from module-level
guardrail clauses plus the wrapped intent block. Separate module from `review.py` since
none of this text is conditional on intent provenance.

Guardrail clauses close specific loopholes a test-sufficiency agent could slip through:

- `_DECISION_LADDER`: for each changed behavior, cite an existing test, else write one,
  else fall back to described manual verification, else admit it's unverified. Never
  fabricate a pass.
- `_NOT_SUFFICIENT_EVIDENCE_ALONE`: "tests pass" alone doesn't count without naming which
  test covers which changed behavior.
- `_COMPLETE_SUITE_PROHIBITION`: a full/unfiltered suite run isn't targeted evidence, but
  isn't license to run nothing either.
- `_TEST_QUALITY_RULE`: evidence must come from execution, not from reading/grepping source.
- `_SUGGESTION_OBLIGATION_CLAUSE`: mirrors `review.py`'s clause (separate copy, not
  imported, by this package's no-cross-step-sharing convention).
"""

from __future__ import annotations

from code_review.pipeline.step import StepContext
from code_review.prompt.intent import wrap_intent

# --- Guardrail clause constants ----------------------------------------------------------

_DECISION_LADDER = (
    "For every behavior this diff changes, work through this decision ladder in order and "
    "stop at the first rung that applies:\n"
    "1. An existing test already exercises the changed behavior -- cite it by name and "
    "location.\n"
    "2. No existing test covers it -- write or improve a focused test, run it, and cite "
    "it by name and location.\n"
    "3. Writing a test is genuinely not feasible -- perform manual verification instead, "
    "and describe the concrete steps you took and what you observed.\n"
    "4. None of the above was possible -- say so honestly: report a finding stating the "
    "change is unverified. Never fabricate a passing test or claim verification that did "
    "not happen."
)

_NOT_SUFFICIENT_EVIDENCE_ALONE = (
    "unit tests passing is not sufficient evidence by itself -- you must be able to name "
    "which specific test exercises which specific changed behavior; a green test run with "
    "no such mapping proves nothing about this diff."
)

_COMPLETE_SUITE_PROHIBITION = (
    'Citing "ran the whole test suite and it passed" or "ran the entire test suite and it '
    'passed" is not acceptable evidence on its own -- it does not show that any specific '
    "changed behavior was exercised. Running one broad, unfiltered command that happens to "
    "cover the whole suite does not count as targeted evidence either; targeted means the "
    "test(s) you cite are ones you can point to as covering the specific changed behavior, "
    "not merely the whole codebase incidentally. This prohibition is NOT permission to run "
    "nothing: every changed behavior must still complete at least one rung of the decision "
    "ladder above."
)

_TEST_QUALITY_RULE = (
    "Evidence must come from executing code, not from inspecting it. Reading the source or "
    "grepping for a pattern is never acceptable evidence that a behavior is correct -- only "
    "an actual test run or an actual manual verification step counts."
)

_SUGGESTION_OBLIGATION_CLAUSE = (
    'For every finding whose action resolves to "ask-user" -- including one where '
    "action is left unset, null, or set to an unrecognized value, since that is exactly "
    'what resolves to "ask-user" via action_or_default -- you MUST populate that '
    "finding's suggestions with one or more concrete, actionable remediation options: "
    "what a human could actually do about it, not a restatement of the problem. Findings "
    'whose action resolves to "no-op" or "auto-fix" do not need suggestions.'
)


def build_test_sufficiency_prompt(ctx: StepContext) -> str:
    """Assemble `TestSufficiencyStep`'s prompt: diff, wrapped intent, then the five
    guardrail clauses above, always in fixed order.
    """

    sections = [
        f"Assess test sufficiency for this diff:\n{ctx.diff}",
        wrap_intent(ctx.intent.summary, ctx.intent.source),
        _DECISION_LADDER,
        _NOT_SUFFICIENT_EVIDENCE_ALONE,
        _COMPLETE_SUITE_PROHIBITION,
        _TEST_QUALITY_RULE,
        _SUGGESTION_OBLIGATION_CLAUSE,
    ]
    return "\n\n".join(sections)


# --- build_test_sufficiency_fix_prompt -----------------------------------------------------

_FIX_ROUND_INSTRUCTION = (
    "You are running a fix round on a test-sufficiency assessment you previously made. Act "
    "on every item below -- write the missing test, perform the missing manual "
    "verification, or otherwise do whatever the item calls for -- then re-run your own "
    "test-sufficiency assessment from scratch: report a fresh set of findings and a fresh "
    "tested/testing_summary/artifacts reflecting the diff as it now stands. Do not simply "
    "restate or echo the items below as your findings -- they describe what to address, "
    "not what you must report back."
)

_STALE_DIFF_WARNING = (
    "The diff below is what triggered the ORIGINAL test-sufficiency assessment, before any "
    "fix round ran -- it does not reflect edits a prior fix round may already have made to "
    "the working tree. Treat it only as background on what this change was originally "
    "about; re-inspect the live working tree yourself (e.g. run `git diff` against the same "
    "base) to see what the diff actually looks like right now, and assess that."
)


def build_test_sufficiency_fix_prompt(ctx: StepContext) -> str:
    """Assemble `TestSufficiencyStep`'s fix-mode prompt: instructs the agent to act on
    `ctx.fix_round.instructions`, then re-run the assessment from scratch and return a
    fresh `TestSufficiencyOutput` rather than echo what triggered the round.

    `ctx.diff` is captured once before the pipeline starts, so it's stale by fix rounds;
    it's included only as background and the agent is told to re-inspect the live working
    tree itself.

    Requires `ctx.fix_round is not None`.
    """

    assert ctx.fix_round is not None, (
        "build_test_sufficiency_fix_prompt requires ctx.fix_round to be set"
    )

    sections = [
        _FIX_ROUND_INSTRUCTION,
        f"Address the following:\n{ctx.fix_round.instructions}",
        f"{_STALE_DIFF_WARNING}\n\n{ctx.diff}",
        wrap_intent(ctx.intent.summary, ctx.intent.source),
        _DECISION_LADDER,
        _NOT_SUFFICIENT_EVIDENCE_ALONE,
        _COMPLETE_SUITE_PROHIBITION,
        _TEST_QUALITY_RULE,
        _SUGGESTION_OBLIGATION_CLAUSE,
    ]
    return "\n\n".join(sections)
