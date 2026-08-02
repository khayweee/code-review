"""Finding schema, the fail-safe action default, and the blocking-findings gate --
Milestone 5 (schema, issue #26). Milestone 7 (fix-loop helpers such as override-merging)
is out of scope for this module today; nothing here builds a fix/approval state machine.

`Finding` is a pydantic `BaseModel`, not this repo's usual frozen/slotted dataclass (see
`steps/intent.py`'s `Intent`), because it is passed as `RunOpts.output_schema` to an
`Agent.run` call (see `agent/base.py`) and validated via `agent/schema.py`'s
`validate_output` -- both require a pydantic model, not a dataclass.

The fail-safe default (see docs/GLOSSARY.md) is the single most important rule in this
module: an unset, null, or unrecognized `action` must resolve to `ask-user`, never to
`no-op` or `auto-fix`. Getting this backwards is a fail-open hole -- the pipeline would
silently proceed precisely in the cases it understood least. `Finding.action` is
deliberately typed as an open `str | None` rather than a closed `Literal`, mirroring
`Intent.source` in `steps/intent.py`: a pydantic `Literal` field would *reject* an
unrecognized action at validation time, but the fail-safe default requires accepting it and
routing it toward a human instead, so validation must not fail on it. `action_or_default`
is the one place that resolves an `action` value to one of the three known outcomes;
`has_blocking_finding` (the shared blocking-findings gate, reused as-is by Milestone 6's
test-sufficiency step, see docs/GLOSSARY.md's "Blocking finding") is the one place that
turns a list of findings into a single yes/no "does this run need a human" answer. Both
are pure functions with no agent or subprocess dependency.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

# The three outcomes `action_or_default` ever returns. `Finding.action` itself is typed
# more loosely (see module docstring) so this alias is only used for return/parameter
# types on the resolving helpers below, never as the field type on `Finding`.
Action = Literal["no-op", "auto-fix", "ask-user"]

# The fail-safe default every unset/null/unrecognized `action` resolves to (see
# docs/GLOSSARY.md's "Fail-safe default"). Named as a constant, not inlined, so a future
# reader searching for "ask-user" finds the one place this rule is encoded.
DEFAULT_ACTION: Action = "ask-user"

_VALID_ACTIONS: frozenset[str] = frozenset({"no-op", "auto-fix", "ask-user"})


class Finding(BaseModel):
    """One structured observation from a review-family step: what is wrong, how serious,
    what should happen about it, and which delivery scope it belongs to (see
    docs/GLOSSARY.md's "Finding"). Shared across Milestone 5 (Review) and Milestone 6
    (test sufficiency) so "what a finding looks like" has one answer.
    """

    # How serious this observation is. Consumer: a step's own prompt/logic; no code in
    # this module branches on it, `action_or_default`/`has_blocking_finding` look only at
    # `action`.
    severity: Literal["error", "warning", "info"]

    # Human-readable explanation of what is wrong and where. Consumer: rendered to the
    # user (TUI findings display, Milestone 13 issue #42; PR body, Milestone 8).
    description: str

    # What should happen about this finding, in {"no-op", "auto-fix", "ask-user"}.
    # Deliberately `str | None` rather than the closed `Action` Literal: an agent
    # response with this field missing, null, or set to an unrecognized string must
    # still validate successfully so the fail-safe default in `action_or_default` can
    # resolve it to "ask-user" rather than the whole response failing schema validation
    # over exactly the case where a human's judgement is needed most. Consumers:
    # `action_or_default` (this module) and, once built, Milestone 7's fix/approval loop.
    action: str | None = None

    # Which delivery scope this finding concerns: "source" is author-written code,
    # "pipeline-owned-delivery" is content this pipeline itself generates or manages
    # (see `steps/review.py`'s scope filter, the only current consumer of this field
    # today). "external-delivery" is a reserved value with no current producer or
    # consumer -- this project has no separate Push/CI pipeline step (see parent issue
    # #25's Implementation Decisions) -- kept only so the enum shape does not need to
    # change if one is ever added.
    review_scope: Literal["source", "pipeline-owned-delivery", "external-delivery"]

    # Human-readable file/line locator, e.g. "steps/review.py:42", or `None` when a
    # finding has no specific location to point at. Optional and defaults to `None` so
    # this field is backward-compatible with every `Finding` already validated by #26/#27's
    # tests. Consumer: `tui/widgets.py`'s `FindingsBox`/`render_findings` (issue #42), the
    # only current consumer -- it renders this alongside severity and description when
    # present, and omits it when `None`. No current producer sets it: `ReviewStep`'s prompt
    # (`steps/review.py`) does not yet ask the agent to fill it in, so every finding
    # produced today leaves this at its default. That is a future refinement, not this
    # field's job.
    location: str | None = None


def action_or_default(action: str | None) -> Action:
    """Resolve `action` to itself if it is one of the three known values, else to the
    fail-safe default `ask-user` (see module docstring). Handles all three cases the
    acceptance criteria call out identically, because they collapse to the same check:
    an unset field, a JSON `null`, and an unrecognized string all fail the membership
    test below and fall through to the same default.
    """

    if action in _VALID_ACTIONS:
        # Safe: membership in `_VALID_ACTIONS` (whose members are exactly `Action`'s
        # literal values) is what mypy cannot infer from `in` alone.
        return action  # type: ignore[return-value]
    return DEFAULT_ACTION


def has_blocking_finding(findings: list[Finding]) -> bool:
    """True if any finding in `findings`, after resolving its action via
    `action_or_default`, needs a human (`action == "ask-user"`) -- the shared blocking-
    findings gate (see docs/GLOSSARY.md's "Blocking finding"). Operates on `list[Finding]`
    rather than anything review-specific so Milestone 6's test-sufficiency step can reuse
    it unchanged.
    """

    return any(action_or_default(finding.action) == "ask-user" for finding in findings)
