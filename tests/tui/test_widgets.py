"""Widget-level tests for `PipelineBox`/`FindingsBox`, driven with Textual's
`Pilot`/`run_test()`.

`render_rows`/`format_row`/`format_duration` (and `render_findings`/`format_finding`) are
exercised directly for the pure formatting rules; `PipelineBox`/`FindingsBox` themselves are
mounted in a minimal `App` and driven through `run_test()` to prove `update_rows`/
`update_findings` actually reach the rendered widget content.
"""

from __future__ import annotations

import asyncio
from io import StringIO

import pytest
from rich.console import Console
from rich.spinner import Spinner
from textual.app import App, ComposeResult

from code_review.pipeline.findings import Finding
from code_review.steps.review import ReviewOutput
from code_review.steps.test_sufficiency import TestSufficiencyOutput
from code_review.tui.state import ActivityRow, StepRow
from code_review.tui.widgets import (
    FindingsBox,
    PipelineBox,
    StatusBox,
    _render_row,
    format_activity_row,
    format_duration,
    format_finding,
    format_row,
    gradient_text,
    render_findings,
    render_rows,
    render_rows_live,
)


def _render_content(renderable: object) -> str:
    buffer = StringIO()
    console = Console(file=buffer, force_terminal=True, width=80, color_system=None)
    console.print(renderable)
    return buffer.getvalue().rstrip()


# --- pure formatting -------------------------------------------------------------------


def test_format_duration_renders_sub_minute_durations_with_one_decimal() -> None:
    assert format_duration(0.3) == "0.3s"
    assert format_duration(59.9) == "59.9s"


def test_format_duration_renders_minute_and_above_as_mm_ss() -> None:
    assert format_duration(60.0) == "1:00"
    assert format_duration(125.0) == "2:05"


@pytest.mark.parametrize(
    ("status", "icon"),
    [("pending", "◌"), ("completed", "✔"), ("failed", "✘")],
)
def test_format_row_uses_a_distinct_icon_per_status(status: str, icon: str) -> None:
    row = StepRow(name="IntentStep", status=status, duration=None)  # type: ignore[arg-type]

    assert format_row(row).startswith(icon)


def test_format_row_uses_a_fallback_icon_for_running_status() -> None:
    row = StepRow(name="IntentStep", status="running", duration=1.2)

    assert format_row(row).startswith("◔")


def test_format_row_omits_duration_while_pending() -> None:
    row = StepRow(name="IntentStep", status="pending", duration=None)

    assert format_row(row) == "◌ IntentStep"


def test_format_row_includes_duration_once_running_or_completed() -> None:
    running = StepRow(name="IntentStep", status="running", duration=1.2)
    completed = StepRow(name="IntentStep", status="completed", duration=3.4)

    assert format_row(running) == "◔ IntentStep  1.2s"
    assert format_row(completed) == "✔ IntentStep  3.4s"


def test_render_rows_renders_one_line_per_row_in_order() -> None:
    rows = [
        StepRow(name="IntentStep", status="completed", duration=0.1),
        StepRow(name="RebaseStep", status="pending", duration=None),
    ]

    assert render_rows(rows) == "✔ IntentStep  0.1s\n◌ RebaseStep"


# --- ActivityRow rendering (issue #66) ---------------------------------------------------


def test_format_activity_row_uses_tree_connectors_and_the_same_status_icons() -> None:
    running = ActivityRow(label="fetch", status="running", duration=1.2)
    completed = ActivityRow(label="rebase", status="completed", duration=3.4)

    assert format_activity_row(running, is_last=False) == "  ├  ◔ fetch  1.2s"
    assert format_activity_row(completed, is_last=True) == "  └  ✔ rebase  3.4s"


def test_format_activity_row_omits_duration_when_none() -> None:
    activity = ActivityRow(label="fetch", status="running", duration=None)

    assert format_activity_row(activity, is_last=True) == "  └  ◔ fetch"


