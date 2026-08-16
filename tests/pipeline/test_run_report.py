"""Pure unit tests for `build_run_report`/`format_run_report`, independent of Textual (see
`run_report.py`'s docstring).

No `App`, no `Pilot`, no subprocess -- every `StepEvent` here is hand-built, mirroring
`tests/tui/test_state.py`'s convention.
"""

from __future__ import annotations

from code_review.agent import Usage
from code_review.pipeline.run_report import (
    PipelineRunReport,
    StepUsage,
    build_run_report,
    format_run_report,
)
from code_review.pipeline.schemas import StepEvent
from code_review.pipeline.step import StepOutcome


def _completed(
    step_name: str, usage: Usage | None, *, started_at: float = 0.0, duration: float = 0.1
) -> StepEvent:
    return StepEvent(
        step_name=step_name,
        status="completed",
        outcome=StepOutcome(needs_approval=False, auto_fixable=False, payload=[], usage=usage),
        started_at=started_at,
        duration=duration,
    )


def _running(step_name: str, *, started_at: float = 0.0) -> StepEvent:
    return StepEvent(
        step_name=step_name, status="running", outcome=None, started_at=started_at, duration=None
    )


# --- build_run_report ----------------------------------------------------------------------


def test_build_run_report_with_a_single_step_reports_its_usage_as_the_totals() -> None:
    events = [
        _running("ReviewStep"),
        _completed("ReviewStep", Usage(input_tokens=100, output_tokens=50, total_cost_usd=0.01)),
    ]

    report = build_run_report(events)

    assert report.total_input_tokens == 100
    assert report.total_output_tokens == 50
    assert report.total_cost_usd == 0.01
    assert report.per_step == (
        StepUsage(
            step_name="ReviewStep",
            usage=Usage(input_tokens=100, output_tokens=50, total_cost_usd=0.01),
        ),
    )


def test_build_run_report_sums_multiple_steps_into_the_totals() -> None:
    events = [
        _completed("ReviewStep", Usage(input_tokens=100, output_tokens=50, total_cost_usd=0.01)),
        _completed(
            "TestSufficiencyStep",
            Usage(input_tokens=200, output_tokens=80, total_cost_usd=0.02),
        ),
    ]

    report = build_run_report(events)

    assert report.total_input_tokens == 300
    assert report.total_output_tokens == 130
    assert report.total_cost_usd == 0.03
    assert [su.step_name for su in report.per_step] == ["ReviewStep", "TestSufficiencyStep"]


def test_build_run_report_sums_multiple_fix_rounds_of_the_same_step_into_one_entry() -> None:
    """A step whose `supports_fix_round = True` can complete more than one round in a single
    pipeline slot (auto-fix rounds, or an uncapped human "fix" park response) -- each round
    its own separate `StepEvent`/`Usage`, summed into one `StepUsage` entry, not one per
    round."""

    events = [
        _completed("ReviewStep", Usage(input_tokens=100, output_tokens=50, total_cost_usd=0.01)),
        _running("ReviewStep", started_at=1.0),
        _completed(
            "ReviewStep",
            Usage(input_tokens=40, output_tokens=20, total_cost_usd=0.005),
            started_at=1.0,
        ),
    ]

    report = build_run_report(events)

    assert len(report.per_step) == 1
    assert report.per_step[0] == StepUsage(
        step_name="ReviewStep",
        usage=Usage(input_tokens=140, output_tokens=70, total_cost_usd=0.015),
    )
    assert report.total_input_tokens == 140
    assert report.total_output_tokens == 70
    assert report.total_cost_usd == 0.015


def test_build_run_report_excludes_a_step_that_never_set_usage() -> None:
    """A step that made no agent call (`StepOutcome.usage` stays `None`) has nothing to
    show -- it's omitted from `per_step` entirely, not rendered as a zeroed row."""

    events = [
        _completed("WorktreeStep", None),
        _completed("ReviewStep", Usage(input_tokens=100, output_tokens=50)),
    ]

    report = build_run_report(events)

    assert [su.step_name for su in report.per_step] == ["ReviewStep"]


