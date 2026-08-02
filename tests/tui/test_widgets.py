"""Widget-level tests for `PipelineBox`, driven with Textual's `Pilot`/`run_test()`.

`render_rows`/`format_row`/`format_duration` are exercised directly for the pure
formatting rules; `PipelineBox` itself is mounted in a minimal `App` and driven through
`run_test()` to prove `update_rows` actually reaches the rendered widget content.
"""

from __future__ import annotations

import asyncio

import pytest
from textual.app import App, ComposeResult

from code_review.tui.state import StepRow
from code_review.tui.widgets import PipelineBox, format_duration, format_row, render_rows

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