def test_render_rows_nests_each_rows_activities_beneath_it() -> None:
    rows = [
        StepRow(
            name="RebaseStep",
            status="running",
            duration=1.5,
            activities=(
                ActivityRow(label="fetch", status="completed", duration=0.2),
                ActivityRow(label="rebase", status="running", duration=1.1),
            ),
        ),
        StepRow(name="ReviewStep", status="pending", duration=None),
    ]

    assert render_rows(rows) == (
        "◔ RebaseStep  1.5s\n  ├  ✔ fetch  0.2s\n  └  ◔ rebase  1.1s\n◌ ReviewStep"
    )


def test_render_rows_a_row_with_no_activities_gets_no_extra_lines() -> None:
    rows = [StepRow(name="IntentStep", status="completed", duration=0.1)]

    assert render_rows(rows) == "✔ IntentStep  0.1s"


def test_render_rows_live_keeps_each_steps_icon_column_independent_of_others_activities() -> None:
    """Regression test for `render_rows_live`'s `Group`-of-per-row-grids shape (see its own
    docstring): the previous single shared `Table.grid` sized its icon column to the widest
    icon cell across *every* row ever added, so a step with no activities at all still got
    padded out to match however wide another step's activity connector happened to be. A
    step's own rendered line must therefore be identical whether or not some other step in
    the same list has activities."""

    alone = render_rows_live([StepRow(name="IntentStep", status="completed", duration=0.1)], {})
    alongside_a_step_with_activities = render_rows_live(
        [
            StepRow(name="IntentStep", status="completed", duration=0.1),
            StepRow(
                name="RebaseStep",
                status="running",
                duration=1.0,
                activities=(ActivityRow(label="fetch", status="running", duration=0.2),),
            ),
        ],
        {},
    )

    def _first_line(renderable: object) -> str:
        return _render_content(renderable).splitlines()[0]

    assert _first_line(alone) == _first_line(alongside_a_step_with_activities)


# --- gradient_text (animated running-step-name shimmer) ----------------------------------


def test_gradient_text_preserves_the_labels_plain_text() -> None:
    text = gradient_text("RebaseStep", phase=0.0)

    assert text.plain == "RebaseStep"


def test_gradient_text_gives_each_character_its_own_colored_span() -> None:
    text = gradient_text("RebaseStep", phase=0.0)

    assert len(text.spans) == len("RebaseStep")
    # Not every character ends up the same color -- a real gradient, not a solid fill.
    colors = {span.style for span in text.spans}
    assert len(colors) > 1


def test_gradient_text_is_phase_aware_so_consecutive_repaints_visibly_move() -> None:
    """Two different phases must not render identically -- what proves the animation
    actually depends on its `phase` argument (the caller passes `time.monotonic()`) rather
    than being a static gradient recomputed for no reason on every repaint."""

    first = gradient_text("RebaseStep", phase=0.0)
    second = gradient_text("RebaseStep", phase=0.37)

    first_colors = [span.style for span in first.spans]
    second_colors = [span.style for span in second.spans]
    assert first_colors != second_colors


def test_gradient_text_handles_an_empty_label_without_dividing_by_zero() -> None:
    text = gradient_text("", phase=0.2)

    assert text.plain == ""
    assert text.spans == []


def test_render_row_gradients_the_name_of_a_running_step_only() -> None:
    """Distinct from the plain text a pending/completed row gets -- checked directly via
    `_render_row`'s returned `Text` (`.spans`), the same way `PipelineBox._spinners` is
    checked directly elsewhere in this file, since a `color_system=None` console capture
    (`_render_content`) is not a reliable signal for a color-only invariant."""

    spinners: dict[str, Spinner] = {}

    _, running_text = _render_row(
        StepRow(name="RebaseStep", status="running", duration=1.2), spinners
    )
    _, pending_text = _render_row(
        StepRow(name="RebaseStep", status="pending", duration=None), spinners
    )
    _, completed_text = _render_row(
        StepRow(name="RebaseStep", status="completed", duration=1.2), spinners
    )

    # The running row's name portion carries per-character color spans...
    assert len(running_text.spans) == len("RebaseStep")
    # ...while pending/completed rows render as plain text with no color spans at all.
    assert pending_text.spans == []
    assert completed_text.spans == []


