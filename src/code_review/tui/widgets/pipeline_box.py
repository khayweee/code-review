"""The Pipeline box: one line per registry step, live status icon, elapsed/final duration.

- Renders plain `StepRow`/`ActivityRow` data (see `state.py`), never reads a `StepEvent`
  stream or a registry/agent output itself.
- Pure formatting helpers (`format_row`, `render_rows`, `gradient_text`, ...) are
  unit-testable without Textual; `PipelineBox` itself is the live, animated widget.
- `render_rows_live` renders each row as Rich renderables so the running row's name can
  shimmer and its icon can spin, while `render_rows` is the plain-text fallback used by
  tests and non-animated call sites.
"""

from __future__ import annotations

import colorsys
import time
from collections.abc import Sequence

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
    return f"{icon} {row.name}{duration}"


def format_activity_row(activity: ActivityRow, *, is_last: bool) -> str:
    """Render one `ActivityRow` as a directory-tree-style line beneath its owning step.

    - Mirrors `format_row`'s icon/duration conventions.
    - `is_last` picks the closing `└ ` connector vs. a continuing `├ `.
    - `is_last` is the caller's (`render_rows`/`render_rows_live`) job to compute, since
      this function only knows about one `ActivityRow` at a time, not its position among
      its owning step's other activities.
    """

    connector = "└ " if is_last else "├ "
    icon = _STATUS_ICONS[activity.status]
    duration = "" if activity.duration is None else f"  {format_duration(activity.duration)}"
    return f"  {connector} {icon} {activity.label}{duration}"


def render_rows(rows: Sequence[StepRow]) -> str:
    """Render every row as one line each, with each row's own `activities` rendered as
    tree-connected lines immediately beneath it."""

    lines = []
    for row in rows:
        lines.append(format_row(row))
        last_index = len(row.activities) - 1
        lines.extend(
            format_activity_row(activity, is_last=index == last_index)
            for index, activity in enumerate(row.activities)
        )
    return "\n".join(lines)


_SHIMMER_BASE_LIGHTNESS = 0.45
_SHIMMER_PEAK_LIGHTNESS = 0.90


def gradient_text(label: str, phase: float) -> Text:
    """Pure per-character gradient color computation for the running step's name.

    - A "rendering..." shimmer distinct from the plain text a pending/completed row gets.
    - Factored out of `_render_row` so the color math is unit-testable without Textual or
      timing flakiness.
    - A single grayscale highlight band sweeps across the label once per phase cycle,
      brightest at the band's center, fading to `_SHIMMER_BASE_LIGHTNESS` at its edges.
    - The caller passes `time.monotonic()` as `phase` so consecutive repaints visibly move.
    - Zero saturation keeps every stop neutral gray/white rather than cycling hues.
    """

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

    - `spinners` caches one `Spinner` instance per running step name (`PipelineBox._spinners`),
      shared across repeated calls -- constructing a fresh `Spinner` every render would reset
      its animation clock and it would never look animated.
    - A row that stops running has its cached spinner evicted, so a later run of the
      same-named step starts its animation fresh.
    - A running row's name renders via `gradient_text` (phased by `time.monotonic()`); the
      duration suffix stays plain, appended after.
    - A completed/failed row's icon renders as a colored `_DOT_ICON`; pending keeps its
      plain hollow-ring glyph.
    """

    if row.status != "running":
        spinners.pop(row.name, None)
        dot_style = _STATUS_DOT_STYLES.get(row.status)
        icon: Spinner | Text = (
            Text(_DOT_ICON, style=dot_style) if dot_style else Text(_STATUS_ICONS[row.status])
        )
        row_text = Text(row.name)
    else:
        icon = spinners.setdefault(row.name, Spinner("moon"))
        row_text = gradient_text(row.name, phase=time.monotonic())
    duration = "" if row.duration is None else f"  {format_duration(row.duration)}"
    row_text.append(duration)
    return icon, row_text


def render_rows_live(rows: Sequence[StepRow], spinners: dict[str, Spinner]) -> Group:
    """Render every row as Rich renderables so the running row can animate itself.

    - Each row's own `activities` render as tree-connected lines immediately beneath it.
    - Returns a `Group` of one small `(icon, text)` grid per step, not one shared
      `Table.grid` spanning every row -- a shared grid sizes every row's icon column to
      the widest cell across *every* row ever added, so a step with no activities still
      got padded to match some unrelated step's longest connector.
    - Activity lines need no grid: each is one already-formed `Text` line, its `├─`/`└─`
      connector baked directly into the string by `format_activity_row`.
    - `spinners` is the caller's cache (see `_render_row`), passed in so it persists
      across repeated calls for the same `PipelineBox`.
    """

    renderables: list[Table | Text] = []
    for row in rows:
        step_table = Table.grid(padding=(0, 1), pad_edge=False, expand=False)
        step_table.add_row(*_render_row(row, spinners))
        renderables.append(step_table)

        last_index = len(row.activities) - 1
        for index, activity in enumerate(row.activities):
            renderables.append(
                Text(
                    format_activity_row(activity, is_last=index == last_index),
                    style=_ACTIVITY_STYLE,
                )
            )
    return Group(*renderables)


class PipelineBox(_BorderedBox):
    """A bordered box listing every registered step and its current status."""

    def __init__(
        self,
        rows: Sequence[StepRow] = (),
        *,
        id: (str | None) = None,  # noqa: A002 -- matches Textual's own Widget.__init__ shape
        classes: str | None = None,
    ) -> None:
        # One `Spinner` per currently-running step name, reused across `update_rows`
        # calls for as long as that step stays running -- see `_render_row`'s docstring
        # for why a fresh `Spinner` per render never animates.
        self._spinners: dict[str, Spinner] = {}
        super().__init__(render_rows_live(rows, self._spinners), id=id, classes=classes)
        self._rows = list(rows)
        self.border_title = "Agentic Code Review Pipeline"

    def on_mount(self) -> None:
        self.set_interval(1 / 60, self._animate_shimmer)

    def _animate_shimmer(self) -> None:
        """Re-run `render_rows_live` every tick, not just `self.refresh()`.

        - `refresh()` alone only repaints the already-stored renderable; it does not call
          `gradient_text` again, so a running row's shimmer would freeze between events.
        - `layout=False` since this recompute never changes row count/line length, only
          per-character color.
        - Named `_animate_shimmer`, not `_animate` -- `Widget` already defines a private
          `_animate` attribute for its own animation system.
        """

        self.update(render_rows_live(self._rows, self._spinners), layout=False)

    def update_rows(self, rows: Sequence[StepRow]) -> None:
        """Replace the displayed rows with `rows`, re-rendered in order."""

        self._rows = list(rows)
        self.update(render_rows_live(rows, self._spinners))
