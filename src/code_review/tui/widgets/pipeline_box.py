"""The Pipeline box: one line per registry step, live status icon, elapsed/final duration,
plus each row's own optional `StepRow.detail` text (e.g. `PRStep`'s opened/updated PR link).

Renders plain `StepRow`/`ActivityRow` data (see `state.py`); the formatting helpers are
pure and Textual-free. `PipelineBox` itself owns no timer of its own -- `animate_shimmer`
is a plain repaint method `ReviewApp`'s own single tick timer calls (see `app.py`'s module
docstring for why there is exactly one timer, not one per widget). `render_rows_live`
renders Rich renderables so the running row can shimmer/spin; `render_rows` is the
plain-text fallback for tests.
"""

from __future__ import annotations

import colorsys
import time
from collections.abc import Sequence
from pathlib import Path

from rich.console import Group
from rich.spinner import Spinner
from rich.table import Table
from rich.text import Text

from code_review.tui.state import ActivityRow, StepRow
from code_review.tui.widgets.base import _BorderedBox
from code_review.tui.widgets.styles import (
    _ACTIVITY_STYLE,
    _DOT_ICON,
    _STATUS_DOT_STYLES,
    _STATUS_ICONS,
)


def format_duration(duration: float) -> str:
    """Render a duration in seconds as `M:SS` once it reaches a minute, else `0.3s`."""

    if duration < 60:
        return f"{duration:.1f}s"
    minutes, seconds = divmod(int(duration), 60)
    return f"{minutes}:{seconds:02d}"


def format_row(row: StepRow) -> str:
    """Render one `StepRow` as plain text for tests and non-animated fallbacks."""

    icon = _STATUS_ICONS[row.status]
    duration = "" if row.duration is None else f"  {format_duration(row.duration)}"
    detail = "" if row.detail is None else f"  {row.detail}"
    return f"{icon} {row.name}{duration}{detail}"


def format_activity_row(activity: ActivityRow, *, is_last: bool, depth: int = 1) -> str:
    """Render one `ActivityRow` as a directory-tree-style line beneath its owning step.

    `is_last` picks the closing `└ ` connector vs. a continuing `├ `; the caller computes
    it since this function only sees one row at a time. `depth` is how many activity levels
    deep this row sits (1 for a row reported directly under its step, 2 for a tool call an
    agent made while that agent's own span was open, via `ActivityRow.children`, etc.) --
    each level adds one more `"  "` of indent so a child visibly nests under its parent
    rather than reading as a sibling at the same indent.
    """

    connector = "└ " if is_last else "├ "
    icon = _STATUS_ICONS[activity.status]
    duration = "" if activity.duration is None else f"  {format_duration(activity.duration)}"
    detail = "" if activity.detail is None else f"  {activity.detail}"
    indent = "  " * depth
    return f"{indent}{connector} {icon} {activity.label}{duration}{detail}"


def _iter_activity_lines(
    activities: Sequence[ActivityRow], *, depth: int = 1
) -> list[tuple[ActivityRow, int, bool]]:
    """Flatten `activities` (and, recursively, each one's own `children`) into
    `(activity, depth, is_last)` triples in tree display order -- a row immediately
    followed by its own children before any of its siblings, each level one `depth` deeper
    than its parent. Shared by `render_rows`/`render_rows_live` so both walk the same tree
    the same way."""

    lines: list[tuple[ActivityRow, int, bool]] = []
    last_index = len(activities) - 1
    for index, activity in enumerate(activities):
        lines.append((activity, depth, index == last_index))
        lines.extend(_iter_activity_lines(activity.children, depth=depth + 1))
    return lines


def render_rows(rows: Sequence[StepRow]) -> str:
    """Render every row as one line each, with each row's own `activities` rendered as
    tree-connected lines immediately beneath it."""

    lines = []
    for row in rows:
        lines.append(format_row(row))
        lines.extend(
            format_activity_row(activity, is_last=is_last, depth=depth)
            for activity, depth, is_last in _iter_activity_lines(row.activities)
        )
    return "\n".join(lines)


_SHIMMER_BASE_LIGHTNESS = 0.45
_SHIMMER_PEAK_LIGHTNESS = 0.90
# Matches rich.spinner.Spinner("dots").interval (80ms) -- the spinner icon itself can't
# show a new frame any faster than that, so ticking `animate_shimmer` past it (the
# previous 1/60s, ~5x faster) only spent extra CPU/output re-rendering and re-writing an
# unchanged spinner glyph, at real, measured cost: a still-"running"/parked step's steady
# ANSI output over a real pty (~130KB/s at 60Hz) was enough to push a real end-to-end
# `code-review review` subprocess -- and, on a loaded CI runner, its siblings -- past their
# own real-pty tests' exit-wait timeouts (see tests/test_cli_review.py's
# `_run_review_with_keypresses` docstring). The color shimmer (`gradient_text`) is
# continuous, not frame-quantized, so sampling it at 12.5Hz instead of 60Hz still reads as
# smooth motion to the eye; it just no longer redraws faster than anything on screen can
# actually change. `app.py` imports this directly as its own single tick timer's interval
# -- see that module's docstring for why there is only one timer, not one per widget.
_SHIMMER_TICK_SECONDS = 0.08


