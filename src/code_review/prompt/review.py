"""Review-step prompt construction: the intent-conformance clause and the prompt assembly
functions. `steps/review.py` keeps the schema and orchestration; this module only builds
prompt text.
"""

from __future__ import annotations

from code_review.pipeline.step import StepContext
from code_review.prompt.intent import wrap_intent

# --- intent_conformance_clause ----------------------------------------------------------

_INTENT_CONFORMANCE_CLAUSE = (
    "Intent conformance is mandatory. For any diff hunk that removes or contradicts a "
    "criterion the user's intent marks REQUIRED, or that adds a behavior the user's "
    'intent marks FORBIDDEN, you MUST report a finding with action "ask-user" for that '
    "hunk -- even if every other aspect of the change is otherwise risk-clean. A "
    "risk-clean diff is not a substitute for conforming to explicit, authoritative "
    "intent."
)


# --- suggestion_obligation_clause -------------------------------------------------------

# Unconditional (unlike _INTENT_CONFORMANCE_CLAUSE): appended directly, not source-gated.
_SUGGESTION_OBLIGATION_CLAUSE = (
    'For every finding whose action resolves to "ask-user" -- including one where '
    "action is left unset, null, or set to an unrecognized value, since that is exactly "
    'what resolves to "ask-user" via action_or_default -- you MUST populate that '
    "finding's suggestions with one or more concrete, actionable remediation options: "
    "what a human could actually do about it, not a restatement of the problem. Findings "
    'whose action resolves to "no-op" or "auto-fix" do not need suggestions.'
)


def intent_conformance_clause(source: str) -> str:
    """Return the prompt clause obligating an `ask-user` finding on intent violations, or
    `""` when `source` is not `"explicit"`.
    """

    if source != "explicit":
        return ""
    return _INTENT_CONFORMANCE_CLAUSE


# --- build_review_prompt -----------------------------------------------------------------


def build_review_prompt(ctx: StepContext) -> str:
    """Assemble `ReviewStep`'s prompt: diff, wrapped intent, intent-conformance clause (if
    non-empty), then the suggestion-obligation clause.
    """

    sections = [
        f"Review this diff:\n{ctx.diff}",
        wrap_intent(ctx.intent.summary, ctx.intent.source),
    ]

    clause = intent_conformance_clause(ctx.intent.source)
    if clause:
        sections.append(clause)

    sections.append(_SUGGESTION_OBLIGATION_CLAUSE)

    return "\n\n".join(sections)


# --- build_review_fix_prompt --------------------------------------------------------------

_FIX_ROUND_INSTRUCTION = (
    "You are running a fix round on a change you previously reviewed. Edit the affected "
    "files in the current working tree to address every item below, then re-review your "
    "own result from scratch: report a fresh set of findings and a fresh risk_level/"
    "risk_rationale reflecting the diff as it now stands. Do not simply restate or echo "
    "the items below as your findings -- they describe what to fix, not what you must "
    "report back."
)

_STALE_DIFF_WARNING = (
    "The diff below is what triggered the ORIGINAL review, before any fix round ran -- it "
    "does not reflect edits a prior fix round may already have made to the working tree. "
    "Treat it only as background on what this change was originally about; re-inspect the "
    "live working tree yourself (e.g. run `git diff` against the same base) to see what the "
    "diff actually looks like right now, and review that."
)


def build_review_fix_prompt(ctx: StepContext) -> str:
    """Assemble `ReviewStep`'s fix-mode prompt: instructs the agent to edit the affected
    files to address `ctx.fix_round.instructions`, then re-review from scratch and return a
    fresh `ReviewOutput` rather than echo the findings that triggered the round.

    `ctx.diff` was captured once before the pipeline started, so a fix round's own edits
    make it stale; it's included only as background context and the agent is told to
    re-inspect the live working tree itself instead of trusting that string.

    Requires `ctx.fix_round is not None`.
    """

    assert ctx.fix_round is not None, "build_review_fix_prompt requires ctx.fix_round to be set"

    sections = [
        _FIX_ROUND_INSTRUCTION,
        f"Address the following:\n{ctx.fix_round.instructions}",
        f"{_STALE_DIFF_WARNING}\n\n{ctx.diff}",
        wrap_intent(ctx.intent.summary, ctx.intent.source),
    ]

    clause = intent_conformance_clause(ctx.intent.source)
    if clause:
        sections.append(clause)

    sections.append(_SUGGESTION_OBLIGATION_CLAUSE)

    return "\n\n".join(sections)
