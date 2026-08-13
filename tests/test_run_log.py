"""Tests for `run_log.py`'s pure formatting helpers and `RunLogWriter`'s file-writing side
effects.

No real pipeline run here (see `tests/test_cli_review.py`'s
`test_review_runs_end_to_end_against_a_real_repo_and_exits_cleanly` for the end-to-end
proof of `cli.py`'s wiring) -- every `StepEvent`/`ActivityEvent` below is hand-built,
matching `tests/tui/test_state.py`'s own convention for the same event types.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from code_review.install_state import STATE_DIR_ENV_VAR
from code_review.pipeline.step import StepEvent, StepOutcome
from code_review.run_log import (
    RunLogWriter,
    format_activity_event_line,
    format_step_event_line,
    run_log_path,
)
from code_review.tui.activity import ActivityEvent

_OUTCOME = StepOutcome(needs_approval=False, auto_fixable=False, payload=[])

# --- run_log_path -----------------------------------------------------------------------


def test_run_log_path_sanitizes_slashes_in_the_branch_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(STATE_DIR_ENV_VAR, str(tmp_path))

    path = run_log_path("feat/event_streaming", now=1_700_000_000.0)

    assert path.parent == tmp_path / "runs"
    assert "/" not in path.name.removesuffix(".log")
    assert path.name.startswith("feat-event_streaming-")


def test_run_log_path_is_deterministic_for_a_fixed_now(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(STATE_DIR_ENV_VAR, str(tmp_path))

    first = run_log_path("main", now=1_700_000_000.0)
    second = run_log_path("main", now=1_700_000_000.0)

    assert first == second


def test_run_log_path_differs_for_a_different_now(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(STATE_DIR_ENV_VAR, str(tmp_path))

    first = run_log_path("main", now=1_700_000_000.0)
    second = run_log_path("main", now=1_700_000_100.0)

    assert first != second


# --- format_step_event_line (pure) --------------------------------------------------------


def test_format_step_event_line_renders_a_running_event_with_no_duration() -> None:
    event = StepEvent(
        step_name="RebaseStep", status="running", outcome=None, started_at=1.0, duration=None
    )

    assert format_step_event_line(event) == "▶ RebaseStep"


def test_format_step_event_line_renders_a_completed_event_with_its_duration() -> None:
    event = StepEvent(
        step_name="RebaseStep", status="completed", outcome=_OUTCOME, started_at=1.0, duration=8.0
    )

    assert format_step_event_line(event) == "✔ RebaseStep  8.0s"


# --- format_activity_event_line (pure) -----------------------------------------------------


def test_format_activity_event_line_returns_none_for_a_started_event() -> None:
    event = ActivityEvent(1, None, "git fetch origin", "started", 5.0)

    assert format_activity_event_line(event) is None


def test_format_activity_event_line_renders_a_finished_event_with_its_duration() -> None:
    event = ActivityEvent(1, None, "git fetch origin", "finished", 5.4)

    assert format_activity_event_line(event, duration=0.4) == "  ✔ git fetch origin  0.4s"


def test_format_activity_event_line_omits_duration_when_not_provided() -> None:
    event = ActivityEvent(1, None, "git fetch origin", "finished", 5.4)

    assert format_activity_event_line(event) == "  ✔ git fetch origin"


def test_format_activity_event_line_appends_the_error_detail_when_present() -> None:
    event = ActivityEvent(1, None, "git fetch origin", "finished", 5.4, "exit 1")

    assert format_activity_event_line(event, duration=0.4) == "  ✘ git fetch origin  0.4s  (exit 1)"


# --- RunLogWriter (real file, tmp_path) -----------------------------------------------------


def test_run_log_writer_creates_the_parent_directory(tmp_path: Path) -> None:
    path = tmp_path / "runs" / "does" / "not" / "exist" / "main-20260101-000000.log"

    writer = RunLogWriter(path)
    writer.close()

    assert path.exists()


def test_run_log_writer_writes_step_events_as_they_arrive(tmp_path: Path) -> None:
    path = tmp_path / "run.log"
    writer = RunLogWriter(path)

    writer.write_step_event(
        StepEvent(
            step_name="IntentStep", status="running", outcome=None, started_at=0.0, duration=None
        )
    )
    writer.write_step_event(
        StepEvent(
            step_name="IntentStep",
            status="completed",
            outcome=_OUTCOME,
            started_at=0.0,
            duration=0.5,
        )
    )
    writer.close()

    lines = path.read_text().splitlines()
    assert lines == ["▶ IntentStep", "✔ IntentStep  0.5s"]


def test_run_log_writer_writes_only_the_finished_half_of_an_activity_pair(tmp_path: Path) -> None:
    path = tmp_path / "run.log"
    writer = RunLogWriter(path)

    writer.write_activity_event(ActivityEvent(1, None, "git fetch origin", "started", 5.0))
    writer.write_activity_event(ActivityEvent(1, None, "git fetch origin", "finished", 5.4))
    writer.close()

    lines = path.read_text().splitlines()
    assert lines == ["  ✔ git fetch origin  0.4s"]


def test_run_log_writer_computes_the_activitys_own_duration_from_its_started_finished_pair(
    tmp_path: Path,
) -> None:
    path = tmp_path / "run.log"
    writer = RunLogWriter(path)

    writer.write_activity_event(ActivityEvent(1, None, "gh pr create", "started", 100.0))
    writer.write_activity_event(ActivityEvent(1, None, "gh pr create", "finished", 102.5, "exit 1"))
    writer.close()

    assert path.read_text().splitlines() == ["  ✘ gh pr create  2.5s  (exit 1)"]


def test_run_log_writer_write_line_appends_a_plain_line(tmp_path: Path) -> None:
    path = tmp_path / "run.log"
    writer = RunLogWriter(path)

    writer.write_line("Pipeline ran successfully.")
    writer.close()

    assert path.read_text() == "Pipeline ran successfully.\n"


def test_run_log_writer_appends_across_multiple_writes_without_truncating(tmp_path: Path) -> None:
    path = tmp_path / "run.log"

    first_writer = RunLogWriter(path)
    first_writer.write_line("first")
    first_writer.close()

    second_writer = RunLogWriter(path)
    second_writer.write_line("second")
    second_writer.close()

    assert path.read_text().splitlines() == ["first", "second"]