def test_render_row_uses_a_colored_dot_icon_for_completed_and_failed_but_not_pending() -> None:
    """Distinct from the plain ✔/✘ glyph the deterministic text fallback (`format_row`)
    uses -- the live pipeline view renders a completed/failed row's icon as a solid dot,
    colored by status (blue/orange), so status reads at a glance from color rather than
    glyph shape. Checked directly on the returned icon `Text` (`.style`), not via a printed
    console capture, for the same reason `gradient_text`'s tests do: color-only invariants
    aren't reliably recoverable from plain-text output."""

    spinners: dict[str, Spinner] = {}

    completed_icon, _ = _render_row(
        StepRow(name="IntentStep", status="completed", duration=0.1), spinners
    )
    failed_icon, _ = _render_row(
        StepRow(name="RebaseStep", status="failed", duration=0.1), spinners
    )
    pending_icon, _ = _render_row(
        StepRow(name="ReviewStep", status="pending", duration=None), spinners
    )

    assert completed_icon.plain == "●"
    assert completed_icon.style == "#5fafff"
    assert failed_icon.plain == "●"
    assert failed_icon.style == "#bb6400"
    # Pending keeps the plain, uncolored hollow-ring glyph -- only completed/failed dot.
    assert pending_icon.plain == "◌"
    assert pending_icon.style == ""


def test_render_row_keeps_the_duration_suffix_plain_even_while_running() -> None:
    spinners: dict[str, Spinner] = {}

    _, running_text = _render_row(
        StepRow(name="RebaseStep", status="running", duration=1.2), spinners
    )

    assert running_text.plain == "RebaseStep  1.2s"
    # Every gradient span ends at or before the name/duration boundary -- only the name is
    # gradiented, the duration suffix stays plain.
    name_length = len("RebaseStep")
    assert all(span.end <= name_length for span in running_text.spans)


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
            assert _render_content(box.content) == "◌ IntentStep"

    asyncio.run(scenario())


def test_pipeline_box_update_rows_replaces_the_rendered_content() -> None:
    async def scenario() -> None:
        app = _HostApp([StepRow(name="IntentStep", status="pending", duration=None)])
        async with app.run_test() as pilot:
            await pilot.pause()
            box = app.query_one(PipelineBox)

            box.update_rows([StepRow(name="IntentStep", status="running", duration=0.5)])
            await pilot.pause()

            content = _render_content(box.content)
            assert "IntentStep" in content
            assert any(
                frame in content for frame in ("🌑", "🌒", "🌓", "🌔", "🌕", "🌖", "🌗", "🌘")
            )

    asyncio.run(scenario())


def test_pipeline_box_reuses_the_same_spinner_instance_while_a_step_keeps_running() -> None:
    """Regression test: `update_rows` used to build a *fresh* `rich.spinner.Spinner` on
    every call. A `Spinner`'s animation clock (`start_time`) is set lazily on its own
    first `render()` call and never reset after that, so a fresh instance on every call
    (as `ReviewApp`'s 0.25s tick timer would trigger) reset that clock back to "now" each
    time and the frame never advanced far enough to look animated. `PipelineBox` must
    reuse the *same* `Spinner` object for a running step across repeated `update_rows`
    calls -- checked here directly via `PipelineBox._spinners`'s cache, rather than via
    rendered frame content, since Rich's own render-time jitter makes frame content an
    unreliable signal for this specific invariant.
    """

    async def scenario() -> None:
        app = _HostApp([StepRow(name="IntentStep", status="running", duration=0.0)])
        async with app.run_test() as pilot:
            await pilot.pause()
            box = app.query_one(PipelineBox)
            first_spinner = box._spinners["IntentStep"]

            box.update_rows([StepRow(name="IntentStep", status="running", duration=0.3)])
            await pilot.pause()

            assert box._spinners["IntentStep"] is first_spinner

    asyncio.run(scenario())


def test_pipeline_box_evicts_a_step_s_spinner_once_it_stops_running() -> None:
    async def scenario() -> None:
        app = _HostApp([StepRow(name="IntentStep", status="running", duration=0.0)])
        async with app.run_test() as pilot:
            await pilot.pause()
            box = app.query_one(PipelineBox)
            assert "IntentStep" in box._spinners

            box.update_rows([StepRow(name="IntentStep", status="completed", duration=0.3)])
            await pilot.pause()

            assert "IntentStep" not in box._spinners

    asyncio.run(scenario())

    asyncio.run(scenario())

    asyncio.run(scenario())


