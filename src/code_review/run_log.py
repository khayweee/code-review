"""A dumb, write-only, append-only plain-text transcript of one pipeline run, persisted to
disk under `install_state.state_dir() / "runs"` so a run's command trail survives closing
the TUI. This is a narrower thing than a database/resume-after-crash machinery (deliberately
out of scope, see `docs/ROADMAP.md`/`pipeline/README.md`'s milestone 2 rationale) -- no
schema, no resume logic, and never read back by this tool itself. `cli.py`'s `review`
command is the sole writer; anyone else wants this file, they open it in a text editor.

Depends on both `pipeline.step.StepEvent` and `tui.activity.ActivityEvent`, so it lives here
rather than under `pipeline/`/`steps/` (which must never import `tui/`).
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import TextIO

from code_review.install_state import state_dir
from code_review.pipeline.step import StepEvent
from code_review.tui.activity import ActivityEvent


def _sanitize_branch_for_log_filename(branch: str) -> str:
    """Replace `/` in `branch` (e.g. `feat/event_streaming`) with `-` so it's safe to use
    as one filesystem path segment -- a branch name is otherwise unconstrained here.
    """

    return branch.replace("/", "-")


def run_log_path(branch: str, *, now: float | None = None) -> Path:
    """Where this run's log file lives: `state_dir()/runs/<sanitized-branch>-<timestamp>.log`.
    `now` (a `time.time()`-shaped float) is only for testability -- production callers pass
    nothing and get the real current time.
    """

    timestamp = time.time() if now is None else now
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(timestamp))
    filename = f"{_sanitize_branch_for_log_filename(branch)}-{stamp}.log"
    return state_dir() / "runs" / filename


def _format_duration(seconds: float) -> str:
    """Plain `12.3s`, no minute-rollover polish -- this is a log file, not a live view (see
    `tui/widgets/pipeline_box.py`'s `format_duration` for the TUI's fuller version).
    """

    return f"{seconds:.1f}s"


def format_step_event_line(event: StepEvent) -> str:
    """Render one `StepEvent` as a run-log line: `"▶ <step>"` while running, `"✔ <step>
    <Ns>"` once completed. Uses `event.step_name` (the canonical name) -- this module has
    no `display_names` mapping to translate through, unlike the TUI's own rendering.
    """

    if event.status == "running":
        return f"▶ {event.step_name}"
    assert event.duration is not None  # a "completed" event always carries one
    return f"✔ {event.step_name}  {_format_duration(event.duration)}"


def format_activity_event_line(
    event: ActivityEvent, *, duration: float | None = None
) -> str | None:
    """Render one `ActivityEvent` as a run-log line, or `None` for a `"started"` event --
    only `"finished"` events are written, since a `"started"`-only line with no duration
    isn't informative and one-shot `log()` events already emit both back-to-back (writing
    only "finished" avoids a near-duplicate line for those).

    `ActivityEvent` itself carries only a point-in-time `timestamp`, not its own elapsed
    duration -- `duration`, when the caller has it (`RunLogWriter` tracks each activity's own
    started/finished pairing, mirroring `tui/state.py`'s `backfill_activities`), is rendered
    alongside the label. `event.error` (set via `ActivityHandle.fail(...)`) is appended in
    parens when present -- the pass/fail signal Part A added.
    """

    if event.status != "finished":
        return None
    # Mirrors tui/widgets/styles.py's _STATUS_ICONS "completed"/"failed" glyphs, kept as
    # separate literals here rather than imported -- this module lives outside
    # tui/widgets/'s own package boundary, and _STATUS_ICONS is that module-private.
    icon = "✘" if event.error is not None else "✔"
    line = f"  {icon} {event.label}"
    if duration is not None:
        line += f"  {_format_duration(duration)}"
    if event.error is not None:
        line += f"  ({event.error})"
    return line


class RunLogWriter:
    """Appends `format_step_event_line`/`format_activity_event_line`'s output to `path`,
    flushing after every write (small, infrequent writes -- simplicity over batching perf).
    Never reads `path` back; `close()` is the caller's responsibility once the run ends.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._file: TextIO = self.path.open("a", encoding="utf-8")
        # Tracks each still-open activity's own "started" timestamp, keyed by activity_id,
        # so its matching "finished" event can report a real elapsed duration -- the same
        # pairing tui/state.py's backfill_activities does, kept here rather than shared
        # since this is the only other place that needs it.
        self._activity_started_at: dict[int, float] = {}

    def write_step_event(self, event: StepEvent) -> None:
        self.write_line(format_step_event_line(event))

    def write_activity_event(self, event: ActivityEvent) -> None:
        if event.status == "started":
            self._activity_started_at[event.activity_id] = event.timestamp
        duration = None
        if event.status == "finished":
            started_at = self._activity_started_at.pop(event.activity_id, None)
            duration = None if started_at is None else event.timestamp - started_at
        line = format_activity_event_line(event, duration=duration)
        if line is not None:
            self.write_line(line)

    def write_line(self, text: str) -> None:
        self._file.write(text + "\n")
        self._file.flush()

    def close(self) -> None:
        self._file.close()
