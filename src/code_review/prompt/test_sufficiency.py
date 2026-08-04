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

`build_test_sufficiency_fix_prompt` (issue #82) is the fix-mode counterpart to
`build_test_sufficiency_prompt`: `steps/test_sufficiency.py`'s `TestSufficiencyStep.run`
calls this instead whenever `ctx.fix_round is not None`. It instructs the agent to actually
act on `ctx.fix_round.instructions` (write the missing test, perform the missing manual
verification, etc.), then re-run its own test-sufficiency assessment from scratch -- the
returned `TestSufficiencyOutput` must be a fresh verdict (new `findings`/`tested`/
`testing_summary`/`artifacts`), never an echo of what triggered the round. Mirrors
`prompt/review.py`'s `build_review_fix_prompt` shape and reasoning exactly, including its
own `ctx.diff`-staleness handling (`_STALE_DIFF_WARNING` below, a same-named but separately
defined local constant -- not imported from `prompt/review.py`, per this module's own
"no cross-step sharing" rule above), but is a standalone definition in this module rather
than an import from `prompt/review.py`, for the same reason `build_test_sufficiency_prompt`
itself is: nothing here should depend on `prompt/review.py`, or `prompt/review.py` on this
module. This module's four guardrail clauses apply to a fix round's re-assessment exactly
as they do to a normal round, so `build_test_sufficiency_fix_prompt` includes all four too.

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

A fifth, `_SUGGESTION_OBLIGATION_CLAUSE` (issue #76, part of #75), obligates the agent to
populate `Finding.suggestions` (`pipeline/findings.py`) with concrete remediation options
for every finding whose `action` resolves to `"ask-user"`. It is unconditional, matching
this module's other four clauses, and is a separately-defined local constant rather than
an import from `prompt/review.py` -- same wording, same obligation, but per this module's
"no cross-step sharing" rule (see above), each module owns its own copy.
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

# Mirrors `prompt/review.py`'s `_SUGGESTION_OBLIGATION_CLAUSE` in wording -- a same-named,
# separately defined local constant, not an import (see this module's own docstring for
# why `prompt/test_sufficiency.py` keeps no cross-step sharing with `prompt/review.py`).
# Unconditional, matching this module's other four guardrail clauses above.
_SUGGESTION_OBLIGATION_CLAUSE = (
    'For every finding whose action resolves to "ask-user" -- including one where '
    "action is left unset, null, or set to an unrecognized value, since that is exactly "
    'what resolves to "ask-user" via action_or_default -- you MUST populate that '
    "finding's suggestions with one or more concrete, actionable remediation options: "
    "what a human could actually do about it, not a restatement of the problem. Findings "
    'whose action resolves to "no-op" or "auto-fix" do not need suggestions.'
)


def build_test_sufficiency_prompt(ctx: StepContext) -> str:
    """Assemble `TestSufficiencyStep`'s single prompt: the diff, then the wrapped intent
    block (`wrap_intent`, see `prompt/intent.py`), then the five guardrail clauses above,
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
        _SUGGESTION_OBLIGATION_CLAUSE,
    ]
    return "\n\n".join(sections)


# --- build_test_sufficiency_fix_prompt (issue #82) ----------------------------------------

# Kept as a module-level constant, mirroring `prompt/review.py`'s `_FIX_ROUND_INSTRUCTION`,
# so the exact re-assessment instruction is one grep away and diffable on its own line when
# it needs to change.
_FIX_ROUND_INSTRUCTION = (
    "You are running a fix round on a test-sufficiency assessment you previously made. Act "
    "on every item below -- write the missing test, perform the missing manual "
    "verification, or otherwise do whatever the item calls for -- then re-run your own "
    "test-sufficiency assessment from scratch: report a fresh set of findings and a fresh "
    "tested/testing_summary/artifacts reflecting the diff as it now stands. Do not simply "
    "restate or echo the items below as your findings -- they describe what to address, "
    "not what you must report back."
)

# Mirrors `prompt/review.py`'s `_STALE_DIFF_WARNING` in reasoning and wording -- a
# same-named, separately defined local constant, not an import (see this module's own
# docstring for why `prompt/test_sufficiency.py` keeps no cross-step sharing with
# `prompt/review.py`, per issue #58's Implementation Decisions).
_STALE_DIFF_WARNING = (
    "The diff below is what triggered the ORIGINAL test-sufficiency assessment, before any "
    "fix round ran -- it does not reflect edits a prior fix round may already have made to "
    "the working tree. Treat it only as background on what this change was originally "
    "about; re-inspect the live working tree yourself (e.g. run `git diff` against the same "
    "base) to see what the diff actually looks like right now, and assess that."
)


def build_test_sufficiency_fix_prompt(ctx: StepContext) -> str:
    """Assemble `TestSufficiencyStep`'s fix-mode prompt (issue #82): instructs the agent to
    act on `ctx.fix_round.instructions`, then re-run its own test-sufficiency assessment
    and return a fresh `TestSufficiencyOutput` -- new findings/tested/testing_summary/
    artifacts, never an unchanged echo of what triggered the round.

    Requires `ctx.fix_round is not None`; the caller (`steps/test_sufficiency.py`'s
    `TestSufficiencyStep.run`) is responsible for choosing this function over
    `build_test_sufficiency_prompt` based on exactly that check.

    **Why the live working tree, not `ctx.diff`**: same reasoning as
    `prompt/review.py`'s `build_review_fix_prompt` -- `ctx.diff` is computed once, before
    the pipeline starts, from a `git diff` against a base ref, and does not reflect edits a
    fix round's own agent call makes to the working tree in a later round. A fix-mode
    prompt that treated `ctx.diff` as ground truth for "what to re-assess" would silently
    assess a stale snapshot from before its own edits, on every round after the first.
    Instead, `ctx.diff` is included only as originating context (`_STALE_DIFF_WARNING`
    above makes this explicit, by name) and the agent is told to re-inspect the live
    working tree itself -- it already has full tool/shell access via `RunOpts`'s existing
    permission defaults (see `steps/test_sufficiency.py`'s `TestSufficiencyStep.run`), so
    no `RunOpts` change is needed here, only this prompt's wording.

    Section order: the fix instruction first (what to do), then the stale-diff-warned
    original diff (background), then the wrapped intent block, then all five guardrail
    clauses (unchanged from `build_test_sufficiency_prompt`, since a fix round's
    re-assessment must honor the same guardrails as any other round) -- fix instructions
    lead so the agent knows it is acting, not merely reading, before it reaches the diff.
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