def gradient_text(label: str, phase: float) -> Text:
    """Per-character grayscale shimmer for a running step's name: one highlight band
    sweeps across the label per phase cycle. Pass `time.monotonic()` as `phase` so
    consecutive repaints visibly move."""

    text = Text()
    length = max(len(label), 1)
    for index, char in enumerate(label):
        position = (index / length + phase) % 2.0
        distance_from_peak = abs(position - 0.5) * 2
        brightness = max(0.0, 1.0 - distance_from_peak)
        lightness = _SHIMMER_BASE_LIGHTNESS + brightness * (
            _SHIMMER_PEAK_LIGHTNESS - _SHIMMER_BASE_LIGHTNESS
        )
        red, green, blue = colorsys.hls_to_rgb(0.0, lightness, 0.0)
        color = f"#{int(red * 255):02x}{int(green * 255):02x}{int(blue * 255):02x}"
        text.append(char, style=color)
    return text


def _render_row(row: StepRow, spinners: dict[str, Spinner]) -> tuple[Spinner | Text, Text]:
    """Render one row as Rich renderables, using a live spinner for running rows.

    `spinners` caches one `Spinner` per running step name so repeated calls reuse the
    same instance -- a fresh `Spinner` every render would reset its animation clock.
    Running rows shimmer via `gradient_text`; completed/failed rows get a colored
    `_DOT_ICON`; pending keeps the plain glyph.
    """

    if row.status != "running":
        spinners.pop(row.name, None)
        dot_style = _STATUS_DOT_STYLES.get(row.status)
        icon: Spinner | Text = (
            Text(_DOT_ICON, style=dot_style) if dot_style else Text(_STATUS_ICONS[row.status])
        )
        row_text = Text(row.name)
    else:
        icon = spinners.setdefault(row.name, Spinner("dots"))
        row_text = gradient_text(row.name, phase=time.monotonic())
    duration = "" if row.duration is None else f"  {format_duration(row.duration)}"
    row_text.append(duration)
    if row.detail is not None:
        row_text.append(f"  {row.detail}")
    return icon, row_text


def render_rows_live(rows: Sequence[StepRow], spinners: dict[str, Spinner]) -> Group:
    """Render every row as Rich renderables so the running row can animate itself, each
    row's `activities` as tree-connected lines beneath it.

    Returns a `Group` of one small `(icon, text)` grid per step rather than one shared
    `Table.grid`, since a shared grid would size every row's icon column to the widest
    cell across all rows.
    """

    renderables: list[Table | Text] = []
    for row in rows:
        step_table = Table.grid(padding=(0, 1), pad_edge=False, expand=False)
        step_table.add_row(*_render_row(row, spinners))
        renderables.append(step_table)

        for activity, depth, is_last in _iter_activity_lines(row.activities):
            renderables.append(
                Text(
                    format_activity_row(activity, is_last=is_last, depth=depth),
                    style=_ACTIVITY_STYLE,
                )
            )
    return Group(*renderables)


class PipelineBox(_BorderedBox):
    """A bordered box listing every registered step and its current status."""

    DEFAULT_CSS = (
        Path(__file__).with_name("tokens.tcss").read_text()
        + "\n"
        + Path(__file__).with_suffix(".tcss").read_text()
    )

    def __init__(
        self,
        rows: Sequence[StepRow] = (),
        *,
        branch: str | None = None,
        id: (str | None) = None,  # noqa: A002 -- matches Textual's own Widget.__init__ shape
        classes: str | None = None,
    ) -> None:
        # One Spinner per running step name, reused across renders so its clock persists.
        self._spinners: dict[str, Spinner] = {}
        super().__init__(render_rows_live(rows, self._spinners), id=id, classes=classes)
        self._rows = list(rows)
        self.border_title = "Agentic Code-Review Pipeline"
        # None (no branch passed) leaves no border_subtitle at all -- "no box, not a
        # placeholder", same discipline the Findings/Status boxes already follow.
        if branch is not None:
            self.border_subtitle = branch

    def animate_shimmer(self) -> None:
        """Re-render from the current rows/spinners so `gradient_text` recomputes the
        shimmer sweep and the "dots" spinner's own glyph can advance; `self.refresh()`
        alone would just repaint the stale renderable. `layout=False` since only color
        changes, not row count/line length.

        Called by `ReviewApp`'s own single tick timer (see `app.py`'s module docstring),
        not a timer this widget owns itself -- two independently-owned timers (this one
        plus `ReviewApp`'s) used to both drive repaints here, and `ReviewApp` stopping only
        its own on completion left this one running forever, starving Textual's compositor
        of ever landing the final "done" repaint (confirmed directly against a real pty
        run: `tests/test_cli_review.py`'s `test_review_skipping_both_findings_of_a_two_
        finding_park_completes_the_run`, the screen frozen on the last pre-completion frame
        even though the run had genuinely finished). A single timer, owned where the "done"
        state itself lives, can't leave a second one behind to forget."""

        self.update(render_rows_live(self._rows, self._spinners), layout=False)

    def update_rows(self, rows: Sequence[StepRow]) -> None:
        """Replace the displayed rows with `rows`, re-rendered in order."""

        self._rows = list(rows)
        self.update(render_rows_live(rows, self._spinners))
