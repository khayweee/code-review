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
