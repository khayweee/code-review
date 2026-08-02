"""Widget-level tests for `PipelineBox`/`FindingsBox`, driven with Textual's
`Pilot`/`run_test()`.

`render_rows`/`format_row`/`format_duration` (and `render_findings`/`format_finding`) are
exercised directly for the pure formatting rules; `PipelineBox`/`FindingsBox` themselves are
mounted in a minimal `App` and driven through `run_test()` to prove `update_rows`/
`update_findings` actually reach the rendered widget content.
"""

from __future__ import annotations

import asyncio

import pytest
from textual.app import App, ComposeResult

from code_review.pipeline.findings import Finding
from code_review.steps.review import ReviewOutput
from code_review.tui.state import StepRow
from code_review.tui.widgets import (
    FindingsBox,
    PipelineBox,
    format_duration,
    format_finding,
    format_row,
    render_findings,
    render_rows,
)

# --- pure formatting -------------------------------------------------------------------


def test_format_duration_renders_sub_minute_durations_with_one_decimal() -> None:
    assert format_duration(0.3) == "0.3s"
    assert format_duration(59.9) == "59.9s"


def test_format_duration_renders_minute_and_above_as_mm_ss() -> None:
    assert format_duration(60.0) == "1:00"
    assert format_duration(125.0) == "2:05"


@pytest.mark.parametrize(
    ("status", "icon"),
    [("pending", "○"), ("running", "◐"), ("completed", "✓"), ("failed", "✗")],
)
def test_format_row_uses_a_distinct_icon_per_status(status: str, icon: str) -> None:
    row = StepRow(name="IntentStep", status=status, duration=None)  # type: ignore[arg-type]

    assert format_row(row).startswith(icon)


def test_format_row_omits_duration_while_pending() -> None:
    row = StepRow(name="IntentStep", status="pending", duration=None)

    assert format_row(row) == "○ IntentStep"


def test_format_row_includes_duration_once_running_or_completed() -> None:
    running = StepRow(name="IntentStep", status="running", duration=1.2)
    completed = StepRow(name="IntentStep", status="completed", duration=3.4)

    assert format_row(running) == "◐ IntentStep  1.2s"
    assert format_row(completed) == "✓ IntentStep  3.4s"


def test_render_rows_renders_one_line_per_row_in_order() -> None:
    rows = [
        StepRow(name="IntentStep", status="completed", duration=0.1),
        StepRow(name="RebaseStep", status="pending", duration=None),
    ]

    assert render_rows(rows) == "✓ IntentStep  0.1s\n○ RebaseStep"


# --- PipelineBox, mounted and driven through Pilot --------------------------------------


class _HostApp(App[None]):
    """Minimal host app: mounts one `PipelineBox` so `Pilot` can drive it directly,
    independent of `ReviewApp`'s event-consuming worker."""

    def __init__(self, rows: list[StepRow]) -> None:
        super().__init__()
        self._initial_rows = rows

    def compose(self) -> ComposeResult:
        yield PipelineBox(self._initial_rows)


def test_pipeline_box_renders_its_initial_rows_on_mount() -> None:
    async def scenario() -> None:
        app = _HostApp([StepRow(name="IntentStep", status="pending", duration=None)])
        async with app.run_test() as pilot:
            await pilot.pause()
            box = app.query_one(PipelineBox)
            assert box.content == "○ IntentStep"

    asyncio.run(scenario())


def test_pipeline_box_update_rows_replaces_the_rendered_content() -> None:
    async def scenario() -> None:
        app = _HostApp([StepRow(name="IntentStep", status="pending", duration=None)])
        async with app.run_test() as pilot:
            await pilot.pause()
            box = app.query_one(PipelineBox)

            box.update_rows([StepRow(name="IntentStep", status="running", duration=0.5)])
            await pilot.pause()

            assert box.content == "◐ IntentStep  0.5s"

    asyncio.run(scenario())


