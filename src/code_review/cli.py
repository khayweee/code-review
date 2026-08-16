"""CLI entry point (`code-review`).

`review` requires a real TTY on stdin and stdout (the live progress view can't render into
a pipe/redirect). After the branch ref is verified synchronously (so a bad BRANCH fails fast
with no TUI flash), it runs `tui.app.ReviewApp` driven by `_run_pipeline` -- an async
generator that builds `StepContext` (`cwd=Path.cwd()`, the user's real repo; `branch=branch`)
from `_diff_against_default_branch` (fetched in a worker thread so a slow diff doesn't delay
the TUI's first paint) and runs the implemented steps through `run_steps`, `steps/worktree.
py`'s `WorktreeStep` first among them. `WorktreeStep` creates a throwaway `git worktree`
checked out **detached** at `<branch>`'s tip commit -- never by name, so it can never
collide with (or corrupt) `<branch>` already being checked out in the user's real repo,
the ordinary case when reviewing the branch you're currently on (see `steps/worktree.py`'s
module docstring) -- and redirects every later step's `ctx.cwd` at it (`StepOutcome.
cwd_override`, see `pipeline/AGENTS.md`'s WorktreeStep section) -- so a run never touches
the user's real checkout, and this holds for any caller of `run_steps`, not only this
command. `_capture_worktree_path` tees the event stream to record
`WorktreeStep`'s real worktree path (from its "completed" `StepEvent`) into a small mutable
holder `review` reads once `tui_app.run()` returns: the worktree is removed then -- success,
pipeline failure, or any uncaught exception -- unless `--keep-worktree` was passed, in which
case its path is echoed instead. If `WorktreeStep` itself never completes (e.g. it raised),
the holder stays empty and there is nothing to clean up.

`review` also persists a per-run plain-text transcript via `run_log.RunLogWriter`: `_log_
step_events` tees `StepEvent`s to it as `ReviewApp` consumes them, `ActivityRelay`'s
`on_event` hook feeds it `ActivityEvent`s the same way, and the final status line is written
once the TUI exits. Write-only -- nothing in this module or `run_log.py` reads it back.

`update`/`uninstall` shell out to `uv tool upgrade`/`uv tool uninstall`; `uv` owns all
dependency/env/binary management, nothing is reimplemented here.
"""

from __future__ import annotations

import asyncio
import re
import shutil
import subprocess
import sys
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import typer

from code_review import __version__
from code_review.agent import ClaudeCLI
from code_review.install_state import state_dir
from code_review.pipeline import StepContext, StepEvent, run_steps
from code_review.run_log import RunLogWriter, run_log_path
from code_review.steps.intent import Intent
from code_review.steps.registry import IMPLEMENTED_STEPS, STEP_DISPLAY_NAMES, STEP_REGISTRY
from code_review.steps.worktree import remove_worktree
from code_review.tui.activity import ActivityRelay
from code_review.tui.app import ReviewApp
from code_review.tui.approval_relay import ApprovalRelay
from code_review.tui.input_relay import InputRelay
from code_review.tui.state import final_status_message

app = typer.Typer(help="Agentic code-review/gating pipeline.")

PACKAGE_NAME = "code-review"


def _version_callback(show_version: bool) -> None:
    """Eager `--version` handler: print and exit before any other option/command resolves.
    Reads `code_review.__version__` directly (kept in sync with `pyproject.toml` by hand)
    rather than `importlib.metadata`, so it works against an editable/`uv run` checkout
    with no installed distribution metadata."""

    if show_version:
        typer.echo(f"{PACKAGE_NAME} {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(
        False,
        "--version",
        callback=_version_callback,
        is_eager=True,
        help="Show the installed code-review version and exit.",
    ),
) -> None:
    """Agentic code-review/gating pipeline."""


_UV_NOT_FOUND_MESSAGE = (
    "error: 'uv' is not installed or not on PATH.\n"
    "code-review is managed via uv -- install it first, then retry:\n"
    "  https://docs.astral.sh/uv/getting-started/installation/"
)

