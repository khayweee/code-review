"""Unit tests for the Review step's schema, intent-conformance clause, and deterministic
pipeline-owned-delivery scope filter (Milestone 5, issue #26).

Pure function/schema tests, matching `tests/steps/test_intent.py`'s "pure function tests,
nothing mocked" convention -- no `ReviewStep` (issue #27, not built yet), no agent, no
subprocess anywhere in this file. Fixtures are plain JSON dicts fed through
`ReviewOutput.model_validate`.
"""

from __future__ import annotations

from code_review.steps.intent import Intent
from code_review.steps.review import (
    ReviewOutput,
    filter_pipeline_owned_delivery_findings,
    intent_conformance_clause,
)

# --- ReviewOutput schema shape -----------------------------------------------------------


def test_review_output_field_order_is_findings_then_risk_fields() -> None:
    # Pins the reference implementation's chain-of-thought ordering (see the schema's
    # docstring): a reordering here would silently change what an agent is asked to
    # reason about first.
    assert list(ReviewOutput.model_fields.keys()) == [
        "findings",
        "risk_level",
        "risk_rationale",
        "risk_scope",
    ]


def test_review_output_accepts_all_documented_fields() -> None:
    output = ReviewOutput.model_validate(
        {
            "findings": [
                {
                    "severity": "warning",
                    "description": "missing null check",
                    "action": "ask-user",
                    "review_scope": "source",
                }
            ],
            "risk_level": "medium",
            "risk_rationale": "touches error handling on a hot path",
            "risk_scope": "source-or-external",
        }
    )

    assert len(output.findings) == 1
    assert output.risk_level == "medium"
    assert output.risk_rationale == "touches error handling on a hot path"
    assert output.risk_scope == "source-or-external"


def test_review_output_risk_scope_is_optional_and_defaults_to_none() -> None:
    output = ReviewOutput.model_validate(
        {"findings": [], "risk_level": "low", "risk_rationale": "no issues found"}
    )

    assert output.risk_scope is None


def test_review_output_requires_risk_level_and_risk_rationale() -> None:
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ReviewOutput.model_validate({"findings": []})


# --- intent_conformance_clause -----------------------------------------------------------


def test_intent_conformance_clause_is_empty_for_non_explicit_intent() -> None:
    intent = Intent(summary="inferred from transcript", source="claude", score=0.4)

    assert intent_conformance_clause(intent) == ""


def test_intent_conformance_clause_is_present_for_explicit_intent() -> None:
    intent = Intent(summary="use a queue, not polling", source="explicit", score=1.0)

    clause = intent_conformance_clause(intent)

    assert clause != ""


def test_intent_conformance_clause_obligates_ask_user_on_required_criterion_removal() -> None:
    intent = Intent(summary="use a queue, not polling", source="explicit", score=1.0)

    clause = intent_conformance_clause(intent)

    assert "REQUIRED" in clause
    assert "ask-user" in clause


def test_intent_conformance_clause_obligates_ask_user_on_forbidden_behavior_addition() -> None:
    intent = Intent(summary="use a queue, not polling", source="explicit", score=1.0)

    clause = intent_conformance_clause(intent)

    assert "FORBIDDEN" in clause
    assert "ask-user" in clause


def test_intent_conformance_clause_applies_even_when_otherwise_risk_clean() -> None:
    intent = Intent(summary="use a queue, not polling", source="explicit", score=1.0)

    clause = intent_conformance_clause(intent)

    assert "risk-clean" in clause.lower()


# --- filter_pipeline_owned_delivery_findings: the deterministic scope filter -------------


def _finding(**overrides: object) -> dict[str, object]:
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
                _finding(review_scope="source"),
                _finding(review_scope="pipeline-owned-delivery"),
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
            "findings": [_finding(review_scope="pipeline-owned-delivery")],
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
                _finding(severity="error", review_scope="pipeline-owned-delivery"),
                _finding(severity="info", review_scope="source"),
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
                _finding(severity="error", review_scope="pipeline-owned-delivery"),
                _finding(severity="error", review_scope="source"),
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
            "findings": [_finding(severity="error", review_scope="pipeline-owned-delivery")],
            "risk_level": "low",
            "risk_rationale": "informational only",
        }
    )

    filtered = filter_pipeline_owned_delivery_findings(output)

    assert filtered.risk_level == "low"
    assert filtered.risk_rationale == "informational only"