def test_pipeline_box_renders_nested_activity_lines_under_their_owning_row() -> None:
    async def scenario() -> None:
        app = _HostApp([StepRow(name="RebaseStep", status="running", duration=1.0)])
        async with app.run_test() as pilot:
            await pilot.pause()
            box = app.query_one(PipelineBox)

            box.update_rows(
                [
                    StepRow(
                        name="RebaseStep",
                        status="running",
                        duration=1.0,
                        activities=(ActivityRow(label="fetch", status="running", duration=0.4),),
                    )
                ]
            )
            await pilot.pause()

            content = _render_content(box.content)
            lines = content.splitlines()
            assert any("RebaseStep" in line for line in lines)
            assert any("fetch" in line and "0.4s" in line for line in lines)
            # The activity line is indented (nested) beneath the step's own line, not a
            # flush-left top-level row.
            fetch_line = next(line for line in lines if "fetch" in line)
            assert fetch_line.startswith(" ")

    asyncio.run(scenario())


def test_pipeline_box_activity_line_ticks_live_then_collapses_to_a_final_duration() -> None:
    """Mirrors `test_pipeline_box_update_rows_replaces_the_rendered_content`'s own
    live-then-final shape, for a nested activity line: a still-running activity's duration
    changes as `update_rows` is called with a later `now`, and once it reports
    `status="completed"` the duration stops moving and reflects the activity's own final
    span -- matching a `StepRow`'s own "elapsed-so-far, then frozen" duration rule."""

    async def scenario() -> None:
        app = _HostApp([StepRow(name="RebaseStep", status="running", duration=0.0)])
        async with app.run_test() as pilot:
            await pilot.pause()
            box = app.query_one(PipelineBox)

            box.update_rows(
                [
                    StepRow(
                        name="RebaseStep",
                        status="running",
                        duration=0.2,
                        activities=(ActivityRow(label="fetch", status="running", duration=0.2),),
                    )
                ]
            )
            await pilot.pause()
            assert "0.2s" in _render_content(box.content)

            box.update_rows(
                [
                    StepRow(
                        name="RebaseStep",
                        status="running",
                        duration=0.6,
                        activities=(ActivityRow(label="fetch", status="running", duration=0.6),),
                    )
                ]
            )
            await pilot.pause()
            assert "0.6s" in _render_content(box.content)

            box.update_rows(
                [
                    StepRow(
                        name="RebaseStep",
                        status="running",
                        duration=5.0,
                        activities=(ActivityRow(label="fetch", status="completed", duration=0.63),),
                    )
                ]
            )
            await pilot.pause()
            content = _render_content(box.content)
            assert "0.6s" in content
            assert "✔" in content

    asyncio.run(scenario())


def test_pipeline_box_shimmers_a_running_steps_name_purely_from_its_own_interval_tick() -> None:
    """Regression test: `PipelineBox.on_mount` used to wire its 60fps timer to
    `self.refresh`, which repaints whatever renderable is already stored but never calls
    `gradient_text` again -- so a running step's shimmer only ever moved on a *real*
    `update_rows` call (see `_animate_shimmer`'s own docstring). It's now wired to
    `_animate_shimmer`, which reruns `render_rows_live` (and therefore `gradient_text`,
    phased by `time.monotonic()`) on every tick. Proven here by waiting real wall-clock time
    with *no* `update_rows` call at all and checking the rendered color escape codes for the
    running row's name actually change -- `_render_content`'s `color_system=None` can't see
    this, so this test renders with real color on instead."""

    async def scenario() -> None:
        app = _HostApp([StepRow(name="RebaseStep", status="running", duration=0.0)])
        async with app.run_test() as pilot:
            await pilot.pause()
            box = app.query_one(PipelineBox)

            def _colored_render() -> str:
                buffer = StringIO()
                console = Console(
                    file=buffer, force_terminal=True, width=80, color_system="truecolor"
                )
                console.print(box.content)
                return buffer.getvalue()

            first = _colored_render()
            changed = False
            for _ in range(60):
                await pilot.pause()
                await asyncio.sleep(0.02)
                if _colored_render() != first:
                    changed = True
                    break

            assert changed
            # Purely a repaint -- update_rows was never called, so the underlying data
            # (name/status) is unchanged, only the shimmer's per-character color. Checked
            # via the plain (no-color) render, not `_colored_render()` -- the shimmer wraps
            # every character in its own color escape sequence, so the literal substring
            # "RebaseStep" can never appear contiguously in colored output at all.
            assert "RebaseStep" in _render_content(box.content)

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


