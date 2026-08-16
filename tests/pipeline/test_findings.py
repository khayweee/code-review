"""Unit tests for the Finding schema, the fail-safe action default, the shared
blocking-findings gate (Milestone 5, issue #26), and the deterministic
pipeline-owned-delivery scope filter (moved here from `tests/steps/test_review.py` in a
later structural refactor alongside `filter_pipeline_owned_delivery_findings` itself
moving to `pipeline/findings.py`).

Pure function/schema tests -- no agent, subprocess, or filesystem dependency, matching
`tests/steps/test_intent.py`'s "pure function tests, nothing mocked" convention. Fixtures
are plain JSON dicts fed through `Finding.model_validate`, never constructed by calling
`Finding(...)` directly with keyword arguments only, wherever the point being tested is
about what an agent's raw JSON response would look like (e.g. an "unset" field simply
being absent from the dict, matching what a real backend response could omit).
"""

from __future__ import annotations

from code_review.pipeline.findings import (
    DEFAULT_ACTION,
    Finding,
    action_or_default,
    describe_finding_decisions,
    filter_pipeline_owned_delivery_findings,
    has_blocking_finding,
)
from code_review.pipeline.schemas import ApprovalResponse, FindingDecision
from code_review.steps.review import ReviewOutput


def _finding(**overrides: object) -> Finding:
    base: dict[str, object] = {
        "severity": "warning",
        "description": "example finding",
        "review_scope": "source",
    }
    base.update(overrides)
    return Finding.model_validate(base)


# --- Finding schema shape ---------------------------------------------------------------


def test_finding_accepts_all_documented_fields() -> None:
    finding = Finding.model_validate(
        {
            "severity": "error",
            "description": "removes the retry loop's backoff",
            "action": "ask-user",
            "review_scope": "source",
        }
    )

    assert finding.severity == "error"
    assert finding.description == "removes the retry loop's backoff"
    assert finding.action == "ask-user"
    assert finding.review_scope == "source"


def test_finding_review_scope_accepts_the_reserved_external_delivery_value() -> None:
    # external-delivery has no producer/consumer in this codebase yet (see the field's
    # doc comment in pipeline/findings.py) but must still validate as a legal enum value.
    finding = _finding(review_scope="external-delivery")

    assert finding.review_scope == "external-delivery"


def test_finding_location_defaults_to_none_when_unset() -> None:
    # No producer sets `location` yet (see the field's doc comment in
    # pipeline/findings.py) -- an agent response omitting it entirely must still validate.
    finding = _finding()

    assert finding.location is None


def test_finding_location_accepts_a_file_line_string() -> None:
    finding = _finding(location="steps/review.py:42")

    assert finding.location == "steps/review.py:42"


def test_finding_suggestions_defaults_to_empty_list_when_unset() -> None:
    # Mirrors `location`'s own default test above -- an agent response omitting
    # `suggestions` entirely must still validate, matching every existing fixture.
    finding = _finding()

    assert finding.suggestions == []


def test_finding_suggestions_accepts_a_list_of_strings() -> None:
    finding = _finding(suggestions=["revert the rewrap", "add a comment explaining this"])

    assert finding.suggestions == ["revert the rewrap", "add a comment explaining this"]


def test_finding_with_ask_user_action_and_empty_suggestions_still_validates() -> None:
    # Prompt-only enforcement (see pipeline/findings.py's module docstring for why):
    # nothing at the schema level rejects an ask-user finding with no suggestions.
    finding = _finding(action="ask-user", suggestions=[])

    assert finding.action == "ask-user"
    assert finding.suggestions == []


# --- action_or_default: the fail-safe default -------------------------------------------


def test_action_or_default_is_ask_user() -> None:
    assert DEFAULT_ACTION == "ask-user"


def test_action_or_default_resolves_unset_action_to_ask_user() -> None:
    finding = Finding.model_validate(
        {"severity": "warning", "description": "no action key at all", "review_scope": "source"}
    )

    assert action_or_default(finding.action) == "ask-user"


def test_action_or_default_resolves_explicit_null_action_to_ask_user() -> None:
    finding = Finding.model_validate(
        {
            "severity": "warning",
            "description": "action explicitly null",
            "action": None,
            "review_scope": "source",
        }
    )

    assert action_or_default(finding.action) == "ask-user"


def test_action_or_default_resolves_unrecognized_action_string_to_ask_user() -> None:
    finding = Finding.model_validate(
        {
            "severity": "warning",
            "description": "an agent hallucinated an action value",
            "action": "delete-repo",
            "review_scope": "source",
        }
    )

    assert action_or_default(finding.action) == "ask-user"


def test_action_or_default_passes_through_each_recognized_action_unchanged() -> None:
    assert action_or_default("no-op") == "no-op"
    assert action_or_default("auto-fix") == "auto-fix"
    assert action_or_default("ask-user") == "ask-user"


# --- has_blocking_finding: the shared blocking-findings gate -----------------------------


def test_has_blocking_finding_is_true_when_a_finding_has_an_explicit_ask_user_action() -> None:
    findings = [_finding(action="no-op"), _finding(action="ask-user")]

    assert has_blocking_finding(findings) is True


def test_has_blocking_finding_is_true_when_a_finding_has_an_unset_action() -> None:
    # Proves the gate applies the fail-safe default itself rather than requiring the
    # caller to pre-resolve every finding's action first.
    findings = [_finding(action="no-op"), _finding()]

    assert has_blocking_finding(findings) is True


def test_has_blocking_finding_is_false_when_every_finding_resolves_away_from_ask_user() -> None:
    findings = [_finding(action="no-op"), _finding(action="auto-fix")]

    assert has_blocking_finding(findings) is False