def test_build_run_report_treats_a_none_field_as_no_contribution_not_zero() -> None:
    """Summing `[3, None, 5]` gives `8`, not treating the `None` round as `0` -- proven here
    via one step's two rounds, one of which only reported cost, the other only tokens."""

    events = [
        _completed("ReviewStep", Usage(input_tokens=100, output_tokens=50, total_cost_usd=None)),
        _running("ReviewStep", started_at=1.0),
        _completed(
            "ReviewStep",
            Usage(input_tokens=None, output_tokens=None, total_cost_usd=0.02),
            started_at=1.0,
        ),
    ]

    report = build_run_report(events)

    assert report.per_step[0].usage == Usage(
        input_tokens=100, output_tokens=50, total_cost_usd=0.02
    )
    assert report.total_input_tokens == 100
    assert report.total_output_tokens == 50
    assert report.total_cost_usd == 0.02


def test_build_run_report_with_no_usage_anywhere_reports_none_totals_and_no_per_step() -> None:
    """A run that failed before any agent call (or `scripts/preview_tui.py`'s synthetic
    outcomes, which never set `usage`) reports `None` totals, not `0` -- `0` would
    misleadingly claim "0 tokens used" instead of "no usage data available"."""

    events = [_running("WorktreeStep"), _completed("WorktreeStep", None)]

    report = build_run_report(events)

    assert report == PipelineRunReport(
        total_input_tokens=None, total_output_tokens=None, total_cost_usd=None, per_step=()
    )


def test_build_run_report_ignores_a_still_running_event_with_no_outcome_yet() -> None:
    events = [_running("ReviewStep")]

    report = build_run_report(events)

    assert report.per_step == ()


# --- format_run_report ----------------------------------------------------------------------


def test_format_run_report_is_empty_string_when_per_step_is_empty() -> None:
    report = PipelineRunReport(
        total_input_tokens=None, total_output_tokens=None, total_cost_usd=None, per_step=()
    )

    assert format_run_report(report) == ""


def test_format_run_report_renders_totals_and_a_line_per_step() -> None:
    report = PipelineRunReport(
        total_input_tokens=1234,
        total_output_tokens=567,
        total_cost_usd=0.0891,
        per_step=(
            StepUsage(
                step_name="ReviewStep",
                usage=Usage(input_tokens=1000, output_tokens=400, total_cost_usd=0.07),
            ),
            StepUsage(
                step_name="TestSufficiencyStep",
                usage=Usage(input_tokens=234, output_tokens=167, total_cost_usd=0.0191),
            ),
        ),
    )

    text = format_run_report(report)

    assert text == (
        "Tokens used: 1,234 in / 567 out ($0.0891)\n"
        "  ReviewStep: 1,000 in / 400 out ($0.0700)\n"
        "  TestSufficiencyStep: 234 in / 167 out ($0.0191)"
    )


def test_format_run_report_translates_step_names_via_display_names() -> None:
    report = PipelineRunReport(
        total_input_tokens=100,
        total_output_tokens=50,
        total_cost_usd=None,
        per_step=(
            StepUsage(step_name="ReviewStep", usage=Usage(input_tokens=100, output_tokens=50)),
        ),
    )

    text = format_run_report(report, display_names={"ReviewStep": "Review"})

    assert "  Review: 100 in / 50 out" in text
    assert "ReviewStep" not in text


def test_format_run_report_omits_cost_when_total_cost_usd_is_none() -> None:
    report = PipelineRunReport(
        total_input_tokens=100,
        total_output_tokens=50,
        total_cost_usd=None,
        per_step=(
            StepUsage(step_name="ReviewStep", usage=Usage(input_tokens=100, output_tokens=50)),
        ),
    )

    text = format_run_report(report)

    assert "$" not in text
    assert text.splitlines()[0] == "Tokens used: 100 in / 50 out"


def test_format_run_report_shows_cost_only_when_token_counts_are_none() -> None:
    report = PipelineRunReport(
        total_input_tokens=None,
        total_output_tokens=None,
        total_cost_usd=0.05,
        per_step=(StepUsage(step_name="ReviewStep", usage=Usage(total_cost_usd=0.05)),),
    )

    text = format_run_report(report)

    assert text == "Tokens used: $0.0500\n  ReviewStep: $0.0500"