def test_render_findings_accepts_a_test_sufficiency_output() -> None:
    output = TestSufficiencyOutput(
        findings=[
            Finding(
                severity="error",
                description="no test covers the new retry path",
                review_scope="source",
                location="tests/test_foo.py:10",
            ),
            Finding(severity="warning", description="manual check only", review_scope="source"),
        ],
        tested=["retry path"],
        testing_summary="mostly covered",
        artifacts=[],
    )

    assert render_findings(output) == (
        "error: no test covers the new retry path (tests/test_foo.py:10)\n"
        "warning: manual check only\n"
        "\n"
        "1 error, 1 warning, 0 info"
    )


# --- FindingsBox, mounted and driven through Pilot ----------------------------------------


class _FindingsHostApp(App[None]):
    """Minimal host app: mounts one `FindingsBox` so `Pilot` can drive it directly,
    independent of `ReviewApp`'s event-consuming worker."""

    def __init__(self, output: ReviewOutput | TestSufficiencyOutput) -> None:
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


def test_findings_box_renders_a_test_sufficiency_output_on_mount() -> None:
    async def scenario() -> None:
        output = TestSufficiencyOutput(
            findings=[
                Finding(
                    severity="warning",
                    description="no test covers the retry path",
                    review_scope="source",
                )
            ],
            tested=[],
            testing_summary="mostly fine",
            artifacts=[],
        )
        app = _FindingsHostApp(output)
        async with app.run_test() as pilot:
            await pilot.pause()
            box = app.query_one(FindingsBox)
            assert box.content == render_findings(output)
            assert box.content == (
                "warning: no test covers the retry path\n\n0 error, 1 warning, 0 info"
            )

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


# --- StatusBox, mounted and driven through Pilot ------------------------------------------


class _StatusHostApp(App[None]):
    """Minimal host app: mounts one `StatusBox` so `Pilot` can drive it directly,
    independent of `ReviewApp`'s event-consuming worker."""

    def __init__(self, message: str) -> None:
        super().__init__()
        self._initial_message = message

    def compose(self) -> ComposeResult:
        yield StatusBox(self._initial_message)


def test_status_box_renders_its_initial_message_on_mount() -> None:
    async def scenario() -> None:
        app = _StatusHostApp("Pipeline ran successfully.\n\nPress 'e' to exit.")
        async with app.run_test() as pilot:
            await pilot.pause()
            box = app.query_one(StatusBox)
            assert box.content == "Pipeline ran successfully.\n\nPress 'e' to exit."

    asyncio.run(scenario())


def test_status_box_update_status_replaces_the_rendered_content() -> None:
    async def scenario() -> None:
        app = _StatusHostApp("Pipeline ran successfully.\n\nPress 'e' to exit.")
        async with app.run_test() as pilot:
            await pilot.pause()
            box = app.query_one(StatusBox)

            box.update_status("Pipeline failed: boom.\n\nPress 'e' to exit.")
            await pilot.pause()

            assert box.content == "Pipeline failed: boom.\n\nPress 'e' to exit."

    asyncio.run(scenario())


def test_status_box_has_a_status_border_title() -> None:
    async def scenario() -> None:
        app = _StatusHostApp("Pipeline ran successfully.\n\nPress 'e' to exit.")
        async with app.run_test() as pilot:
            await pilot.pause()
            box = app.query_one(StatusBox)
            assert box.border_title == "Status"

    asyncio.run(scenario())
