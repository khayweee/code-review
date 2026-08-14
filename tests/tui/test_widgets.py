"""Widget-level tests for `PipelineBox`/`FindingsList`, driven with Textual's
`Pilot`/`run_test()`.

`render_rows`/`format_row`/`format_duration`/`format_finding` are exercised directly for
the pure formatting rules; `PipelineBox`/`FindingsList` themselves are mounted in a
minimal `App` and driven through `run_test()` to prove `update_rows`/`update_findings` --
and, for `FindingsList`, the parked-mode interactive decision flow (issue #87) -- actually
reach the rendered widget content.
"""

from __future__ import annotations

import asyncio
from io import StringIO

import pytest
from rich.console import Console
from rich.spinner import Spinner
from textual.app import App, ComposeResult
from textual.color import Color
from textual.widgets import Input, ListItem, Static

from code_review.pipeline.findings import Finding
from code_review.pipeline.step import ApprovalResponse
from code_review.steps.review import ReviewOutput
from code_review.steps.test_sufficiency import TestSufficiencyOutput
from code_review.tui.state import ActivityRow, StepRow
from code_review.tui.widgets import Finding as FindingItem
from code_review.tui.widgets import (
    FindingsDescription,
    FindingsList,
    FindingsSuggestion,
    PipelineBox,
    StatusBox,
    _FindingsListView,
    _render_row,
    format_activity_row,
    format_duration,
    format_finding,
    format_row,
    gradient_text,
    render_custom_entry_line,
    render_decision_cycle,
    render_decision_cycle_head,
    render_description,
    render_rows,
    render_rows_live,
    render_suggestions_plain,
)


def _render_content(renderable: object) -> str:
    buffer = StringIO()
    console = Console(file=buffer, force_terminal=True, width=80, color_system=None)
    console.print(renderable)
    return buffer.getvalue().rstrip()


def _finding_rows_content(findings_list: FindingsList) -> list[str]:
    """Render every mounted `Finding` row's `FindingsDescription`+`FindingsSuggestion`
    content to plain text, one entry per row -- the `FindingsList`-equivalent of
    `_option_list_content`'s old per-`Option` rendering, since a `_FindingsListView` has
    no single renderable `.content` the way `_BorderedBox`-based widgets do.

    `FindingsSuggestion` is itself a small container (issue #92), not a single `Static`
    the way it was pre-#92 -- its own two `Static` children (every entry before the
    trailing `_CUSTOM_ENTRY`, then that entry's own line) are rendered and joined here,
    same as reading one shared `.content` used to. Whichever of those `Static`s is
    currently swapped out for a live `Input` (`FindingsSuggestion.ensure_input`)
    contributes nothing to this text -- exactly like the old sibling `_InlineApprovalChat`
    never appeared in this helper's output either, since it lived outside
    `FindingsSuggestion` entirely; a mounted chat's own state is asserted separately via
    `box.query_one(Input)` in every test that opens one."""

    rows = []
    for item in findings_list.query(FindingItem):
        description = _render_content(item.query_one(FindingsDescription).content)
        suggestion = item.query_one(FindingsSuggestion)
        suggestion_text = "\n".join(
            _render_content(static.content) for static in suggestion.query(Static)
        )
        rows.append(f"{description}\n{suggestion_text}")
    return rows


# --- pure formatting -------------------------------------------------------------------


def test_format_duration_renders_sub_minute_durations_with_one_decimal() -> None:
    assert format_duration(0.3) == "0.3s"
    assert format_duration(59.9) == "59.9s"


def test_format_duration_renders_minute_and_above_as_mm_ss() -> None:
    assert format_duration(60.0) == "1:00"
    assert format_duration(125.0) == "2:05"