def test_has_blocking_finding_is_false_for_an_empty_findings_list() -> None:
    assert has_blocking_finding([]) is False


# --- filter_pipeline_owned_delivery_findings: the deterministic scope filter -------------


def _finding_dict(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "severity": "warning",
        "description": "example finding",
        "action": "ask-user",
        "review_scope": "source",
    }
    base.update(overrides)
    return base


def test_scope_filter_strips_pipeline_owned_delivery_findings() -> None:
    output = ReviewOutput.model_validate(
        {
            "findings": [
                _finding_dict(review_scope="source"),
                _finding_dict(review_scope="pipeline-owned-delivery"),
            ],
            "risk_level": "low",
            "risk_rationale": "one source finding, informational",
        }
    )

    filtered = filter_pipeline_owned_delivery_findings(output)

    assert len(filtered.findings) == 1
    assert filtered.findings[0].review_scope == "source"


def test_scope_filter_does_not_mutate_the_input_review_output() -> None:
    output = ReviewOutput.model_validate(
        {
            "findings": [_finding_dict(review_scope="pipeline-owned-delivery")],
            "risk_level": "low",
            "risk_rationale": "unaffected",
        }
    )

    filter_pipeline_owned_delivery_findings(output)

    assert len(output.findings) == 1


def test_scope_filter_resets_risk_level_when_pipeline_owned_delivery_findings_are_sole_basis() -> (
    None
):
    """Regression test A (issue #26): the only findings severe enough to justify a
    medium/high risk_level are pipeline-owned-delivery-scoped. After filtering, the
    elevated risk level has no remaining basis and must reset to "low" with a rationale
    that names the reset -- overwritten, not appended, so it never describes a risk level
    that is no longer returned."""

    output = ReviewOutput.model_validate(
        {
            "findings": [
                _finding_dict(severity="error", review_scope="pipeline-owned-delivery"),
                _finding_dict(severity="info", review_scope="source"),
            ],
            "risk_level": "high",
            "risk_rationale": "pipeline-generated lockfile diff looked malformed",
        }
    )

    filtered = filter_pipeline_owned_delivery_findings(output)

    assert filtered.risk_level == "low"
    assert filtered.risk_rationale != output.risk_rationale
    assert "reset" in filtered.risk_rationale.lower()
    assert len(filtered.findings) == 1
    assert filtered.findings[0].review_scope == "source"


def test_scope_filter_keeps_risk_level_when_a_source_finding_at_the_same_severity_survives() -> (
    None
):
    """Regression test B (issue #26): at least one source-scoped finding at the same
    severity that justified risk_level survives filtering, so the elevated risk level
    must NOT be reset -- filtering must not overreach into risk the pipeline-owned
    findings were never the sole basis for."""

    output = ReviewOutput.model_validate(
        {
            "findings": [
                _finding_dict(severity="error", review_scope="pipeline-owned-delivery"),
                _finding_dict(severity="error", review_scope="source"),
            ],
            "risk_level": "high",
            "risk_rationale": "both a pipeline artifact and hand-written code look broken",
        }
    )

    filtered = filter_pipeline_owned_delivery_findings(output)

    assert filtered.risk_level == "high"
    assert filtered.risk_rationale == output.risk_rationale
    assert len(filtered.findings) == 1
    assert filtered.findings[0].review_scope == "source"


def test_scope_filter_leaves_an_already_low_risk_level_untouched() -> None:
    output = ReviewOutput.model_validate(
        {
            "findings": [_finding_dict(severity="error", review_scope="pipeline-owned-delivery")],
            "risk_level": "low",
            "risk_rationale": "informational only",
        }
    )

    filtered = filter_pipeline_owned_delivery_findings(output)

    assert filtered.risk_level == "low"
    assert filtered.risk_rationale == "informational only"


# --- describe_finding_decisions (issue #98) ----------------------------------------------


def test_describe_finding_decisions_renders_one_line_per_fix_decided_finding() -> None:
    decisions = [
        FindingDecision(
            finding=_finding(severity="warning", description="unclear naming", location="a.py:1"),
            response=ApprovalResponse(decision="fix", instructions="rename it"),
        ),
        FindingDecision(
            finding=_finding(severity="error", description="missing null check"),
            response=ApprovalResponse(decision="fix", instructions="add a guard clause"),
        ),
    ]

    assert describe_finding_decisions(decisions) == (
        "- [warning] unclear naming (a.py:1): rename it\n"
        "- [error] missing null check: add a guard clause"
    )


def test_describe_finding_decisions_omits_skip_decided_findings() -> None:
    decisions = [
        FindingDecision(
            finding=_finding(severity="warning", description="unclear naming"),
            response=ApprovalResponse(decision="fix", instructions="rename it"),
        ),
        FindingDecision(
            finding=_finding(severity="info", description="minor style nit"),
            response=ApprovalResponse(decision="skip", instructions=None),
        ),
    ]

    assert describe_finding_decisions(decisions) == "- [warning] unclear naming: rename it"


def test_describe_finding_decisions_is_empty_when_every_finding_was_skipped() -> None:
    decisions = [
        FindingDecision(
            finding=_finding(), response=ApprovalResponse(decision="skip", instructions=None)
        ),
        FindingDecision(
            finding=_finding(), response=ApprovalResponse(decision="skip", instructions=None)
        ),
    ]

    assert describe_finding_decisions(decisions) == ""


def test_describe_finding_decisions_is_empty_for_an_empty_list() -> None:
    assert describe_finding_decisions([]) == ""
