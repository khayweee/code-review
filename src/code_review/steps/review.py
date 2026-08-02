"""Correctness/alignment review + risk assessment schema, intent-conformance clause, and
deterministic scope filter -- Milestone 5 (see docs/ROADMAP.md), sliced as issue #26.

This module also builds `ReviewStep` (issue #27, unblocked by #26 landing above): the
class that actually calls `ctx.agent.run`, wires `wrap_intent`, and applies the scope
filter below to the parsed answer before reporting a `StepOutcome`. `ReviewStep` is
deliberately NOT added to `steps/registry.py`'s `IMPLEMENTED_STEPS` or wired into
`cli.py`'s step list here -- issue #27 is scoped to proving the class directly, the same
way `tests/pipeline/test_executor.py`'s `_ReviewStep`/`_OrderStep` are proven without ever
being registered anywhere; that wiring is a later ticket.

Single prompt, single schema: `ReviewOutput` carries findings *and* the required
`risk_level`/`risk_rationale` fields together (see docs/GLOSSARY.md's "Risk verdict") --
risk is not a separate step, and a required schema field means the agent structurally
cannot return a review without a risk assessment, the way prompt wording alone cannot
guarantee (see root AGENTS.md's design invariants).

`intent_conformance_clause` exists because Milestone 3 (explicit intent) landed before
Milestone 5: once a user has supplied authoritative acceptance criteria via `--intent`,
the review prompt must obligate the agent to flag a change that removes/contradicts a
REQUIRED criterion or adds a FORBIDDEN behavior as `ask-user`, even when the change is
otherwise risk-clean -- an intent violation is a distinct failure mode from an ordinary
correctness or risk finding, and a risk-clean diff must not be able to slip past it. The
clause only ever appears when `intent.source == "explicit"` (see `steps/intent.py`'s
provenance rule: authority changes, sanitization never does -- this clause is itself part
of "authority", so it is withheld for inferred/hinted intent, never partially applied).

The deterministic scope filter exists because `Finding.review_scope ==
"pipeline-owned-delivery"` (see `pipeline/findings.py`) marks content this pipeline itself
generates or manages, not the author's source -- a review that lets a pipeline-owned
finding alone drive `risk_level` into "medium"/"high" would misattribute risk to the
change under review. `filter_pipeline_owned_delivery_findings` strips those findings after
the agent call returns and resets `risk_level` back to "low" (overwriting, not appending
to, `risk_rationale`) exactly when no surviving finding still supports an elevated risk
level -- see the function's own docstring for the exact rule and its documented limits.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from code_review.agent import RunOpts
from code_review.pipeline.findings import Finding, action_or_default, has_blocking_finding
from code_review.pipeline.step import Step, StepContext, StepOutcome
from code_review.steps.intent import Intent, wrap_intent

# --- ReviewOutput ----------------------------------------------------------------------


class ReviewOutput(BaseModel):
    """The Review step's schema: correctness findings plus a required risk verdict on
    the same object (see docs/GLOSSARY.md's "Risk verdict"). Field order is deliberate --
    `findings` first, then the risk fields -- mirroring the reference implementation's
    chain-of-thought ordering: an agent reasons about what is wrong before it summarizes
    overall risk, so the schema asks for findings first for the same reason.
    """

    # The review's structured observations (see `pipeline/findings.py`'s `Finding`).
    # Consumer: `filter_pipeline_owned_delivery_findings` (this module) strips
    # pipeline-owned-delivery-scoped entries; `pipeline/findings.py`'s
    # `has_blocking_finding` (Milestone 7's fix/approval loop) decides whether the run
    # needs a human from this list.
    findings: list[Finding]

    # Overall risk level of the change, required so the agent cannot return a review
    # without a risk verdict (see docs/GLOSSARY.md's "Risk verdict"). Consumer: Milestone
    # 7's fix/approval loop and, once filtering resets it, this module's own scope filter.
    risk_level: Literal["low", "medium", "high"]

    # Written justification for `risk_level`, required for the same reason `risk_level`
    # is. Consumer: rendered alongside the risk-flavored PR body line (Milestone 8);
    # overwritten (not appended to) by `filter_pipeline_owned_delivery_findings` when it
    # resets `risk_level`, so the rationale always describes the risk level actually
    # returned, never a stale explanation for a level that no longer holds.
    risk_rationale: str

    # Which delivery scope the agent judges its own risk assessment to be about. Only the
    # enum shape is built here -- no code in this module reads this field yet (the scope
    # filter below reasons from `Finding.review_scope`/`Finding.severity` instead, not
    # from this field). "source-or-external" covers the common case of author-written or
    # externally-delivered changes; "pipeline-owned-delivery" is a reserved value for
    # future use once something consumes it. Optional (defaults to `None`) because,
    # unlike `risk_level`/`risk_rationale`, no pipeline decision depends on it being
    # present yet (see docs/GLOSSARY.md's "Risk verdict", which defines the required risk
    # verdict as level plus rationale only).
    risk_scope: Literal["source-or-external", "pipeline-owned-delivery"] | None = None


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


def intent_conformance_clause(intent: Intent) -> str:
    """Return the prompt clause obligating an `ask-user` finding on intent violations, or
    `""` when `intent` is not authoritative.

    Only returns non-empty text when `intent.source == "explicit"` -- mirroring
    `wrap_intent`'s provenance rule (`steps/intent.py`): explicit intent is treated as
    authoritative acceptance criteria, inferred/hinted intent is not, and this clause is
    part of that authority, so it must not appear for non-explicit provenance. The caller
    (`ReviewStep`, issue #27) is expected to append this to its prompt alongside
    `wrap_intent(intent.summary, intent.source)`; nothing in this function itself embeds
    `intent.summary` or calls `wrap_intent`.
    """

    if intent.source != "explicit":
        return ""
    return _INTENT_CONFORMANCE_CLAUSE


# --- Deterministic pipeline-owned-delivery scope filter ---------------------------------

# Severities treated as capable of justifying an elevated (`medium`/`high`) `risk_level`.
# "info" findings are never, on their own, treated as elevating risk -- see the filter's
# docstring for what this assumption does and does not cover.
_RISK_SUPPORTING_SEVERITIES: frozenset[str] = frozenset({"error", "warning"})

_RESET_RATIONALE = (
    'risk_level reset to "low" by the deterministic pipeline-owned-delivery scope '
    "filter: every error/warning-severity finding that could have justified the prior "
    'risk_level was scoped "pipeline-owned-delivery" and has been removed, and no '
    "remaining finding is severe enough to justify an elevated risk level on its own."
)


def filter_pipeline_owned_delivery_findings(output: ReviewOutput) -> ReviewOutput:
    """Strip `pipeline-owned-delivery`-scoped findings from `output` and return a new
    `ReviewOutput` (pure function; `output` itself is left untouched).

    Also resets `risk_level` to `"low"` and overwrites `risk_rationale` (never appends to
    it, so the rationale always matches the level actually returned) when, after
    stripping, no surviving finding is at `"error"` or `"warning"` severity -- meaning
    every finding that could have justified an elevated `risk_level` was itself
    pipeline-owned-delivery-scoped and is now gone. If a `"source"`- (or
    `"external-delivery"`-) scoped finding at `"error"`/`"warning"` severity survives
    filtering, or `risk_level` was already `"low"`, the risk verdict is left untouched.

    This is a severity-based heuristic, not a per-finding causal link: it assumes an
    elevated `risk_level` is always attributable to at least one surviving
    error/warning-severity finding. A `risk_level` the agent elevated on narrative
    judgement alone, with no error/warning finding backing it, would also be reset by
    this rule -- that case is out of scope for this filter (there is no field connecting
    a risk verdict to the specific findings that justify it) and is not exercised by this
    ticket's regression tests.
    """

    remaining = [f for f in output.findings if f.review_scope != "pipeline-owned-delivery"]

    still_supported = any(f.severity in _RISK_SUPPORTING_SEVERITIES for f in remaining)
    if output.risk_level != "low" and not still_supported:
        return output.model_copy(
            update={
                "findings": remaining,
                "risk_level": "low",
                "risk_rationale": _RESET_RATIONALE,
            }
        )

    return output.model_copy(update={"findings": remaining})


# --- ReviewStep --------------------------------------------------------------------------


def _build_prompt(ctx: StepContext) -> str:
    """Assemble `ReviewStep`'s single prompt: the diff, then the wrapped intent block
    (`wrap_intent`, see `steps/intent.py`), then the intent-conformance clause appended
    only when non-empty (see `intent_conformance_clause` above). Diff first, so the agent
    reads what changed before what it is being held to -- mirroring `ReviewOutput`'s own
    findings-before-risk field ordering rationale.
    """

    sections = [
        f"Review this diff:\n{ctx.diff}",
        wrap_intent(ctx.intent.summary, ctx.intent.source),
    ]

    clause = intent_conformance_clause(ctx.intent)
    if clause:
        sections.append(clause)

    return "\n\n".join(sections)


@dataclass(frozen=True, slots=True)
class ReviewStep(Step):
    """Milestone 5's single-pass correctness/alignment review and risk assessment (issue
    #27): one prompt built by `_build_prompt`, exactly one `ctx.agent.run` call against
    `ReviewOutput`, and the deterministic pipeline-owned-delivery scope filter applied to
    the parsed answer before it becomes this step's `StepOutcome`.

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
                prompt=_build_prompt(ctx),
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