# Matches uv's "+ code-review==<version> (from <source>[@<git-rev>])" upgrade-line output.
_UPGRADE_LINE = re.compile(
    rf"^\s*\+\s*{re.escape(PACKAGE_NAME)}==(?P<version>\S+)"
    rf"(?:\s*\(from .*?@(?P<rev>[0-9a-f]{{7,40}})\))?",
    re.MULTILINE,
)


def _require_uv() -> str:
    uv = shutil.which("uv")
    if uv is None:
        typer.echo(_UV_NOT_FOUND_MESSAGE, err=True)
        raise typer.Exit(code=1)
    return uv


def _run_uv_tool_command(
    args: list[str], *, failure_prefix: str
) -> subprocess.CompletedProcess[str]:
    """Run a `uv tool ...` subcommand, echoing a clear message and exiting on failure."""

    uv = _require_uv()
    # --color never: uv still emits ANSI escapes in captured output otherwise, breaking
    # _UPGRADE_LINE's regex match.
    result = subprocess.run([uv, "--color", "never", *args], capture_output=True, text=True)
    if result.returncode != 0:
        typer.echo(f"{failure_prefix}: {result.stderr.strip()}", err=True)
        raise typer.Exit(code=result.returncode)
    return result


def _complete_branch(ctx: object, args: list[str], incomplete: str) -> list[str]:
    """List local git branch names for `review BRANCH` shell completion. Returns no
    candidates (not an error) if `git` is missing or the cwd isn't a repo.
    """
    git = shutil.which("git")
    if git is None:
        return []

    result = subprocess.run(
        [git, "for-each-ref", "--format=%(refname:short)", "refs/heads/"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return []

    return [name for name in result.stdout.splitlines() if name.startswith(incomplete)]


def _describe_upgrade(stderr: str) -> str:
    """Turn `uv tool upgrade`'s stderr into a clear, specific one-line report."""
    if "Nothing to upgrade" in stderr:
        return f"{PACKAGE_NAME} is already up to date."

    match = _UPGRADE_LINE.search(stderr)
    if match is None:
        # Exit code says success even if stderr doesn't match the expected shape.
        return f"{PACKAGE_NAME} was upgraded."

    version = match.group("version")
    rev = match.group("rev")
    if rev:
        return f"{PACKAGE_NAME} upgraded to {version} ({rev[:12]})."
    return f"{PACKAGE_NAME} upgraded to {version}."


_NOT_A_TTY_MESSAGE = (
    "error: 'code-review review' needs an interactive terminal on both stdin and stdout to "
    "render its live progress view.\n"
    "Run it directly in a terminal - don't pipe or redirect its input or output, and don't "
    "run it from a non-interactive script."
)


def _require_git() -> str:
    git = shutil.which("git")
    if git is None:
        typer.echo("error: 'git' is not installed or not on PATH.", err=True)
        raise typer.Exit(code=1)
    return git


def _verify_branch(git: str, branch: str, cwd: Path) -> None:
    """Raise a clear, code-review-specific error if BRANCH isn't a valid ref in `cwd`,
    instead of letting `git diff` fail later with its own "ambiguous argument" phrasing.
    """
    verify = subprocess.run(
        [git, "rev-parse", "--verify", "--quiet", f"{branch}^{{commit}}"],
        cwd=cwd,
        capture_output=True,
        text=True,
    )
    if verify.returncode != 0:
        typer.echo(f"error: '{branch}' is not a valid branch or ref in this repository.", err=True)
        raise typer.Exit(code=1)


def _verify_branch_exists(branch: str) -> None:
    """Fast pre-flight check `review` runs before starting the TUI: only the cheap
    `git rev-parse --verify`, not the full diff capture (deferred to `_run_pipeline` so a
    large diff doesn't block the TUI from appearing). Runs against `Path.cwd()` -- the
    user's real repo -- since this check happens before the worktree exists.
    """
    _verify_branch(_require_git(), branch, Path.cwd())


# Default branch every step this pipeline runs diffs/rebases against. Matches
# `RebaseStep`/`PRStep`'s own `default_branch: str = "main"` constructor field -- not a
# shared constant, just the same not-auto-detected convention repeated here (see those
# steps' own module docstrings for why).
_DEFAULT_BRANCH = "main"


def _diff_against_default_branch(
    branch: str, cwd: Path, default_branch: str = _DEFAULT_BRANCH
) -> str:
    """Return `git diff origin/<default_branch>...<branch>`: BRANCH's changes since its
    merge-base with the default branch. `cwd` is the user's real repo -- this never depends
    on which branch happens to be checked out there (no `HEAD` reference at all), so unlike
    `WorktreeStep`'s own worktree creation, this does not need to wait for or run inside the
    worktree.

    Diffs against `origin/<default_branch>`, never the literal local `<default_branch>` ref
    -- mirrors `steps/rebase.py`'s/`steps/pr.py`'s own reasoning for the identical
    local-vs-origin staleness tradeoff (the local ref can be arbitrarily stale, never
    pulled). Never `HEAD...<branch>` either: once `WorktreeStep` checks `<branch>` out for
    real elsewhere, `HEAD` in *that* worktree and `<branch>` would be the same ref -- a
    same-ref diff would always be empty -- but this function never touches `HEAD` in the
    first place, so it stays correct regardless of what `cwd`'s own HEAD happens to be.
    """
    git = _require_git()
    _verify_branch(git, branch, cwd)

    result = subprocess.run(
        [git, "diff", f"origin/{default_branch}...{branch}"],
        cwd=cwd,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        typer.echo(result.stderr.strip(), err=True)
        raise typer.Exit(code=result.returncode)
    return result.stdout


async def _run_pipeline(
    branch: str,
    intent: Intent,
    agent: ClaudeCLI,
    relay: InputRelay,
    cwd: Path,
    activity_relay: ActivityRelay | None = None,
    approval_relay: ApprovalRelay | None = None,
) -> AsyncIterator[StepEvent]:
    """Build the events `ReviewApp` renders: fetch the diff, then run every implemented
    step against it, in order. `cwd` is the user's real repo -- `steps/worktree.py`'s
    `WorktreeStep`, the first step in `IMPLEMENTED_STEPS`, redirects `ctx.cwd` to a fresh
    worktree for every step after it; nothing here needs to know that path itself.

    `_diff_against_default_branch` runs in a worker thread so a slow `git diff` capture
    doesn't delay the TUI's first paint, and runs against `cwd` (the real repo) too --
    its content (`origin/<default>...<branch>`) never depends on which working directory it
    runs from, so it does not need to wait for `WorktreeStep`. `activity_relay`/
    `approval_relay` are optional (default `None`) and become `ctx.activity_reporter`/
    `ctx.on_approval_needed`; `review` below always passes real ones.
    """
    diff = await asyncio.to_thread(_diff_against_default_branch, branch, cwd)
    ctx = StepContext(
        cwd=cwd,
        branch=branch,
        agent=agent,
        diff=diff,
        intent=intent,
        on_input_needed=relay.request_input,
        activity_reporter=activity_relay,
        on_approval_needed=None if approval_relay is None else approval_relay.request_approval,
    )
    steps = [cls() for cls in IMPLEMENTED_STEPS]
    async for event in run_steps(steps, ctx):
        yield event


async def _log_step_events(
    events: AsyncIterator[StepEvent], writer: RunLogWriter
) -> AsyncIterator[StepEvent]:
    """Tee `events` through `writer.write_step_event` as they pass, so the persisted run log
    (`run_log.py`) never falls behind what `ReviewApp` is rendering -- a pure pass-through
    from `ReviewApp`'s point of view."""
    async for event in events:
        writer.write_step_event(event)
        yield event


@dataclass
class _WorktreePathCapture:
    """Mutable holder `_capture_worktree_path` fills in as `WorktreeStep`'s "completed"
    `StepEvent` passes through -- `review` reads `.path` once `tui_app.run()` returns to know
    what (if anything) to clean up. Stays `None` if `WorktreeStep` never completes (e.g. it
    itself raised) -- nothing to clean up in that case."""

    path: Path | None = None


async def _capture_worktree_path(
    events: AsyncIterator[StepEvent], capture: _WorktreePathCapture
) -> AsyncIterator[StepEvent]:
    """Tee `events`, recording `WorktreeStep`'s real worktree path (its `StepOutcome.
    cwd_override`) into `capture` as it passes -- so `review` can clean it up (or report it,
    for `--keep-worktree`) after the TUI exits, without threading it through `ReviewApp`
    itself or re-deriving it a second time."""
    async for event in events:
        if event.step_name == "WorktreeStep" and event.status == "completed":
            assert event.outcome is not None  # a "completed" event always carries one
            capture.path = event.outcome.cwd_override
        yield event


@app.command()
def review(
    branch: str = typer.Argument(
        ..., help="Branch or ref to review.", autocompletion=_complete_branch
    ),
    intent: str = typer.Option(..., "--intent", help="What this change is trying to do."),
    keep_worktree: bool = typer.Option(
        False,
        "--keep-worktree",
        help="Leave this run's throwaway git worktree on disk instead of removing it when "
        "the run ends.",
    ),
) -> None:
    """Run the review pipeline against BRANCH, rendered live in a full-screen terminal UI."""
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        typer.echo(_NOT_A_TTY_MESSAGE, err=True)
        raise typer.Exit(code=1)

    stripped_intent = intent.strip()
    if not stripped_intent:
        raise typer.BadParameter("must be non-empty and not just whitespace", param_hint="--intent")

    parsed_intent = Intent(summary=stripped_intent, source="explicit", score=1.0)
    _verify_branch_exists(branch)

    agent = ClaudeCLI()
    relay = InputRelay()
    writer = RunLogWriter(run_log_path(branch))
    activity_relay = ActivityRelay(on_event=writer.write_activity_event)
    approval_relay = ApprovalRelay()
    worktree_capture = _WorktreePathCapture()

    try:
        tui_app = ReviewApp(
            STEP_REGISTRY,
            _capture_worktree_path(
                _log_step_events(
                    _run_pipeline(
                        branch,
                        parsed_intent,
                        agent,
                        relay,
                        Path.cwd(),
                        activity_relay,
                        approval_relay,
                    ),
                    writer,
                ),
                worktree_capture,
            ),
            input_relay=relay,
            activity_relay=activity_relay,
            approval_relay=approval_relay,
            branch=branch,
            display_names=STEP_DISPLAY_NAMES,
        )
        tui_app.run()
        asyncio.run(agent.close())

        writer.write_line(
            final_status_message(
                tui_app.error, report=tui_app.run_report, display_names=STEP_DISPLAY_NAMES
            )
        )
        writer.close()
        typer.echo(f"Run log written to {writer.path}")

        if tui_app.error is not None:
            # One generic path for any pipeline failure, including RunAbortedError (a human
            # chose "abort" on a parked step's approval request) and any plain RuntimeError
            # from a step's own git/gh subprocess work -- each message already names the
            # step/problem, so no dedicated branch per exception type is needed.
            typer.echo(f"code-review review failed: {tui_app.error}", err=True)
            raise typer.Exit(code=1)
    finally:
        # Runs on every outcome -- success, pipeline failure, or any uncaught exception --
        # so the worktree never outlives one run unless the user explicitly asked to keep
        # it. worktree_capture.path is None if WorktreeStep never completed (e.g. it itself
        # raised) -- nothing to clean up then.
        if worktree_capture.path is not None:
            if keep_worktree:
                typer.echo(f"--keep-worktree: worktree left in place at {worktree_capture.path}")
            else:
                remove_worktree(_require_git(), Path.cwd(), worktree_capture.path)


@app.command()
def update() -> None:
    """Upgrade the installed code-review tool to the latest version via `uv tool upgrade`."""
    result = _run_uv_tool_command(
        ["tool", "upgrade", PACKAGE_NAME], failure_prefix="code-review update failed"
    )

    typer.echo(_describe_upgrade(result.stderr))


@app.command()
def uninstall() -> None:
    """Remove the code-review tool and this project's own state directory."""
    _run_uv_tool_command(
        ["tool", "uninstall", PACKAGE_NAME], failure_prefix="code-review uninstall failed"
    )

    directory = state_dir()
    if directory.exists():
        shutil.rmtree(directory)

    typer.echo(f"{PACKAGE_NAME} has been uninstalled, including its state directory.")


if __name__ == "__main__":
    app()
