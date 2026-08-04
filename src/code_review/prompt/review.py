"""Review-step prompt construction: the intent-conformance clause and the single-prompt
assembly function -- moved here from `steps/review.py` in a later structural refactor to
separate prompt-construction logic from step-orchestration logic. `steps/review.py` keeps
only `ReviewOutput` (the schema) and `ReviewStep` (orchestration).

`intent_conformance_clause` exists because Milestone 3 (explicit intent) landed before
Milestone 5: once a user has supplied authoritative acceptance criteria via `--intent`,
the review prompt must obligate the agent to flag a change that removes/contradicts a
REQUIRED criterion or adds a FORBIDDEN behavior as `ask-user`, even when the change is
otherwise risk-clean -- an intent violation is a distinct failure mode from an ordinary
correctness or risk finding, and a risk-clean diff must not be able to slip past it. The
clause only ever appears when the caller's `source == "explicit"` (see `prompt/intent.py`'s
provenance rule: authority changes, sanitization never does -- this clause is itself part
of "authority", so it is withheld for inferred/hinted intent, never partially applied).
It takes a bare `source: str` rather than the whole `Intent` object (`steps/intent.py`) so
this leaf package has no dependency on `steps/` at all -- it only ever branches on that one
field.

`build_review_fix_prompt` (issue #81) is the fix-mode counterpart to `build_review_prompt`:
`steps/review.py`'s `ReviewStep.run` calls this instead whenever `ctx.fix_round is not
None`. It instructs the agent to actually edit the affected files to address
`ctx.fix_round.instructions`, then re-review its own result from scratch -- the returned
`ReviewOutput` must be a fresh verdict (new findings, new risk level/rationale), never an
echo of the findings that triggered the round. `ctx.diff` was captured once, before the
pipeline started, and does not reflect edits a fix round itself makes to the working tree
in a later round -- see this function's own docstring for how it resolves that staleness:
it hands the agent the original diff as originating context only, and explicitly tells it
to re-inspect the live working tree (the agent already has full tool/shell access via the
existing `RunOpts` permission defaults -- no `RunOpts` change was needed for this, only
this prompt's wording).
"""

from __future__ import annotations

from code_review.pipeline.step import StepContext
from code_review.prompt.intent import wrap_intent

# --- intent_conformance_clause ----------------------------------------------------------

# Kept as a module-level constant, not inlined into the function, so the exact obligation
# text is one grep away and diffable on its own line when it needs to change.
_INTENT_CONFORMANCE_CLAUSE = (
    "Intent conformance is mandatory. For any diff hunk that removes or contradicts a "
    "criterion the user's intent marks REQUIRED, or that adds a behavior the user's "
    'intent marks FORBIDDEN, you MUST report a finding with action "ask-user" for that '
    "hunk -- even if every other aspect of the change is otherwise risk-clean. A "
    "risk-clean diff is not a substitute for conforming to explicit, authoritative "
    "intent."
)


def intent_conformance_clause(source: str) -> str:
    """Return the prompt clause obligating an `ask-user` finding on intent violations, or
    `""` when `source` is not `"explicit"`.

    Only returns non-empty text when `source == "explicit"` -- mirroring `wrap_intent`'s
    provenance rule (`prompt/intent.py`): explicit intent is treated as authoritative
    acceptance criteria, inferred/hinted intent is not, and this clause is part of that
    authority, so it must not appear for non-explicit provenance. The caller
    (`build_review_prompt` below) is expected to append this to its prompt alongside
    `wrap_intent(intent.summary, intent.source)`; nothing in this function itself embeds
    intent summary text or calls `wrap_intent`.
    """

    if source != "explicit":
        return ""
    return _INTENT_CONFORMANCE_CLAUSE


# --- build_review_prompt -----------------------------------------------------------------


def build_review_prompt(ctx: StepContext) -> str:
    """Assemble `ReviewStep`'s single prompt: the diff, then the wrapped intent block
    (`wrap_intent`, see `prompt/intent.py`), then the intent-conformance clause appended
    only when non-empty (see `intent_conformance_clause` above). Diff first, so the agent
    reads what changed before what it is being held to -- mirroring `ReviewOutput`'s own
    findings-before-risk field ordering rationale.
    """

    sections = [
        f"Review this diff:\n{ctx.diff}",
        wrap_intent(ctx.intent.summary, ctx.intent.source),
    ]

    clause = intent_conformance_clause(ctx.intent.source)
    if clause:
        sections.append(clause)

    return "\n\n".join(sections)


# --- build_review_fix_prompt (issue #81) --------------------------------------------------

# Kept as a module-level constant, mirroring `_INTENT_CONFORMANCE_CLAUSE` above, so the
# exact re-review instruction is one grep away and diffable on its own line when it needs
# to change.
_FIX_ROUND_INSTRUCTION = (
    "You are running a fix round on a change you previously reviewed. Edit the affected "
    "files in the current working tree to address every item below, then re-review your "
    "own result from scratch: report a fresh set of findings and a fresh risk_level/"
    "risk_rationale reflecting the diff as it now stands. Do not simply restate or echo "
    "the items below as your findings -- they describe what to fix, not what you must "
    "report back."
)

# See `build_review_fix_prompt`'s own docstring for why this warns against `ctx.diff`
# specifically, rather than silently including it as if it were still current.
_STALE_DIFF_WARNING = (
    "The diff below is what triggered the ORIGINAL review, before any fix round ran -- it "
    "does not reflect edits a prior fix round may already have made to the working tree. "
    "Treat it only as background on what this change was originally about; re-inspect the "
    "live working tree yourself (e.g. run `git diff` against the same base) to see what the "
    "diff actually looks like right now, and review that."
)


def build_review_fix_prompt(ctx: StepContext) -> str:
    """Assemble `ReviewStep`'s fix-mode prompt (issue #81): instructs the agent to edit the
    affected files to address `ctx.fix_round.instructions`, then re-review its own result
    and return a fresh `ReviewOutput` -- new findings, new risk level/rationale, never an
    unchanged echo of what triggered the round.

    Requires `ctx.fix_round is not None`; the caller (`steps/review.py`'s `ReviewStep.run`)
    is responsible for choosing this function over `build_review_prompt` based on exactly
    that check.

    **Why the live working tree, not `ctx.diff`**: `ctx.diff` is computed once, before the
    pipeline starts, from a `git diff` against a base ref (see `cli.py`'s
    `_diff_against_head`) -- a fix round's own edits (made via the agent's normal tool/shell
    access, not by this module) change the working tree without ever updating that string.
    A fix-mode prompt that treated `ctx.diff` as ground truth for "what to re-review" would
    silently review a stale snapshot from before its own edits, on every round after the
    first. Instead, `ctx.diff` is included only as originating context (`_STALE_DIFF_WARNING`
    above makes this explicit, by name, rather than silently including it as if it were
    still current) and the agent is told to re-inspect the live working tree itself -- it
    already has full tool/shell access via `RunOpts`'s existing permission defaults (see
    `steps/review.py`'s `ReviewStep.run`), so no `RunOpts` change is needed here, only this
    prompt's wording.

    Section order: the fix instruction first (what to do), then the stale-diff-warned
    original diff (background), then the wrapped intent block and intent-conformance clause
    (unchanged from `build_review_prompt`, since a fix round must honor the same intent
    obligations as any other round) -- fix instructions lead so the agent knows it is
    editing, not merely reading, before it reaches the diff.
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

    return "\n\n".join(sections)