def test_pipeline_box_has_a_pipeline_border_title() -> None:
    async def scenario() -> None:
        app = _HostApp([])
        async with app.run_test() as pilot:
            await pilot.pause()
            box = app.query_one(PipelineBox)
            assert box.border_title == "Pipeline"

    asyncio.run(scenario())


# --- render_findings / format_finding: pure formatting -----------------------------------


def test_format_finding_omits_location_when_none() -> None:
    finding = Finding(severity="warning", description="missing null check", review_scope="source")

    assert format_finding(finding) == "warning: missing null check"


def test_format_finding_includes_location_when_present() -> None:
    finding = Finding(
        severity="error",
        description="removes the retry loop's backoff",
        review_scope="source",
        location="steps/review.py:42",
    )

    assert format_finding(finding) == (
        "error: removes the retry loop's backoff (steps/review.py:42)"
    )


def test_render_findings_lists_each_finding_and_a_severity_count_summary() -> None:
    output = ReviewOutput(
        findings=[
            Finding(
                severity="error",
                description="removes the retry loop's backoff",
                review_scope="source",
                location="steps/review.py:42",
            ),
            Finding(severity="warning", description="unclear variable name", review_scope="source"),
            Finding(severity="info", description="consider a docstring", review_scope="source"),
        ],
        risk_level="high",
        risk_rationale="removes retry backoff",
    )

    assert render_findings(output) == (
        "error: removes the retry loop's backoff (steps/review.py:42)\n"
        "warning: unclear variable name\n"
        "info: consider a docstring\n"
        "\n"
        "1 error, 1 warning, 1 info"
    )


def test_render_findings_summary_counts_zero_severities_not_seen() -> None:
    output = ReviewOutput(
        findings=[Finding(severity="info", description="minor style nit", review_scope="source")],
        risk_level="low",
        risk_rationale="fine",
    )

    assert render_findings(output).endswith("0 error, 0 warning, 1 info")


# --- FindingsBox, mounted and driven through Pilot ----------------------------------------


class _FindingsHostApp(App[None]):
    """Minimal host app: mounts one `FindingsBox` so `Pilot` can drive it directly,
    independent of `ReviewApp`'s event-consuming worker."""

    def __init__(self, output: ReviewOutput) -> None:
        super().__init__()
        self._initial_output = output

    def compose(self) -> ComposeResult:
        yield FindingsBox(self._initial_output)


def test_findings_box_renders_its_initial_findings_on_mount() -> None:
    async def scenario() -> None:
        output = ReviewOutput(
            findings=[
                Finding(severity="warning", description="unclear naming", review_scope="source")
            ],
            risk_level="low",
            risk_rationale="fine",
        )
        app = _FindingsHostApp(output)
        async with app.run_test() as pilot:
            await pilot.pause()
            box = app.query_one(FindingsBox)
            assert box.content == render_findings(output)

    asyncio.run(scenario())


def test_findings_box_update_findings_replaces_the_rendered_content() -> None:
    async def scenario() -> None:
        initial = ReviewOutput(
            findings=[Finding(severity="info", description="first", review_scope="source")],
            risk_level="low",
            risk_rationale="fine",
        )
        app = _FindingsHostApp(initial)
        async with app.run_test() as pilot:
            await pilot.pause()
            box = app.query_one(FindingsBox)

            updated = ReviewOutput(
                findings=[Finding(severity="error", description="second", review_scope="source")],
                risk_level="high",
                risk_rationale="bad",
            )
            box.update_findings(updated)
            await pilot.pause()

            assert box.content == render_findings(updated)

    asyncio.run(scenario())


def test_findings_box_has_a_findings_border_title() -> None:
    async def scenario() -> None:
        output = ReviewOutput(findings=[], risk_level="low", risk_rationale="fine")
        app = _FindingsHostApp(output)
        async with app.run_test() as pilot:
            await pilot.pause()
            box = app.query_one(FindingsBox)
            assert box.border_title == "Findings"

    asyncio.run(scenario())
