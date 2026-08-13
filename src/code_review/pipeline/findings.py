"""Finding schema, the fail-safe action default, the blocking-findings gate, and the
deterministic pipeline-owned-delivery scope filter.

`Finding` is a pydantic `BaseModel`, not this repo's usual frozen/slotted dataclass,
because it's passed as `RunOpts.output_schema` to an `Agent.run` call and validated via
`agent/schema.py`'s `validate_output`, both of which require a pydantic model.

Fail-safe default (see docs/GLOSSARY.md): an unset, null, or unrecognized `action` must
resolve to `ask-user`, never `no-op`/`auto-fix` -- getting this backwards is a fail-open
hole. `Finding.action` is deliberately `str | None`, not a closed `Literal`: a `Literal`
field would reject an unrecognized action at validation time, but the fail-safe default
requires accepting it and routing it to a human instead. `action_or_default` resolves an
`action` to one of the three known outcomes; `has_blocking_finding` (see docs/GLOSSARY.md's
"Blocking finding") turns a list of findings into a single yes/no "needs a human" answer.
Both are pure.

`filter_pipeline_owned_delivery_findings` strips pipeline-owned-delivery-scoped findings
from a `ReviewStep` answer and resets `risk_level` to `"low"` when that was the sole basis
for an elevated verdict -- see its own docstring for the exact rule and limits.

`Finding.suggestions` defaults to `[]`, populated by the producing step's prompt, never
enforced at the schema level: a validator that rejected a missing-suggestions response
would fail schema validation exactly when a human's judgement is needed most. An
`ask-user` finding with empty `suggestions` is valid, just less useful.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel

# Top-level import (not TYPE_CHECKING-gated): findings.py and step.py both live in
# pipeline/, and step.py never imports findings.py, so no cycle here.
from code_review.pipeline.step import ApprovalResponse

if TYPE_CHECKING:
    # steps/ depends on pipeline/, never the reverse; ReviewOutput/TestSufficiencyOutput/
    # Intent/PullRequestOutcome live under steps/, so a top-level import would invert that.
    # Lazy annotations make TYPE_CHECKING-only sufficient.
    from code_review.steps.intent import Intent
    from code_review.steps.pr import PullRequestOutcome
    from code_review.steps.review import ReviewOutput
    from code_review.steps.test_sufficiency import TestSufficiencyOutput

# The three outcomes action_or_default ever returns. Finding.action itself is typed more
# loosely (see module docstring).
Action = Literal["no-op", "auto-fix", "ask-user"]

# Fail-safe default every unset/null/unrecognized action resolves to.
DEFAULT_ACTION: Action = "ask-user"

_VALID_ACTIONS: frozenset[str] = frozenset({"no-op", "auto-fix", "ask-user"})


class Finding(BaseModel):
    """One structured observation from a review-family step: what is wrong, how serious,
    what should happen about it, and which delivery scope it belongs to (see
    docs/GLOSSARY.md's "Finding"). Shared across review and test-sufficiency steps.
    """

    severity: Literal["error", "warning", "info"]

    description: str

    # What should happen about this finding, in {"no-op", "auto-fix", "ask-user"}.
    # str | None, not the closed Action Literal, so a missing/null/unrecognized value still
    # validates and can be resolved by action_or_default's fail-safe default instead of
    # failing schema validation.
    action: str | None = None

    # Delivery scope: "source" is author-written code, "pipeline-owned-delivery" is content
    # the pipeline itself generates (see steps/review.py's scope filter). "external-delivery"
    # is reserved, no current producer or consumer.
    review_scope: Literal["source", "pipeline-owned-delivery", "external-delivery"]

    # File/line locator, e.g. "steps/review.py:42", or None when there's no specific
    # location. No current producer sets this.
    location: str | None = None

    # Agent-proposed remediation options, populated by the producing step's prompt, not
    # enforced at the schema level (see module docstring).
    suggestions: list[str] = []


def action_or_default(action: str | None) -> Action:
    """Resolve `action` to itself if it's one of the three known values, else to the
    fail-safe default `ask-user` (see module docstring).
    """

    if action in _VALID_ACTIONS:
        # mypy can't infer membership in _VALID_ACTIONS implies Action's literal values.
        return action  # type: ignore[return-value]
    return DEFAULT_ACTION


def has_blocking_finding(findings: list[Finding]) -> bool:
    """True if any finding, after `action_or_default`, resolves to `ask-user` (see
    docs/GLOSSARY.md's "Blocking finding").
    """

    return any(action_or_default(finding.action) == "ask-user" for finding in findings)


# --- Deterministic pipeline-owned-delivery scope filter ---------------------------------

# Severities that can justify an elevated (medium/high) risk_level; "info" alone never does.
_RISK_SUPPORTING_SEVERITIES: frozenset[str] = frozenset({"error", "warning"})

_RESET_RATIONALE = (
    'risk_level reset to "low" by the deterministic pipeline-owned-delivery scope '
    "filter: every error/warning-severity finding that could have justified the prior "
    'risk_level was scoped "pipeline-owned-delivery" and has been removed, and no '
    "remaining finding is severe enough to justify an elevated risk level on its own."
)


def filter_pipeline_owned_delivery_findings(output: ReviewOutput) -> ReviewOutput:
    """Strip `pipeline-owned-delivery`-scoped findings from `output` and return a new
    `ReviewOutput` (pure; `output` is left untouched).

    Also resets `risk_level` to `"low"` and overwrites `risk_rationale` when, after
    stripping, no surviving finding is `"error"`/`"warning"` severity -- meaning every
    finding that could have justified the elevated level is now gone. Otherwise the risk
    verdict is left untouched.

    This is a severity-based heuristic, not a per-finding causal link: a `risk_level` the
    agent elevated on narrative judgement alone, with no error/warning finding backing it,
    would also get reset by this rule.

    Defined here rather than `steps/` (where `ReviewOutput` lives) to keep it alongside its
    `Finding`-processing siblings without inverting the `steps/` depends on `pipeline/`
    invariant; the annotation is `TYPE_CHECKING`-only and the body uses only duck-typed
    attribute access.
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


def describe_auto_fix_findings(
    payload: list[Finding] | ReviewOutput | TestSufficiencyOutput | Intent | PullRequestOutcome,
) -> str:
    """Render every finding in `payload` whose resolved action is "auto-fix" as fix-round
    instructions text (`pipeline.step.FixRound.instructions`).

    `payload` is `StepOutcome.payload`: a bare `list[Finding]`, a
    `ReviewOutput`/`TestSufficiencyOutput` with a `.findings` list, or an `Intent`/
    `PullRequestOutcome` (no findings at all -- neither `IntentStep` nor `PRStep` opts into
    the fix-round loop, so this case yields no lines rather than raising).

    `ReviewOutput`/`TestSufficiencyOutput` are `TYPE_CHECKING`-only imports here (`steps/`
    depends on `pipeline/`, never the reverse), so the `ReviewOutput`/`TestSufficiencyOutput`
    branch is duck-typed via `getattr` rather than `isinstance` -- the two accessors are
    otherwise indistinguishable at this layer.

    One line per matching finding, in list order: `"- [severity] description (location)"`,
    omitting `(location)` when `None`. Free-form text with no schema contract -- it becomes
    prompt text, not something other code parses back.
    """

    raw = payload if isinstance(payload, list) else getattr(payload, "findings", None)
    if raw is None:
        return ""

    lines = []
    for finding in raw:
        if action_or_default(finding.action) != "auto-fix":
            continue
        location = f" ({finding.location})" if finding.location else ""
        lines.append(f"- [{finding.severity}] {finding.description}{location}")

    return "\n".join(lines)


def describe_finding_decisions(decisions: list[tuple[Finding, ApprovalResponse]]) -> str:
    """Render every `"fix"`-decided finding in `decisions` as combined fix-round
    instructions text, for `tui.widgets.FindingsList`'s per-finding approval-park
    aggregation (`FindingsList._resolve_park` combines per-row decisions into the single
    `ApprovalResponse` that resolves the park).

    `decisions` accepts both `"fix"`- and `"skip"`-decided rows unfiltered; a `"skip"`
    contributes no line.

    One line per `"fix"`-decided finding, in order:
    `"- [severity] description (location): <human's instructions>"`, reusing
    `describe_auto_fix_findings`'s prefix format with a trailing `": <instructions>"`.
    """

    lines = []
    for finding, response in decisions:
        if response.decision != "fix":
            continue
        location = f" ({finding.location})" if finding.location else ""
        instructions = response.instructions or ""
        lines.append(f"- [{finding.severity}] {finding.description}{location}: {instructions}")

    return "\n".join(lines)
