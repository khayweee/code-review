"""Test-sufficiency prompt construction -- Milestone 6 (see docs/ROADMAP.md), issue #59.

Gets its own module, not folded into `review.py`, because the guardrail text here is
unconditional: unlike `review.py`'s `intent_conformance_clause`, nothing in this module
branches on `ctx.intent.source`. There is no per-provenance clause to keep separate from
an always-present one, so this file's only job is to assemble one fixed prompt out of a
handful of module-level constants plus the wrapped intent block.

`build_test_sufficiency_prompt` calls `wrap_intent(ctx.intent.summary, ctx.intent.source)`
itself, off `ctx.intent`, the same way `build_review_prompt` does (see `steps/AGENTS.md`'s
rule: each step's own prompt builder re-derives wrapped intent text from `ctx.intent`,
never receives it forward through a prior step's `StepOutcome`).

The four guardrail constants below exist to close specific loopholes a test-sufficiency
agent could otherwise slip through:

- `_DECISION_LADDER` states the four rungs an agent must climb, in order, for every
  changed behavior: cite an existing test, write one if none exists, fall back to
  described manual verification if a test is genuinely infeasible, or -- if none of the
  above was possible -- say so honestly via a finding rather than claim verification that
  never happened.
- `_NOT_SUFFICIENT_EVIDENCE_ALONE` heads off the shortcut of citing "the unit tests pass"
  as sufficient evidence with no reference to which behavior a given test actually
  exercises.
- `_COMPLETE_SUITE_PROHIBITION` closes two related loopholes: citing a full, unfiltered
  test-suite run as if it were targeted evidence for a specific changed behavior, and
  (the opposite failure mode) reading that prohibition as license to run nothing at all.
- `_TEST_QUALITY_RULE` rules out reading or grepping source as a substitute for actually
  executing code -- evidence must come from a run, not a source-text inspection.

Each constant is kept module-level and out-of-line, matching `review.py`'s
`_INTENT_CONFORMANCE_CLAUSE` pattern, so the exact obligation text is one grep away and
diffable on its own line when it needs to change.
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


def build_test_sufficiency_prompt(ctx: StepContext) -> str:
    """Assemble `TestSufficiencyStep`'s single prompt: the diff, then the wrapped intent
    block (`wrap_intent`, see `prompt/intent.py`), then the four guardrail clauses above,
    always in the same fixed order and always all present -- there is no conditional
    clause here the way `build_review_prompt` conditionally appends
    `intent_conformance_clause`, since none of this module's guardrail text branches on
    intent provenance.
    """

    sections = [
        f"Assess test sufficiency for this diff:\n{ctx.diff}",
        wrap_intent(ctx.intent.summary, ctx.intent.source),
        _DECISION_LADDER,
        _NOT_SUFFICIENT_EVIDENCE_ALONE,
        _COMPLETE_SUITE_PROHIBITION,
        _TEST_QUALITY_RULE,
    ]
    return "\n\n".join(sections)
