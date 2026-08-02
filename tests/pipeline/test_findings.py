"""Unit tests for the Finding schema, the fail-safe action default, and the shared
blocking-findings gate (Milestone 5, issue #26).

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
    has_blocking_finding,
)


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