@pytest.mark.parametrize(
    ("status", "icon"),
    [
        ("pending", "◌"),
        ("completed", "✔"),
        ("failed", "✘"),
        ("parked", "⏸"),
        ("skipped", "⏭"),
    ],
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


def test_format_row_appends_detail_after_the_duration_when_set() -> None:
    row = StepRow(
        name="Pull Request",
        status="completed",
        duration=1.2,
        detail="→ opened https://github.com/owner/repo/pull/42",
    )

    assert format_row(row) == "✔ Pull Request  1.2s  → opened https://github.com/owner/repo/pull/42"


def test_format_row_omits_detail_cleanly_when_unset() -> None:
    row = StepRow(name="Pull Request", status="completed", duration=1.2)

    assert format_row(row) == "✔ Pull Request  1.2s"


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


def test_format_activity_row_appends_detail_after_the_duration_when_set() -> None:
    failed = ActivityRow(label="git fetch origin", status="failed", duration=1.2, detail="exit 1")

    assert format_activity_row(failed, is_last=True) == "  └  ✘ git fetch origin  1.2s  exit 1"


def test_format_activity_row_omits_detail_cleanly_when_unset() -> None:
    completed = ActivityRow(label="rebase", status="completed", duration=3.4)

    assert format_activity_row(completed, is_last=True) == "  └  ✔ rebase  3.4s"


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
    uses -- the live pipeline view renders a completed/failed/parked/skipped row's icon as
    a solid dot, colored by status, so status reads at a glance from color rather than
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
    parked_icon, _ = _render_row(
        StepRow(name="RebaseStep", status="parked", duration=0.1), spinners
    )
    skipped_icon, _ = _render_row(
        StepRow(name="RebaseStep", status="skipped", duration=0.1), spinners
    )
    pending_icon, _ = _render_row(
        StepRow(name="ReviewStep", status="pending", duration=None), spinners
    )

    assert completed_icon.plain == "●"
    assert completed_icon.style == "#5fafff"
    assert failed_icon.plain == "●"
    assert failed_icon.style == "#bb6400"
    assert parked_icon.plain == "●"
    assert parked_icon.style == "#d7af00"
    assert skipped_icon.plain == "●"
    assert skipped_icon.style == "#8a8a8a"
    # Pending keeps the plain, uncolored hollow-ring glyph -- only the four statuses above
    # get a colored dot.
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


def test_render_row_appends_detail_after_the_duration_when_set() -> None:
    spinners: dict[str, Spinner] = {}

    _, text = _render_row(
        StepRow(
            name="Pull Request",
            status="completed",
            duration=1.2,
            detail="→ opened https://github.com/owner/repo/pull/42",
        ),
        spinners,
    )

    assert text.plain == "Pull Request  1.2s  → opened https://github.com/owner/repo/pull/42"


def test_render_row_omits_detail_cleanly_when_unset() -> None:
    spinners: dict[str, Spinner] = {}

    _, text = _render_row(StepRow(name="Pull Request", status="completed", duration=1.2), spinners)

    assert text.plain == "Pull Request  1.2s"


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
            assert any(frame in content for frame in "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏")

    asyncio.run(scenario())


def test_pipeline_box_reuses_the_same_spinner_instance_while_a_step_keeps_running() -> None:
    """Regression test: `update_rows` used to build a *fresh* `rich.spinner.Spinner` on
    every call. A `Spinner`'s animation clock (`start_time`) is set lazily on its own
    first `render()` call and never reset after that, so a fresh instance on every call
    (as `ReviewApp`'s own periodic full-render tick would trigger) reset that clock back to
    "now" each time and the frame never advanced far enough to look animated. `PipelineBox`
    must reuse the *same* `Spinner` object for a running step across repeated
    `update_rows` calls -- checked here directly via `PipelineBox._spinners`'s cache,
    rather than via rendered frame content, since Rich's own render-time jitter makes frame
    content an unreliable signal for this specific invariant.
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


def test_pipeline_box_animate_shimmer_repaints_a_running_steps_name_from_current_time() -> None:
    """Regression test: `PipelineBox.on_mount` used to wire its own 60fps timer to
    `self.refresh`, which repaints whatever renderable is already stored but never calls
    `gradient_text` again -- so a running step's shimmer only ever moved on a *real*
    `update_rows` call (see `animate_shimmer`'s own docstring). `PipelineBox` no longer
    owns a timer of its own at all -- `ReviewApp`'s single tick timer calls
    `animate_shimmer` directly now (see `app.py`'s module docstring for why there is
    exactly one timer, not one per widget) -- so this drives it the same way, calling it
    directly rather than relying on a timer this widget would need to own itself. Proven
    here by waiting real wall-clock time between calls, with *no* `update_rows` call at
    all, and checking the rendered color escape codes for the running row's name actually
    change -- `_render_content`'s `color_system=None` can't see this, so this test renders
    with real color on instead."""

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
                await asyncio.sleep(0.02)
                box.animate_shimmer()
                await pilot.pause()
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
            assert box.border_title == "Agentic Code-Review Pipeline"

    asyncio.run(scenario())


# --- format_finding: pure formatting ------------------------------------------------------


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


# --- render_description/render_suggestions_plain/render_decision_cycle: pure formatting -


def test_render_description_includes_a_severity_dot_and_format_finding() -> None:
    finding = Finding(
        severity="warning",
        description="unclear naming",
        review_scope="source",
        location="widgets.py:42",
    )

    text = render_description(finding)

    assert text.plain == "● warning: unclear naming (widgets.py:42)"


def test_render_description_has_no_decision_marker_by_default() -> None:
    """issue #98: the default (`decision=None`) must render byte-for-byte identical to the
    pre-#98 output above, so every non-parked call site is unaffected."""

    finding = Finding(severity="warning", description="unclear naming", review_scope="source")

    assert render_description(finding).plain == render_description(finding, None).plain


def test_render_description_prefixes_a_fix_decided_marker() -> None:
    finding = Finding(severity="warning", description="unclear naming", review_scope="source")

    text = render_description(finding, "fix")

    assert text.plain == "✔ ● warning: unclear naming"


def test_render_description_prefixes_a_skip_decided_marker() -> None:
    finding = Finding(severity="warning", description="unclear naming", review_scope="source")

    text = render_description(finding, "skip")

    assert text.plain == "⏭ ● warning: unclear naming"


def test_render_suggestions_plain_joins_suggestions_one_per_line() -> None:
    finding = Finding(
        severity="warning",
        description="unclear naming",
        review_scope="source",
        suggestions=["rename it", "add a docstring"],
    )

    assert render_suggestions_plain(finding).plain == "rename it\nadd a docstring"


def test_render_suggestions_plain_is_empty_with_no_suggestions() -> None:
    finding = Finding(severity="info", description="fine as-is", review_scope="source")

    assert render_suggestions_plain(finding).plain == ""


def test_render_decision_cycle_labels_only_entry_0_as_recommended_when_it_is_a_suggestion() -> None:
    """Issue #91: a finding with its own `suggestions` gets " (Recommended)" appended to
    entry 0 only -- every other entry (further suggestions, `_CUSTOM_ENTRY`,
    `_DECISION_ENTRIES`) never carries that label, regardless of cursor position."""

    finding = Finding(
        severity="warning",
        description="unclear naming",
        review_scope="source",
        suggestions=["rename it", "add a docstring"],
    )

    text = render_decision_cycle(finding, decision_cursor=0)

    lines = text.plain.splitlines()
    assert "1. rename it (Recommended)" in lines[0]
    assert not any("(Recommended)" in line for line in lines[1:])


def test_render_decision_cycle_has_no_recommended_label_with_no_suggestions() -> None:
    """Entry 0 is `_CUSTOM_ENTRY` ("Chat about it") when a finding has no suggestions of
    its own -- it never earns the "(Recommended)" label either."""

    finding = Finding(severity="warning", description="unclear naming", review_scope="source")

    text = render_decision_cycle(finding, decision_cursor=0)

    assert "(Recommended)" not in text.plain
    assert "1. Chat about it" in text.plain


def test_render_decision_cycle_gives_the_one_fixed_entry_no_detail_line() -> None:
    """The one fixed entry (`_CUSTOM_ENTRY`) used to carry a short indented detail line
    ("Start typing to describe what you want.") -- removed outright, not just hidden, since
    it read as redundant with the entry's own label."""

    finding = Finding(severity="warning", description="unclear naming", review_scope="source")

    text = render_decision_cycle(finding, decision_cursor=0)

    assert "Start typing" not in text.plain
    assert text.plain == "> 1. Chat about it"


def test_render_decision_cycle_has_no_trailing_blank_line() -> None:
    """A `Text` ending in `"\\n"` renders an extra, otherwise-invisible empty line -- the
    actual root cause of the gap that used to appear above "Chat about it" in
    `FindingsSuggestion` (not a CSS margin). Entries are joined by `"\\n"`, not each
    terminated by one, so the last entry never dangles a trailing newline."""

    finding = Finding(
        severity="warning",
        description="unclear naming",
        review_scope="source",
        suggestions=["rename it"],
    )

    text = render_decision_cycle(finding, decision_cursor=0)

    assert not text.plain.endswith("\n")
    assert text.plain == "> 1. rename it (Recommended)\n  2. Chat about it"


def test_render_decision_cycle_head_excludes_the_trailing_custom_entry() -> None:
    """`FindingsSuggestion`'s `_entries` `Static` (issue #92) renders every entry except the
    trailing `_CUSTOM_ENTRY` -- that one is drawn separately (`render_custom_entry_line`, or
    a live `Input` once the chat is open), never duplicated here."""

    finding = Finding(
        severity="warning",
        description="unclear naming",
        review_scope="source",
        suggestions=["rename it", "add a docstring"],
    )

    text = render_decision_cycle_head(finding, decision_cursor=0)

    assert "1. rename it (Recommended)" in text.plain
    assert "2. add a docstring" in text.plain
    assert "Chat about it" not in text.plain


def test_render_decision_cycle_head_has_no_trailing_blank_line() -> None:
    """Same root-cause fix as `render_decision_cycle` above, for the head-only render
    `FindingsSuggestion.show_decision` actually uses."""

    finding = Finding(
        severity="warning",
        description="unclear naming",
        review_scope="source",
        suggestions=["rename it", "add a docstring"],
    )

    text = render_decision_cycle_head(finding, decision_cursor=0)

    assert not text.plain.endswith("\n")
    assert text.plain == "> 1. rename it (Recommended)\n  2. add a docstring"


def test_render_decision_cycle_head_is_empty_with_no_suggestions() -> None:
    """A finding with no suggestions of its own has only `_CUSTOM_ENTRY` in its decision
    cycle -- entirely excluded from the head, which is left with nothing to render."""

    finding = Finding(severity="warning", description="unclear naming", review_scope="source")

    text = render_decision_cycle_head(finding, decision_cursor=0)

    assert text.plain == ""


def test_render_custom_entry_line_marks_the_cursor_with_no_detail_line() -> None:
    """`render_custom_entry_line` renders exactly the one line `render_decision_cycle` would
    have rendered for `_CUSTOM_ENTRY` -- marked when the cursor is on it, plain otherwise --
    so `FindingsSuggestion` can show it standing alone, swapped for a live `Input` once a
    human opens the chat. No detail line, and no trailing newline of its own."""

    finding = Finding(
        severity="warning",
        description="unclear naming",
        review_scope="source",
        suggestions=["rename it"],
    )

    unmarked = render_custom_entry_line(finding, decision_cursor=0)
    assert unmarked.plain == "  2. Chat about it"

    marked = render_custom_entry_line(finding, decision_cursor=1)
    assert marked.plain == "> 2. Chat about it"


# --- FindingsList, mounted and driven through Pilot ----------------------------------------


class _FindingsHostApp(App[None]):
    """Minimal host app: mounts one `FindingsList` so `Pilot` can drive it directly,
    independent of `ReviewApp`'s event-consuming worker."""

    def __init__(
        self, output: ReviewOutput | TestSufficiencyOutput | list[Finding], step_name: str
    ) -> None:
        super().__init__()
        self._initial_output = output
        self._step_name = step_name

    def compose(self) -> ComposeResult:
        yield FindingsList(self._initial_output, self._step_name)


def test_findings_list_view_rejects_a_non_finding_child() -> None:
    """`_FindingsListView.__init__` asserts every mounted child is a `Finding` -- `ListView`
    itself assumes this (see that class's docstring) and would otherwise fail silently,
    far from the actual mistake."""

    owner = FindingsList(
        ReviewOutput(findings=[], risk_level="low", risk_rationale="fine"), "ReviewStep"
    )

    with pytest.raises(AssertionError):
        _FindingsListView(ListItem(Static("not a finding")), owner=owner)


def test_findings_list_renders_its_initial_findings_on_mount() -> None:
    async def scenario() -> None:
        output = ReviewOutput(
            findings=[
                Finding(severity="warning", description="unclear naming", review_scope="source")
            ],
            risk_level="low",
            risk_rationale="fine",
        )
        app = _FindingsHostApp(output, "ReviewStep")
        async with app.run_test() as pilot:
            await pilot.pause()
            box = app.query_one(FindingsList)
            lines = _finding_rows_content(box)
            assert len(lines) == 1
            assert "warning: unclear naming" in lines[0]
            assert box.query_one("#findings-summary", Static).content == (
                "0 error, 1 warning, 0 info"
            )

    asyncio.run(scenario())


def test_findings_list_highlights_index_0_by_default_and_shows_only_its_suggestions() -> None:
    async def scenario() -> None:
        output = ReviewOutput(
            findings=[
                Finding(
                    severity="warning",
                    description="first finding",
                    review_scope="source",
                    suggestions=["fix the first one"],
                ),
                Finding(
                    severity="error",
                    description="second finding",
                    review_scope="source",
                    suggestions=["fix the second one"],
                ),
            ],
            risk_level="high",
            risk_rationale="bad",
        )
        app = _FindingsHostApp(output, "ReviewStep")
        async with app.run_test() as pilot:
            await pilot.pause()
            box = app.query_one(FindingsList)
            list_view = box.query_one(_FindingsListView)
            assert list_view.index == 0
            lines = _finding_rows_content(box)
            assert "fix the first one" in lines[0]
            assert "fix the second one" not in lines[1]

    asyncio.run(scenario())


# def test_findings_list_highlighted_row_recolors_text_with_no_background_fill() -> None:
#     """The highlighted row must not get a solid background fill -- Textual's own two
#     built-in `ListView` highlight rules (`_list_view.py`'s blurred `& > ListItem.-highlight`
#     and focused `&:focus { & > ListItem.-highlight }`) are overridden (`finding.tcss`) so
#     only the text recolors, to this box's own border color (`$primary`), in both focus
#     states. `FindingsDescription`'s own rendered text is checked too, not just `Finding`'s
#     own `styles.color` -- overriding `color` on `Finding` alone is not sufficient (see
#     `finding.tcss`'s own comment on Textual's `auto-color` companion property)."""

#     async def scenario() -> None:
#         output = ReviewOutput(
#             findings=[
#                 Finding(severity="warning", description="first finding", review_scope="source"),
#                 Finding(severity="error", description="second finding", review_scope="source"),
#             ],
#             risk_level="low",
#             risk_rationale="fine",
#         )
#         app = _FindingsHostApp(output, "ReviewStep")
#         async with app.run_test() as pilot:
#             await pilot.pause()
#             box = app.query_one(FindingsList)
#             list_view = box.query_one(_FindingsListView)
#             rows = list(box.query(FindingItem))
#             desc0 = rows[0].query_one(FindingsDescription)
#             desc1 = rows[1].query_one(FindingsDescription)
#             primary = Color.parse(app.get_css_variables()["primary"])

#             assert list_view.has_focus
#             assert rows[0].styles.background.a == 0
#             assert rows[1].styles.background.a == 0
#             assert rows[0].styles.color == primary
#             assert Color.from_rich_color(desc0.rich_style.color) == primary
#             assert Color.from_rich_color(desc1.rich_style.color) != primary

#             # The focused default rule is the one whose `color` this ticket's own
#             # verification found hardest to beat (see `finding.tcss`) -- prove the blurred
#             # state independently rather than assuming it behaves the same way.
#             app.set_focus(None)
#             await pilot.pause()
#             assert not list_view.has_focus
#             assert rows[0].styles.background.a == 0
#             assert Color.from_rich_color(desc0.rich_style.color) == primary

#     asyncio.run(scenario())


def test_findings_suggestion_has_a_suggestion_border_title() -> None:
    """`FindingsSuggestion.border_title` is set directly in `__init__`, the same mechanism
    `PipelineBox`/`FindingsList`/`StatusBox` already use -- it only actually renders once a
    border is drawn (the `-visible` class), but the attribute itself is set unconditionally,
    same as those other widgets' own `border_title`."""

    async def scenario() -> None:
        output = ReviewOutput(
            findings=[
                Finding(severity="warning", description="unclear naming", review_scope="source")
            ],
            risk_level="low",
            risk_rationale="fine",
        )
        app = _FindingsHostApp(output, "ReviewStep")
        async with app.run_test() as pilot:
            await pilot.pause()
            box = app.query_one(FindingsList)
            suggestion = box.query_one(FindingsSuggestion)
            assert suggestion.border_title == "Suggestion"

    asyncio.run(scenario())


def test_findings_suggestion_custom_entry_is_styled_a_muted_gray_distinct_from_entries() -> None:
    """ "Chat about it" (`self._custom`, the `.-custom-entry` class) should read as "type
    your own", not another agent-generated suggestion -- styled `$fg-secondary`
    (`findings_suggestion.tcss`/`tokens.tcss`), distinct from the plain-foreground
    suggestion entries above it in `self._entries`."""

    async def scenario() -> None:
        output = ReviewOutput(
            findings=[
                Finding(
                    severity="warning",
                    description="unclear naming",
                    review_scope="source",
                    suggestions=["rename it"],
                )
            ],
            risk_level="low",
            risk_rationale="fine",
        )
        app = _FindingsHostApp(output, "ReviewStep")
        async with app.run_test() as pilot:
            await pilot.pause()
            box = app.query_one(FindingsList)
            task = asyncio.ensure_future(box.await_decision())
            await pilot.pause()

            suggestion = box.query_one(FindingsSuggestion)
            entries_static, custom_static = suggestion.query(Static)

            assert custom_static.has_class("-custom-entry")
            # This node sets its own `color` directly, distinct from `_entries`, which has
            # no `color` rule of its own at all (it inherits the highlighted row's own
            # foreground, at full alpha).
            assert custom_static.styles.has_rule("color")
            assert custom_static.styles.color == Color.parse("#949494")
            assert not entries_static.styles.has_rule("color")

            box._quick_decision("abort")
            await task

    asyncio.run(scenario())


def test_findings_suggestion_custom_entry_divider_only_shows_in_decision_mode() -> None:
    """`self._custom`'s `-decision` class (`findings_suggestion.tcss`'s
    `.-custom-entry.-decision` `border-top` divider) must only be present while decision
    mode is actually showing a "Chat about it" entry -- plain mode and a hidden/cleared row
    have no such entry to divide from."""

    async def scenario() -> None:
        output = ReviewOutput(
            findings=[
                Finding(
                    severity="warning",
                    description="unclear naming",
                    review_scope="source",
                    suggestions=["rename it"],
                )
            ],
            risk_level="low",
            risk_rationale="fine",
        )
        app = _FindingsHostApp(output, "ReviewStep")
        async with app.run_test() as pilot:
            await pilot.pause()
            box = app.query_one(FindingsList)
            row = list(box.query(FindingItem))[0]
            suggestion = row.query_one(FindingsSuggestion)
            custom_static = suggestion.query(Static)[1]

            row.set_plain()
            assert not custom_static.has_class("-decision")

            row.set_decision()
            assert custom_static.has_class("-decision")

            row.set_hidden()
            assert not custom_static.has_class("-decision")

    asyncio.run(scenario())


def test_findings_list_arrow_key_down_moves_which_finding_shows_its_suggestions() -> None:
    async def scenario() -> None:
        output = ReviewOutput(
            findings=[
                Finding(
                    severity="warning",
                    description="first finding",
                    review_scope="source",
                    suggestions=["fix the first one"],
                ),
                Finding(
                    severity="error",
                    description="second finding",
                    review_scope="source",
                    suggestions=["fix the second one"],
                ),
            ],
            risk_level="high",
            risk_rationale="bad",
        )
        app = _FindingsHostApp(output, "ReviewStep")
        async with app.run_test() as pilot:
            await pilot.pause()
            box = app.query_one(FindingsList)
            box.query_one(_FindingsListView).focus()
            await pilot.pause()
            await pilot.press("down")
            await pilot.pause()

            list_view = box.query_one(_FindingsListView)
            assert list_view.index == 1
            lines = _finding_rows_content(box)
            assert "fix the first one" not in lines[0]
            assert "fix the second one" in lines[1]

    asyncio.run(scenario())


def test_findings_list_update_findings_preserves_a_browsed_to_highlight() -> None:
    """Regression test: `app.py`'s `_render` calls `update_findings` on every render tick
    regardless of whether the underlying output changed. A human who has arrowed down to
    browse a later finding's suggestions must not see the highlight snap back to finding 0
    on the very next redundant tick."""

    async def scenario() -> None:
        output = ReviewOutput(
            findings=[
                Finding(
                    severity="warning",
                    description="first finding",
                    review_scope="source",
                    suggestions=["fix the first one"],
                ),
                Finding(
                    severity="error",
                    description="second finding",
                    review_scope="source",
                    suggestions=["fix the second one"],
                ),
            ],
            risk_level="high",
            risk_rationale="bad",
        )
        app = _FindingsHostApp(output, "ReviewStep")
        async with app.run_test() as pilot:
            await pilot.pause()
            box = app.query_one(FindingsList)
            box.query_one(_FindingsListView).focus()
            await pilot.pause()
            await pilot.press("down")
            await pilot.pause()

            # Simulate two redundant render ticks with the exact same output.
            box.update_findings(output, "ReviewStep")
            box.update_findings(output, "ReviewStep")
            await pilot.pause()

            list_view = box.query_one(_FindingsListView)
            assert list_view.index == 1
            lines = _finding_rows_content(box)
            assert "fix the first one" not in lines[0]
            assert "fix the second one" in lines[1]

    asyncio.run(scenario())


def test_findings_list_update_findings_replaces_the_rendered_rows() -> None:
    async def scenario() -> None:
        initial = ReviewOutput(
            findings=[Finding(severity="info", description="first", review_scope="source")],
            risk_level="low",
            risk_rationale="fine",
        )
        app = _FindingsHostApp(initial, "ReviewStep")
        async with app.run_test() as pilot:
            await pilot.pause()
            box = app.query_one(FindingsList)

            updated = ReviewOutput(
                findings=[Finding(severity="error", description="second", review_scope="source")],
                risk_level="high",
                risk_rationale="bad",
            )
            box.update_findings(updated, "ReviewStep")
            await pilot.pause()

            lines = _finding_rows_content(box)
            assert len(lines) == 1
            assert "error: second" in lines[0]
            assert "first" not in "".join(lines)
            assert box.query_one("#findings-summary", Static).content == (
                "1 error, 0 warning, 0 info"
            )

    asyncio.run(scenario())


def test_findings_list_update_findings_growing_the_finding_count_keeps_the_old_highlight() -> None:
    """Regression test for the in-place reconciliation `update_findings` uses when the
    finding count changes: growing the list must not disturb the row -- or its mode/cursor
    -- that was already highlighted, only add the extra rows."""

    async def scenario() -> None:
        initial = ReviewOutput(
            findings=[Finding(severity="info", description="first", review_scope="source")],
            risk_level="low",
            risk_rationale="fine",
        )
        app = _FindingsHostApp(initial, "ReviewStep")
        async with app.run_test() as pilot:
            await pilot.pause()
            box = app.query_one(FindingsList)

            grown = ReviewOutput(
                findings=[
                    Finding(
                        severity="info",
                        description="first",
                        review_scope="source",
                        suggestions=["keep it"],
                    ),
                    Finding(
                        severity="error",
                        description="second",
                        review_scope="source",
                        suggestions=["fix it"],
                    ),
                ],
                risk_level="high",
                risk_rationale="bad",
            )
            box.update_findings(grown, "ReviewStep")
            await pilot.pause()

            list_view = box.query_one(_FindingsListView)
            assert list_view.index == 0
            lines = _finding_rows_content(box)
            assert len(lines) == 2
            assert "keep it" in lines[0]
            assert "fix it" not in lines[1]

    asyncio.run(scenario())


def test_findings_list_update_findings_shrinking_the_finding_count_clamps_the_highlight() -> None:
    """The other half of the in-place reconciliation regression above: shrinking the list
    below a browsed-to highlight must clamp it to the new last row, not leave it pointing
    past the end or silently snap back to a stale/removed row."""

    async def scenario() -> None:
        initial = ReviewOutput(
            findings=[
                Finding(
                    severity="info", description="first", review_scope="source", suggestions=["a"]
                ),
                Finding(
                    severity="error",
                    description="second",
                    review_scope="source",
                    suggestions=["b"],
                ),
            ],
            risk_level="high",
            risk_rationale="bad",
        )
        app = _FindingsHostApp(initial, "ReviewStep")
        async with app.run_test() as pilot:
            await pilot.pause()
            box = app.query_one(FindingsList)
            box.query_one(_FindingsListView).focus()
            await pilot.pause()
            await pilot.press("down")
            await pilot.pause()
            assert box.query_one(_FindingsListView).index == 1

            shrunk = ReviewOutput(
                findings=[
                    Finding(
                        severity="info",
                        description="first",
                        review_scope="source",
                        suggestions=["a"],
                    )
                ],
                risk_level="low",
                risk_rationale="fine",
            )
            box.update_findings(shrunk, "ReviewStep")
            await pilot.pause()

            list_view = box.query_one(_FindingsListView)
            assert list_view.index == 0
            lines = _finding_rows_content(box)
            assert len(lines) == 1
            assert "a" in lines[0]

    asyncio.run(scenario())


def test_findings_list_renders_a_test_sufficiency_output_on_mount() -> None:
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
        app = _FindingsHostApp(output, "TestSufficiencyStep")
        async with app.run_test() as pilot:
            await pilot.pause()
            box = app.query_one(FindingsList)
            lines = _finding_rows_content(box)
            assert "warning: no test covers the retry path" in lines[0]
            assert box.query_one("#findings-summary", Static).content == (
                "0 error, 1 warning, 0 info"
            )

    asyncio.run(scenario())


def test_findings_list_border_title_names_the_owning_step() -> None:
    async def scenario() -> None:
        output = ReviewOutput(findings=[], risk_level="low", risk_rationale="fine")
        app = _FindingsHostApp(output, "ReviewStep")
        async with app.run_test() as pilot:
            await pilot.pause()
            box = app.query_one(FindingsList)
            assert box.border_title == "Findings -- ReviewStep"

    asyncio.run(scenario())


def test_findings_list_update_findings_updates_the_border_title_to_the_new_step() -> None:
    async def scenario() -> None:
        initial = ReviewOutput(findings=[], risk_level="low", risk_rationale="fine")
        app = _FindingsHostApp(initial, "ReviewStep")
        async with app.run_test() as pilot:
            await pilot.pause()
            box = app.query_one(FindingsList)

            updated = TestSufficiencyOutput(
                findings=[], tested=[], testing_summary="fine", artifacts=[]
            )
            box.update_findings(updated, "TestSufficiencyStep")
            await pilot.pause()

            assert box.border_title == "Findings -- TestSufficiencyStep"

    asyncio.run(scenario())


def test_findings_list_accepts_a_bare_list_of_findings() -> None:
    async def scenario() -> None:
        findings = [
            Finding(
                severity="error", description="rebase left a conflict marker", review_scope="source"
            )
        ]
        app = _FindingsHostApp(findings, "RebaseStep")
        async with app.run_test() as pilot:
            await pilot.pause()
            box = app.query_one(FindingsList)
            lines = _finding_rows_content(box)
            assert "error: rebase left a conflict marker" in lines[0]
            assert box.query_one("#findings-summary", Static).content == (
                "1 error, 0 warning, 0 info"
            )

    asyncio.run(scenario())


# --- FindingsList, parked-mode interactive decision flow (issue #87, per-finding by #98) -


def test_findings_list_await_decision_populates_the_footer_hint_when_called_right_after_mount() -> (
    None
):
    """Regression test: `await_decision()` used to call `_set_footer_hint(True)` before
    awaiting `_await_list_view()`'s compose-settle retry -- calling it immediately after
    mount, with no intervening `await` of its own (the ordinary production shape: `app.py`'s
    `_relay_approval` calls `_render_findings()` -- which does the mount -- then
    `await_decision()` right after, in the same synchronous stretch of that one coroutine,
    the moment a step's very first park mounts a brand-new `FindingsList`), silently no-op'd
    via `_set_footer_hint`'s own `NoMatches` guard -- and, unlike `Finding`'s own
    `_apply_mode`-on-compose catch-up or the highlighted row's decision cycle/focus (both of
    which already awaited `_await_list_view()` before this fix), nothing else ever primed
    the footer again for the rest of that park, leaving it blank the whole time.

    Mounting `FindingsList` via `_FindingsHostApp.compose()` (every other test in this file)
    doesn't reproduce this: that box is composed as part of the app's own initial-screen
    startup, which `run_test()` already lets settle before a test body ever runs, so its
    `#findings-footer` already exists by the time any test calls `await_decision()` on it.
    This test instead mounts `FindingsList` dynamically, inside the same coroutine that
    immediately calls `await_decision()` on it with zero intervening `await` -- mirroring
    `_relay_approval`'s own shape exactly -- so `FindingsList.compose()` provably has not
    run by the time `await_decision()`'s first synchronous statements do."""

    class _EmptyHostApp(App[None]):
        def compose(self) -> ComposeResult:
            return
            yield  # pragma: no cover - makes this a generator function

    async def scenario() -> None:
        output = ReviewOutput(
            findings=[
                Finding(severity="warning", description="unclear naming", review_scope="source")
            ],
            risk_level="low",
            risk_rationale="fine",
        )
        app = _EmptyHostApp()

        async def _mount_and_park() -> ApprovalResponse:
            box = FindingsList(output, "ReviewStep")
            # no `await` between this and `await_decision()` below
            app.mount(box)
            return await box.await_decision()

        async with app.run_test() as pilot:
            await pilot.pause()
            task = asyncio.ensure_future(_mount_and_park())
            await pilot.pause()

            box = app.query_one(FindingsList)
            footer = box.query_one("#findings-footer", Static)
            assert footer.content != ""

            box._quick_decision("abort")
            await task

    asyncio.run(scenario())


def test_findings_list_await_decision_marks_the_highlighted_row_s_decision_cursor() -> None:
    async def scenario() -> None:
        output = ReviewOutput(
            findings=[
                Finding(
                    severity="warning",
                    description="unclear naming",
                    review_scope="source",
                    suggestions=["rename it"],
                )
            ],
            risk_level="low",
            risk_rationale="fine",
        )
        app = _FindingsHostApp(output, "ReviewStep")
        async with app.run_test() as pilot:
            await pilot.pause()
            box = app.query_one(FindingsList)
            task = asyncio.ensure_future(box.await_decision())
            await pilot.pause()

            lines = _finding_rows_content(box)
            assert "> 1. rename it" in lines[0]

            box._quick_decision("abort")
            await task

    asyncio.run(scenario())


def test_findings_list_plain_up_down_highlighting_never_auto_opens_the_chat_while_parked() -> None:
    """Arrow-key-*browsing between finding rows* (`up`/`down`, `ListView`'s own built-in
    cursor movement -- distinct from `left`/`right`/digit keys, which move a row's own
    `_decision_cursor`) must never yank focus into the inline chat, even when the newly
    highlighted finding has zero suggestions of its own (so `_CUSTOM_ENTRY` is that row's
    entry 0). `on_list_view_highlighted`/`_prime_highlighted` call `reset_decision()`/
    `set_decision()` directly, never `_cycle_decision`/`_jump_decision` -- only a deliberate
    intra-row cursor move auto-opens the chat, per those methods' own docstrings."""

    async def scenario() -> None:
        output = ReviewOutput(
            findings=[
                Finding(
                    severity="warning",
                    description="first finding",
                    review_scope="source",
                    suggestions=["rename it"],
                ),
                Finding(
                    severity="error",
                    description="second finding, no suggestions",
                    review_scope="source",
                ),
            ],
            risk_level="high",
            risk_rationale="bad",
        )
        app = _FindingsHostApp(output, "ReviewStep")
        async with app.run_test() as pilot:
            await pilot.pause()
            box = app.query_one(FindingsList)
            box.query_one(_FindingsListView).focus()
            task = asyncio.ensure_future(box.await_decision())
            await pilot.pause()

            await pilot.press("down")
            await pilot.pause()

            lines = _finding_rows_content(box)
            # The newly highlighted (second) row's only entry is `_CUSTOM_ENTRY` at cursor
            # 0 -- yet no chat opened from merely browsing onto it.
            assert "> 1. Chat about it" in lines[1]
            assert not list(box.query(Input))

            box._quick_decision("abort")
            await task

    asyncio.run(scenario())


def test_findings_list_arrow_keys_cycle_the_decision_cursor_while_parked() -> None:
    """Only two entries remain for a finding with one suggestion (that suggestion, then
    `_CUSTOM_ENTRY`), so one right press already lands on "Chat about it" and auto-opens
    the inline chat -- see `test_findings_list_cursor_arriving_at_chat_about_it_auto_opens_
    and_focuses_the_input` below for a dedicated proof of that. This test uses a finding
    with two suggestions instead, so the first right press stays on a plain suggestion (no
    chat yet) and only the second lands on "Chat about it"."""

    async def scenario() -> None:
        output = ReviewOutput(
            findings=[
                Finding(
                    severity="warning",
                    description="unclear naming",
                    review_scope="source",
                    suggestions=["rename it", "add a docstring"],
                )
            ],
            risk_level="low",
            risk_rationale="fine",
        )
        app = _FindingsHostApp(output, "ReviewStep")
        async with app.run_test() as pilot:
            await pilot.pause()
            box = app.query_one(FindingsList)
            box.query_one(_FindingsListView).focus()
            task = asyncio.ensure_future(box.await_decision())
            await pilot.pause()

            await pilot.press("right")
            await pilot.pause()
            lines = _finding_rows_content(box)
            assert "> 2. add a docstring" in lines[0]
            assert not list(box.query(Input))

            await pilot.press("right")
            await pilot.pause()
            # Landing on `_CUSTOM_ENTRY` (issue #92) replaces its plain numbered line with a
            # live `Input` in place, inside the highlighted row's own `FindingsSuggestion` --
            # so the marked text itself is gone from `lines[0]`, superseded by the `Input`'s
            # own placeholder (see `test_findings_list_cursor_arriving_at_chat_about_it_via_
            # digit_jump_auto_opens_it` for a dedicated proof of the placeholder/focus).
            lines = _finding_rows_content(box)
            assert "Chat about it" not in lines[0]
            chat_input = box.query_one(Input)
            assert chat_input.value == ""
            assert chat_input.placeholder == "Chat about it"

            box._quick_decision("abort")
            await task

    asyncio.run(scenario())


def test_findings_list_enter_confirms_the_cursor_and_resolves_the_pending_decision() -> None:
    """Approve/skip are gone entirely, so confirming the cursor now always resolves via the
    inline chat (`decision="fix"`), never directly -- this reworks the old "resolves via
    approve" scenario around that, submitting the auto-opened chat's `Input` to actually
    resolve the pending future."""

    async def scenario() -> None:
        output = ReviewOutput(
            findings=[
                Finding(severity="warning", description="unclear naming", review_scope="source")
            ],
            risk_level="low",
            risk_rationale="fine",
        )
        app = _FindingsHostApp(output, "ReviewStep")
        async with app.run_test() as pilot:
            await pilot.pause()
            box = app.query_one(FindingsList)
            box.query_one(_FindingsListView).focus()
            task = asyncio.ensure_future(box.await_decision())
            await pilot.pause()

            # No suggestions here -- cursor starts on "Chat about it" (entry 0), so it's
            # already auto-opened by `await_decision`'s own initial `set_decision()`... but
            # that priming call doesn't auto-open (only a deliberate cursor move does, see
            # `_cycle_decision`'s docstring) -- Enter is what actually opens it here.
            await pilot.press("enter")
            await pilot.pause()
            box.query_one(Input).value = "looks good, thanks"
            await pilot.press("enter")
            await pilot.pause()

            response = await task
            assert response == ApprovalResponse(decision="fix", instructions="looks good, thanks")

    asyncio.run(scenario())


def test_findings_list_digit_shortcut_jumps_the_decision_cursor_while_parked() -> None:
    async def scenario() -> None:
        output = ReviewOutput(
            findings=[
                Finding(
                    severity="warning",
                    description="unclear naming",
                    review_scope="source",
                    suggestions=["rename it", "add a docstring"],
                )
            ],
            risk_level="low",
            risk_rationale="fine",
        )
        app = _FindingsHostApp(output, "ReviewStep")
        async with app.run_test() as pilot:
            await pilot.pause()
            box = app.query_one(FindingsList)
            box.query_one(_FindingsListView).focus()
            task = asyncio.ensure_future(box.await_decision())
            await pilot.pause()

            await pilot.press("2")
            await pilot.pause()
            lines = _finding_rows_content(box)
            assert "> 2. add a docstring" in lines[0]
            # Landed on a plain suggestion, not `_CUSTOM_ENTRY` -- no auto-opened chat.
            assert not list(box.query(Input))

            box._quick_decision("abort")
            await task

    asyncio.run(scenario())


def test_findings_list_digit_shortcut_past_the_entry_count_is_a_no_op() -> None:
    async def scenario() -> None:
        output = ReviewOutput(
            findings=[
                Finding(
                    severity="warning",
                    description="unclear naming",
                    review_scope="source",
                    suggestions=["rename it"],
                )
            ],
            risk_level="low",
            risk_rationale="fine",
        )
        app = _FindingsHostApp(output, "ReviewStep")
        async with app.run_test() as pilot:
            await pilot.pause()
            box = app.query_one(FindingsList)
            box.query_one(_FindingsListView).focus()
            task = asyncio.ensure_future(box.await_decision())
            await pilot.pause()

            # Only 2 entries exist (1 suggestion + "Chat about it").
            await pilot.press("9")
            await pilot.pause()
            lines = _finding_rows_content(box)
            assert "> 1. rename it" in lines[0]

            box._quick_decision("abort")
            await task

    asyncio.run(scenario())


def test_findings_list_cursor_arriving_at_chat_about_it_via_digit_jump_auto_opens_it() -> None:
    """A digit-key jump landing on `_CUSTOM_ENTRY` auto-opens and focuses the inline chat's
    `Input`, with no further keypress -- `FindingsList._jump_decision`'s counterpart to
    `_cycle_decision`'s left/right behavior (see `test_findings_list_arrow_keys_cycle_the_
    decision_cursor_while_parked` above for the right-arrow case)."""

    async def scenario() -> None:
        output = ReviewOutput(
            findings=[
                Finding(
                    severity="warning",
                    description="unclear naming",
                    review_scope="source",
                    suggestions=["rename it"],
                )
            ],
            risk_level="low",
            risk_rationale="fine",
        )
        app = _FindingsHostApp(output, "ReviewStep")
        async with app.run_test() as pilot:
            await pilot.pause()
            box = app.query_one(FindingsList)
            box.query_one(_FindingsListView).focus()
            task = asyncio.ensure_future(box.await_decision())
            await pilot.pause()

            assert not list(box.query(Input))

            # Entry 2 ("Chat about it") is the digit-key jump's target here.
            await pilot.press("2")
            await pilot.pause()

            # As with the right-arrow case, `_CUSTOM_ENTRY`'s own marked line is replaced by
            # the live `Input` in place (issue #92), so it no longer shows up as text.
            lines = _finding_rows_content(box)
            assert "Chat about it" not in lines[0]
            chat_input = box.query_one(Input)
            assert chat_input.value == ""
            assert chat_input.has_focus

            box._quick_decision("abort")
            await task

    asyncio.run(scenario())


def test_findings_list_opening_the_chat_widget_twice_mounts_only_one() -> None:
    async def scenario() -> None:
        output = ReviewOutput(
            findings=[
                Finding(severity="warning", description="unclear naming", review_scope="source")
            ],
            risk_level="low",
            risk_rationale="fine",
        )
        app = _FindingsHostApp(output, "ReviewStep")
        async with app.run_test() as pilot:
            await pilot.pause()
            box = app.query_one(FindingsList)
            box.query_one(_FindingsListView).focus()
            task = asyncio.ensure_future(box.await_decision())
            await pilot.pause()

            box._open_chat("")
            box._open_chat("")
            await pilot.pause()

            assert len(box.query(Input)) == 1

            box._quick_decision("abort")
            await task

    asyncio.run(scenario())


def test_findings_list_s_shortcut_resolves_directly_while_parked() -> None:
    """ "s" resolves the park directly with `decision="skip"` via `action_quick_skip` -- a
    restored global escape hatch (not a listed per-finding entry, same treatment "x"/abort
    already gets), kept for a park the inline chat genuinely cannot resolve (e.g. a step
    that ignores the chat's `fix_round` instructions entirely -- see `tui/AGENTS.md`'s
    "Findings box" section). "a" (approve) has no equivalent escape hatch and stays gone --
    see `test_findings_list_a_shortcut_is_a_no_op_while_parked` below."""

    async def scenario() -> None:
        output = ReviewOutput(
            findings=[
                Finding(severity="warning", description="unclear naming", review_scope="source")
            ],
            risk_level="low",
            risk_rationale="fine",
        )
        app = _FindingsHostApp(output, "ReviewStep")
        async with app.run_test() as pilot:
            await pilot.pause()
            box = app.query_one(FindingsList)
            box.query_one(_FindingsListView).focus()
            task = asyncio.ensure_future(box.await_decision())
            await pilot.pause()

            await pilot.press("s")
            await pilot.pause()

            response = await task
            assert response == ApprovalResponse(decision="skip", instructions=None)

    asyncio.run(scenario())


def test_findings_list_a_shortcut_is_a_no_op_while_parked() -> None:
    """ "a" used to resolve the park directly (Approve) via `action_quick_approve` --
    removed entirely, not remapped to some other outcome, so pressing it while parked now
    does nothing at all: no chat opens, and the pending park is left unresolved (the abort
    below is what actually resolves it, so this test can complete)."""

    async def scenario() -> None:
        output = ReviewOutput(
            findings=[
                Finding(severity="warning", description="unclear naming", review_scope="source")
            ],
            risk_level="low",
            risk_rationale="fine",
        )
        app = _FindingsHostApp(output, "ReviewStep")
        async with app.run_test() as pilot:
            await pilot.pause()
            box = app.query_one(FindingsList)
            box.query_one(_FindingsListView).focus()
            task = asyncio.ensure_future(box.await_decision())
            await pilot.pause()

            await pilot.press("a")
            await pilot.pause()

            assert not task.done()
            assert not list(box.query(Input))

            box._quick_decision("abort")
            response = await task
            assert response == ApprovalResponse(decision="abort", instructions=None)

    asyncio.run(scenario())


def test_findings_list_f_shortcut_opens_the_inline_chat_widget_empty() -> None:
    async def scenario() -> None:
        output = ReviewOutput(
            findings=[
                Finding(severity="warning", description="unclear naming", review_scope="source")
            ],
            risk_level="low",
            risk_rationale="fine",
        )
        app = _FindingsHostApp(output, "ReviewStep")
        async with app.run_test() as pilot:
            await pilot.pause()
            box = app.query_one(FindingsList)
            box.query_one(_FindingsListView).focus()
            task = asyncio.ensure_future(box.await_decision())
            await pilot.pause()

            await pilot.press("f")
            await pilot.pause()

            assert box.query_one(Input).value == ""

            await pilot.press("enter")
            await pilot.pause()

            response = await task
            assert response == ApprovalResponse(decision="fix", instructions="")

    asyncio.run(scenario())


def test_findings_list_confirming_a_suggestion_records_it_as_the_fix_immediately() -> None:
    """Confirming a real suggestion (cursor on an entry from `finding.suggestions`, not
    `_CUSTOM_ENTRY`) records it as the fix verbatim in one Enter, with no intermediate chat
    `Input` step -- a suggestion's own text already is the human's chosen instructions the
    moment they confirm it. Only `_CUSTOM_ENTRY` ("Chat about it") opens a chat, since it has
    no text of its own to record without one -- see
    `test_findings_list_f_shortcut_opens_the_inline_chat_widget_empty` and
    `test_findings_list_enter_confirms_the_cursor_and_resolves_the_pending_decision` for that
    path."""

    async def scenario() -> None:
        output = ReviewOutput(
            findings=[
                Finding(
                    severity="warning",
                    description="unclear naming",
                    review_scope="source",
                    suggestions=["rename it"],
                )
            ],
            risk_level="low",
            risk_rationale="fine",
        )
        app = _FindingsHostApp(output, "ReviewStep")
        async with app.run_test() as pilot:
            await pilot.pause()
            box = app.query_one(FindingsList)
            box.query_one(_FindingsListView).focus()
            task = asyncio.ensure_future(box.await_decision())
            await pilot.pause()

            # confirms the cursor at index 0: "rename it"
            await pilot.press("enter")
            await pilot.pause()

            assert not list(box.query(Input))

            response = await task
            assert response == ApprovalResponse(decision="fix", instructions="rename it")

    asyncio.run(scenario())


def test_findings_list_letter_shortcuts_are_no_ops_while_not_parked() -> None:
    """ "a" has no binding at all anymore (approve was removed for good); "s" is bound again
    (`action_quick_skip`, a restored global escape hatch alongside "x") but its handler,
    `FindingsList._quick_decision`, itself no-ops outside a park -- so pressing either
    outside a park does nothing, same as every other interactive binding here, just via two
    different mechanisms."""

    async def scenario() -> None:
        output = ReviewOutput(
            findings=[
                Finding(severity="warning", description="unclear naming", review_scope="source")
            ],
            risk_level="low",
            risk_rationale="fine",
        )
        app = _FindingsHostApp(output, "ReviewStep")
        async with app.run_test() as pilot:
            await pilot.pause()
            box = app.query_one(FindingsList)
            box.query_one(_FindingsListView).focus()

            await pilot.press("a")
            await pilot.pause()
            await pilot.press("s")
            await pilot.pause()

            assert box._pending is None
            assert not list(app.query(Input))

    asyncio.run(scenario())


def test_findings_list_update_findings_preserves_the_decision_cursor_across_a_redundant_tick() -> (
    None
):
    """Strengthens the old `FindingsBox` regression above: while parked, an in-progress
    per-row `_decision_cursor` must survive a same-length redundant `update_findings` tick
    untouched, exactly like a browsed-to highlight already does outside a park."""

    async def scenario() -> None:
        output = ReviewOutput(
            findings=[
                Finding(
                    severity="warning",
                    description="unclear naming",
                    review_scope="source",
                    suggestions=["rename it", "add a docstring"],
                )
            ],
            risk_level="low",
            risk_rationale="fine",
        )
        app = _FindingsHostApp(output, "ReviewStep")
        async with app.run_test() as pilot:
            await pilot.pause()
            box = app.query_one(FindingsList)
            box.query_one(_FindingsListView).focus()
            task = asyncio.ensure_future(box.await_decision())
            await pilot.pause()

            await pilot.press("right")
            await pilot.pause()
            lines = _finding_rows_content(box)
            assert "> 2. add a docstring" in lines[0]

            # Simulate two redundant render ticks with the exact same output while parked.
            box.update_findings(output, "ReviewStep")
            box.update_findings(output, "ReviewStep")
            await pilot.pause()

            lines = _finding_rows_content(box)
            assert "> 2. add a docstring" in lines[0]

            box._quick_decision("abort")
            await task

    asyncio.run(scenario())


def test_findings_list_update_findings_preserves_a_mounted_chat_across_a_redundant_tick() -> None:
    """Strengthens the old `FindingsBox` regression above (which never mounted a widget it
    had to survive a rebuild): a mounted chat `Input` -- and whatever a human has already
    typed into it -- must survive a same-length redundant `update_findings` tick untouched.

    Issue #92 moved this `Input` from a sibling of `_FindingsListView` (untouched by
    `update_findings` regardless of what that method did) into the highlighted row's own
    `FindingsSuggestion` -- which *is* reached by `update_findings`' per-row
    `update_finding` -> `_render_suggestion` -> `show_decision` on every tick, redundant or
    not. This is the load-bearing correctness requirement of that move: `show_decision`
    must recognize an already-open `Input` and leave it alone rather than reconstruct it,
    or a human's in-progress typed text would be silently wiped on the very next periodic
    render (`app.py`'s `_on_tick` calls `_render` regardless of whether the output actually
    changed, on every `_FULL_RENDER_EVERY_TICKS`th tick). Asserts the `Input` is found
    specifically inside the highlighted row's own `FindingsSuggestion` (not merely
    "somewhere in the box"), so this
    proves the *new* in-place location, not just that a chat still exists somewhere."""

    async def scenario() -> None:
        output = ReviewOutput(
            findings=[
                Finding(severity="warning", description="unclear naming", review_scope="source")
            ],
            risk_level="low",
            risk_rationale="fine",
        )
        app = _FindingsHostApp(output, "ReviewStep")
        async with app.run_test() as pilot:
            await pilot.pause()
            box = app.query_one(FindingsList)
            box.query_one(_FindingsListView).focus()
            task = asyncio.ensure_future(box.await_decision())
            await pilot.pause()

            box._open_chat("draft instructions")
            await pilot.pause()
            highlighted = box.query_one(_FindingsListView).highlighted_child
            assert isinstance(highlighted, FindingItem)
            suggestion = highlighted.query_one(FindingsSuggestion)
            assert suggestion.query_one(Input).value == "draft instructions"

            box.update_findings(output, "ReviewStep")
            box.update_findings(output, "ReviewStep")
            await pilot.pause()

            assert len(box.query(Input)) == 1
            assert suggestion.query_one(Input).value == "draft instructions"

            box._resolve_chat(suggestion.query_one(Input).value)
            response = await task
            assert response == ApprovalResponse(decision="fix", instructions="draft instructions")

    asyncio.run(scenario())


def test_findings_list_escape_cancels_the_chat_without_resolving_the_park() -> None:
    """Issue #95: Escape, while the chat `Input` has focus, tears the `Input` down and
    returns focus to `_FindingsListView` -- without resolving `self._pending`, so the
    human can go on to browse/act on other suggestions or findings, and the exact same
    park is still there to resolve afterwards."""

    async def scenario() -> None:
        output = ReviewOutput(
            findings=[
                Finding(
                    severity="warning",
                    description="unclear naming",
                    review_scope="source",
                    suggestions=["rename it"],
                )
            ],
            risk_level="low",
            risk_rationale="fine",
        )
        app = _FindingsHostApp(output, "ReviewStep")
        async with app.run_test() as pilot:
            await pilot.pause()
            box = app.query_one(FindingsList)
            box.query_one(_FindingsListView).focus()
            task = asyncio.ensure_future(box.await_decision())
            await pilot.pause()

            await pilot.press("f")
            await pilot.pause()
            box.query_one(Input).value = "draft instructions"

            await pilot.press("escape")
            await pilot.pause()

            assert not list(box.query(Input))
            assert box._pending is not None
            assert box._parked
            assert box.query_one(_FindingsListView).has_focus

            # The park is still open and resolvable exactly as before Escape.
            await pilot.press("s")
            response = await task
            assert response == ApprovalResponse(decision="skip", instructions=None)

    asyncio.run(scenario())


def test_findings_list_arrow_navigation_away_from_an_open_chat_keeps_the_list_focused() -> None:
    """Regression: `Input` doesn't bind up/down itself, so those keys bubble from a focused
    chat straight to `_FindingsListView`'s inherited `ListView` navigation -- moving the
    highlighted row while its chat is still open. That tears the focused `Input` down
    (`Finding.set_hidden` -> `FindingsSuggestion.clear`) with nothing to reclaim focus
    afterward, stranding it at `None`. Every parked-mode binding lives only on
    `_FindingsListView` (see its own docstring), so a stray `None` focus made the whole box
    permanently unresponsive to every key, including "s"/"x" -- reading as a hard hang."""

    async def scenario() -> None:
        output = ReviewOutput(
            findings=[
                Finding(severity="warning", description="first finding", review_scope="source"),
                Finding(severity="error", description="second finding", review_scope="source"),
            ],
            risk_level="high",
            risk_rationale="bad",
        )
        app = _FindingsHostApp(output, "ReviewStep")
        async with app.run_test() as pilot:
            await pilot.pause()
            box = app.query_one(FindingsList)
            list_view = box.query_one(_FindingsListView)
            list_view.focus()
            task = asyncio.ensure_future(box.await_decision())
            await pilot.pause()

            await pilot.press("f")  # opens row 0's chat and focuses its Input
            await pilot.pause()
            assert box.query_one(Input).has_focus

            # bubbles from the focused Input to the list view
            await pilot.press("down")
            await pilot.pause()
            assert list_view.index == 1
            assert list_view.has_focus

            # The box is still fully interactive: every remaining binding still resolves,
            # here deciding both rows in turn ("down" wrapped to row 0, still undecided).
            await pilot.press("s")
            await pilot.pause()
            assert list_view.index == 0
            await pilot.press("s")
            response = await task
            assert response.decision == "skip"

    asyncio.run(scenario())


# --- FindingsList, per-finding decisions and aggregation (issue #98) --------------------


def test_findings_list_recording_a_decision_does_not_resolve_a_multi_finding_park() -> None:
    """Confirming the highlighted row's chat records that row's own decision but leaves a
    multi-finding park open until every row has one -- superseding this box's pre-#98
    behavior, where any one row's confirm resolved the whole park immediately."""

    async def scenario() -> None:
        output = ReviewOutput(
            findings=[
                Finding(severity="warning", description="first finding", review_scope="source"),
                Finding(severity="error", description="second finding", review_scope="source"),
            ],
            risk_level="high",
            risk_rationale="bad",
        )
        app = _FindingsHostApp(output, "ReviewStep")
        async with app.run_test() as pilot:
            await pilot.pause()
            box = app.query_one(FindingsList)
            box.query_one(_FindingsListView).focus()
            task = asyncio.ensure_future(box.await_decision())
            await pilot.pause()

            await pilot.press("enter")  # opens the chat on row 0
            await pilot.pause()
            box.query_one(Input).value = "fix the first one"
            await pilot.press("enter")  # submits -- decides row 0 only
            await pilot.pause()

            assert not task.done()
            assert box._rows[0].is_decided()
            assert not box._rows[1].is_decided()

            box._quick_decision("abort")
            response = await task
            assert response == ApprovalResponse(decision="abort", instructions=None)

    asyncio.run(scenario())


def test_findings_list_recording_a_decision_advances_the_cursor_to_the_next_undecided_row() -> None:
    async def scenario() -> None:
        output = ReviewOutput(
            findings=[
                Finding(severity="warning", description="first finding", review_scope="source"),
                Finding(severity="error", description="second finding", review_scope="source"),
                Finding(severity="info", description="third finding", review_scope="source"),
            ],
            risk_level="high",
            risk_rationale="bad",
        )
        app = _FindingsHostApp(output, "ReviewStep")
        async with app.run_test() as pilot:
            await pilot.pause()
            box = app.query_one(FindingsList)
            list_view = box.query_one(_FindingsListView)
            list_view.focus()
            task = asyncio.ensure_future(box.await_decision())
            await pilot.pause()

            assert list_view.index == 0
            await pilot.press("s")  # decide row 0 -- advances to row 1
            await pilot.pause()
            assert list_view.index == 1

            await pilot.press("s")  # decide row 1 -- advances to row 2
            await pilot.pause()
            assert list_view.index == 2

            box._quick_decision("abort")
            await task

    asyncio.run(scenario())


def test_findings_list_advancing_to_the_next_undecided_row_wraps_around() -> None:
    """`_advance_to_next_undecided` searches forward from the current index and wraps past
    the end back to the start -- proven here by deciding the *last* row first, which can
    only find an undecided row by wrapping around to row 0."""

    async def scenario() -> None:
        output = ReviewOutput(
            findings=[
                Finding(severity="warning", description="first finding", review_scope="source"),
                Finding(severity="error", description="second finding", review_scope="source"),
                Finding(severity="info", description="third finding", review_scope="source"),
            ],
            risk_level="high",
            risk_rationale="bad",
        )
        app = _FindingsHostApp(output, "ReviewStep")
        async with app.run_test() as pilot:
            await pilot.pause()
            box = app.query_one(FindingsList)
            list_view = box.query_one(_FindingsListView)
            list_view.focus()
            task = asyncio.ensure_future(box.await_decision())
            await pilot.pause()

            await pilot.press("down")
            await pilot.press("down")
            await pilot.pause()
            assert list_view.index == 2

            # decide row 2 (the last row) -- wraps to row 0
            await pilot.press("s")
            await pilot.pause()
            assert list_view.index == 0

            box._quick_decision("abort")
            await task

    asyncio.run(scenario())


def test_findings_list_aggregates_fix_decided_findings_once_every_row_is_decided() -> None:
    async def scenario() -> None:
        output = ReviewOutput(
            findings=[
                Finding(severity="warning", description="unclear naming", review_scope="source"),
                Finding(severity="error", description="missing null check", review_scope="source"),
            ],
            risk_level="high",
            risk_rationale="bad",
        )
        app = _FindingsHostApp(output, "ReviewStep")
        async with app.run_test() as pilot:
            await pilot.pause()
            box = app.query_one(FindingsList)
            box.query_one(_FindingsListView).focus()
            task = asyncio.ensure_future(box.await_decision())
            await pilot.pause()

            await pilot.press("enter")  # opens the chat on row 0
            await pilot.pause()
            box.query_one(Input).value = "rename it"
            await pilot.press("enter")  # decides row 0, advances to row 1
            await pilot.pause()

            await pilot.press("enter")  # opens the chat on row 1
            await pilot.pause()
            box.query_one(Input).value = "add a null guard"
            # decides row 1 -- every row now decided, resolves
            await pilot.press("enter")
            await pilot.pause()

            response = await task
            assert response == ApprovalResponse(
                decision="fix",
                instructions=(
                    "- [warning] unclear naming: rename it\n"
                    "- [error] missing null check: add a null guard"
                ),
            )

    asyncio.run(scenario())


def test_findings_list_confirming_recommended_suggestions_across_rows_aggregates_with_no_chat() -> (
    None
):
    """Same shape as the aggregation test above, but confirming each row's own recommended
    suggestion (a bare Enter, cursor already on entry 0) rather than going through the chat
    -- the exact multi-finding scenario a human reported as "confirming a suggestion just
    copies it into Chat about it instead of accepting it". No `Input` is ever mounted."""

    async def scenario() -> None:
        output = ReviewOutput(
            findings=[
                Finding(
                    severity="warning",
                    description="unclear naming",
                    review_scope="source",
                    suggestions=["rename it"],
                ),
                Finding(
                    severity="error",
                    description="missing null check",
                    review_scope="source",
                    suggestions=["add a null guard"],
                ),
            ],
            risk_level="high",
            risk_rationale="bad",
        )
        app = _FindingsHostApp(output, "ReviewStep")
        async with app.run_test() as pilot:
            await pilot.pause()
            box = app.query_one(FindingsList)
            list_view = box.query_one(_FindingsListView)
            list_view.focus()
            task = asyncio.ensure_future(box.await_decision())
            await pilot.pause()

            assert list_view.index == 0
            # confirms row 0's "rename it" -- advances to row 1
            await pilot.press("enter")
            await pilot.pause()
            assert not list(box.query(Input))
            assert list_view.index == 1

            # confirms row 1's "add a null guard" -- resolves
            await pilot.press("enter")
            await pilot.pause()
            assert not list(box.query(Input))

            response = await task
            assert response == ApprovalResponse(
                decision="fix",
                instructions=(
                    "- [warning] unclear naming: rename it\n"
                    "- [error] missing null check: add a null guard"
                ),
            )

    asyncio.run(scenario())


def test_findings_list_resolves_skip_when_every_row_is_skip_decided() -> None:
    async def scenario() -> None:
        output = ReviewOutput(
            findings=[
                Finding(severity="warning", description="first finding", review_scope="source"),
                Finding(severity="info", description="second finding", review_scope="source"),
            ],
            risk_level="low",
            risk_rationale="fine",
        )
        app = _FindingsHostApp(output, "ReviewStep")
        async with app.run_test() as pilot:
            await pilot.pause()
            box = app.query_one(FindingsList)
            box.query_one(_FindingsListView).focus()
            task = asyncio.ensure_future(box.await_decision())
            await pilot.pause()

            await pilot.press("s")
            await pilot.pause()
            await pilot.press("s")
            await pilot.pause()

            response = await task
            assert response == ApprovalResponse(decision="skip", instructions=None)

    asyncio.run(scenario())


def test_findings_list_revisiting_a_decided_row_overwrites_its_decision() -> None:
    """A human can browse back to an already-decided row (plain up/down browsing was
    already unrestricted) and reconfirm it with a different decision -- overwriting, not
    rejected -- since `Finding.record_decision` carries no "already decided" guard."""

    async def scenario() -> None:
        output = ReviewOutput(
            findings=[
                Finding(severity="warning", description="first finding", review_scope="source"),
                Finding(severity="error", description="second finding", review_scope="source"),
            ],
            risk_level="high",
            risk_rationale="bad",
        )
        app = _FindingsHostApp(output, "ReviewStep")
        async with app.run_test() as pilot:
            await pilot.pause()
            box = app.query_one(FindingsList)
            list_view = box.query_one(_FindingsListView)
            list_view.focus()
            task = asyncio.ensure_future(box.await_decision())
            await pilot.pause()

            # skip row 0 -- advances to row 1, still undecided
            await pilot.press("s")
            await pilot.pause()
            assert list_view.index == 1

            await pilot.press("up")  # browse back to the already-decided row 0
            await pilot.pause()
            assert list_view.index == 0

            await pilot.press("enter")  # opens the chat on row 0 again
            await pilot.pause()
            box.query_one(Input).value = "actually rename it"
            # reconfirms row 0 as "fix", overwriting "skip"
            await pilot.press("enter")
            await pilot.pause()

            # Row 1 was never decided -- the park is still open, and the cursor moved back
            # to it (the only remaining undecided row).
            assert not task.done()
            assert list_view.index == 1

            # decide row 1 too -- every row now decided, resolves
            await pilot.press("s")
            await pilot.pause()

            response = await task
            assert response == ApprovalResponse(
                decision="fix", instructions="- [warning] first finding: actually rename it"
            )

    asyncio.run(scenario())


def test_findings_list_revisiting_a_chat_decided_row_shows_its_typed_instructions_immediately() -> (
    None
):
    """A human types custom instructions into a row's chat, confirms it (advancing to the
    next row), then just browses back -- no Enter/"f" needed -- and must immediately see
    what was originally typed, not the bare "Chat about it" label. Before this fix every
    chat-open call site hardcoded `prefill=""` and browsing a row never auto-opened its
    chat at all, so merely revisiting a chat-decided row showed the plain placeholder text
    until a deliberate re-open -- and a stray Enter there would silently overwrite the
    recorded "fix" instructions with an empty string."""

    async def scenario() -> None:
        output = ReviewOutput(
            findings=[
                Finding(severity="warning", description="first finding", review_scope="source"),
                Finding(severity="error", description="second finding", review_scope="source"),
            ],
            risk_level="high",
            risk_rationale="bad",
        )
        app = _FindingsHostApp(output, "ReviewStep")
        async with app.run_test() as pilot:
            await pilot.pause()
            box = app.query_one(FindingsList)
            list_view = box.query_one(_FindingsListView)
            list_view.focus()
            task = asyncio.ensure_future(box.await_decision())
            await pilot.pause()

            await pilot.press("enter")  # opens the chat on row 0
            await pilot.pause()
            box.query_one(Input).value = "actually rename it"
            await pilot.press("enter")  # confirms row 0, advances to row 1
            await pilot.pause()
            assert list_view.index == 1
            # row 0 hidden, its Input torn down
            assert not list(box.query(Input))

            await pilot.press("up")  # browse back to the already-decided row 0
            await pilot.pause()
            assert list_view.index == 0

            # No Enter/"f" pressed -- the chat reappeared on its own, pre-filled.
            lines = _finding_rows_content(box)
            assert "Chat about it" not in lines[0]
            restored_input = box.query_one(Input)
            assert restored_input.value == "actually rename it"
            assert not restored_input.has_focus  # visible, but doesn't steal keyboard focus

            # The auto-restored `Input` isn't focused, so a first Enter here (still handled
            # by `_FindingsListView`) only focuses it for editing -- it must not resubmit
            # blank text and corrupt the recorded decision.
            await pilot.press("enter")
            await pilot.pause()
            assert list_view.index == 0
            assert box.query_one(Input).has_focus
            assert box.query_one(Input).value == "actually rename it"
            assert box._rows[0].row_decision == ApprovalResponse(
                decision="fix", instructions="actually rename it"
            )

            # A second Enter, now that the `Input` itself has focus, submits it for real --
            # unchanged text, so the recorded decision is untouched and the park advances.
            await pilot.press("enter")
            await pilot.pause()
            assert list_view.index == 1
            assert box._rows[0].row_decision == ApprovalResponse(
                decision="fix", instructions="actually rename it"
            )

            # decide row 1 too -- every row now decided, resolves
            await pilot.press("s")
            await pilot.pause()

            response = await task
            assert response == ApprovalResponse(
                decision="fix", instructions="- [warning] first finding: actually rename it"
            )

    asyncio.run(scenario())


def test_findings_list_abort_resolves_immediately_regardless_of_per_row_progress() -> None:
    """ "x" (abort) stays a whole-run action, unchanged by issue #98: it resolves the park
    the instant it's pressed, even with some rows already decided and others not."""

    async def scenario() -> None:
        output = ReviewOutput(
            findings=[
                Finding(severity="warning", description="first finding", review_scope="source"),
                Finding(severity="error", description="second finding", review_scope="source"),
                Finding(severity="info", description="third finding", review_scope="source"),
            ],
            risk_level="high",
            risk_rationale="bad",
        )
        app = _FindingsHostApp(output, "ReviewStep")
        async with app.run_test() as pilot:
            await pilot.pause()
            box = app.query_one(FindingsList)
            box.query_one(_FindingsListView).focus()
            task = asyncio.ensure_future(box.await_decision())
            await pilot.pause()

            # decide row 0 only -- 2 of 3 rows still undecided
            await pilot.press("s")
            await pilot.pause()

            await pilot.press("x")
            await pilot.pause()

            response = await task
            assert response == ApprovalResponse(decision="abort", instructions=None)

    asyncio.run(scenario())


def test_findings_list_single_finding_park_resolves_immediately_on_one_decision() -> None:
    """Degenerate case (issue #98's own linked design discussion): a park with exactly one
    row (e.g. `steps/rebase.py`'s issue #24 guard, which always emits exactly one `Finding`)
    resolves on that row's very first decision, with its `ApprovalResponse` passed through
    completely unwrapped -- not routed through `describe_finding_decisions`'s combined-
    instructions format, which only earns its keep once there are two or more rows to
    attribute text to. Pinned separately from the pre-existing single-finding tests in the
    section above (which already exercise this path incidentally) to make the "exact,
    unwrapped match" guarantee explicit, since `steps/rebase.py`'s guard relies on it."""

    async def scenario() -> None:
        output = ReviewOutput(
            findings=[
                Finding(severity="warning", description="unclear naming", review_scope="source")
            ],
            risk_level="low",
            risk_rationale="fine",
        )
        app = _FindingsHostApp(output, "ReviewStep")
        async with app.run_test() as pilot:
            await pilot.pause()
            box = app.query_one(FindingsList)
            box.query_one(_FindingsListView).focus()
            task = asyncio.ensure_future(box.await_decision())
            await pilot.pause()

            await pilot.press("enter")
            await pilot.pause()
            box.query_one(Input).value = "looks fine actually"
            await pilot.press("enter")
            await pilot.pause()

            response = await task
            # Not "- [warning] unclear naming: looks fine actually" -- the combined-
            # instructions format never applies to a single-row park.
            assert response == ApprovalResponse(decision="fix", instructions="looks fine actually")

    asyncio.run(scenario())


def test_findings_list_await_decision_resets_stale_decisions_from_a_previous_round() -> None:
    """A fix-round re-park on the same `FindingsList` (a step's own re-run after a human's
    "fix" instructions didn't resolve it) must not carry over the previous round's per-row
    decisions onto the fresh one -- `await_decision` clears every row via `Finding.
    clear_decision` at the very start of each call."""

    async def scenario() -> None:
        output = ReviewOutput(
            findings=[
                Finding(severity="warning", description="first finding", review_scope="source"),
                Finding(severity="error", description="second finding", review_scope="source"),
            ],
            risk_level="high",
            risk_rationale="bad",
        )
        app = _FindingsHostApp(output, "ReviewStep")
        async with app.run_test() as pilot:
            await pilot.pause()
            box = app.query_one(FindingsList)
            box.query_one(_FindingsListView).focus()

            first_task = asyncio.ensure_future(box.await_decision())
            await pilot.pause()
            await pilot.press("s")  # decide row 0 only
            await pilot.pause()
            box._quick_decision("abort")
            await first_task

            assert box._rows[0].is_decided()
            assert not box._rows[1].is_decided()

            second_task = asyncio.ensure_future(box.await_decision())
            await pilot.pause()

            assert not any(row.is_decided() for row in box._rows)

            box._quick_decision("abort")
            await second_task

    asyncio.run(scenario())


def test_findings_list_decided_marker_is_visible_on_every_row_regardless_of_highlight() -> None:
    """`FindingsSuggestion` only ever shows for the highlighted row (issue #88, unchanged),
    so the decided marker on `FindingsDescription` -- visible on every row -- is what lets a
    human tell a decided row apart from an undecided one while browsing anywhere in the
    list, not just the row they last acted on."""

    async def scenario() -> None:
        output = ReviewOutput(
            findings=[
                Finding(severity="warning", description="first finding", review_scope="source"),
                Finding(severity="error", description="second finding", review_scope="source"),
            ],
            risk_level="high",
            risk_rationale="bad",
        )
        app = _FindingsHostApp(output, "ReviewStep")
        async with app.run_test() as pilot:
            await pilot.pause()
            box = app.query_one(FindingsList)
            box.query_one(_FindingsListView).focus()
            task = asyncio.ensure_future(box.await_decision())
            await pilot.pause()

            await pilot.press("s")  # skip row 0 -- advances highlight to row 1
            await pilot.pause()

            lines = _finding_rows_content(box)
            # row 0's marker survives losing highlight
            assert lines[0].startswith("⏭ ●")
            assert not lines[1].startswith(("⏭", "✔"))  # row 1 is still undecided

            box._quick_decision("abort")
            await task

    asyncio.run(scenario())


def test_findings_list_footer_hint_shows_a_decided_progress_count_while_parked() -> None:
    async def scenario() -> None:
        output = ReviewOutput(
            findings=[
                Finding(severity="warning", description="first finding", review_scope="source"),
                Finding(severity="error", description="second finding", review_scope="source"),
            ],
            risk_level="high",
            risk_rationale="bad",
        )
        app = _FindingsHostApp(output, "ReviewStep")
        async with app.run_test() as pilot:
            await pilot.pause()
            box = app.query_one(FindingsList)
            box.query_one(_FindingsListView).focus()
            task = asyncio.ensure_future(box.await_decision())
            await pilot.pause()

            footer = box.query_one("#findings-footer", Static)
            assert "0/2 decided" in footer.content

            await pilot.press("s")
            await pilot.pause()
            assert "1/2 decided" in footer.content

            box._quick_decision("abort")
            await task

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
