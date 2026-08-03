"""CLI entry point (`code-review`).

Milestone 1+ wires this up to a real pipeline run. For now it only proves the Typer
app is installed and importable.

Milestone 3 (issue #19) adds validation and construction of the explicit `Intent` from
`--intent`: `Intent` is fully known before the pipeline starts (it's a CLI flag, not
something discovered mid-run), so it is constructed once here, before anything else, and
threaded through the same immutable `StepContext` every step receives.

Milestone 12 (issues #31-#33) adds `update` and `uninstall` alongside `review`, so the
full install lifecycle is discoverable via `code-review --help` once installed (see
`scripts/install.sh` for first-time install). Both shell out to `uv tool
upgrade`/`uv tool uninstall` for this package -- no dependency resolution, virtual
environment management, or binary replacement is reimplemented here; `uv` already owns
all of that.

Milestone 13's #40 wires `review` up for real: it requires a real TTY on both stdin and
stdout (the live progress view cannot render into a pipe or redirect), then starts
`tui.app.ReviewApp` immediately off `_run_pipeline` -- an async generator that builds
`StepContext` from a `git diff HEAD...<branch>` against the current checkout (fetched in a
worker thread, off the TUI's own event loop, so a slow diff can't delay the TUI's first
paint) and runs the fixed prefix of implemented steps (`steps.registry.IMPLEMENTED_STEPS`)
through `run_steps`. Only the cheap `git rev-parse --verify` ref check
(`_verify_branch_exists`) runs synchronously before the TUI starts, so a bad BRANCH still
fails fast with no TUI flash. See `docs/ROADMAP.md` milestone 13 and `tui/AGENTS.md` for
the design; this docstring only tracks what's wired where.
"""

from __future__ import annotations

import asyncio
import re
import shutil
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import typer

from code_review.agent import ClaudeCLI
from code_review.install_state import state_dir
from code_review.pipeline import StepContext, StepEvent, run_steps
from code_review.steps.intent import Intent
from code_review.steps.registry import IMPLEMENTED_STEPS, STEP_REGISTRY
from code_review.tui.activity import ActivityRelay
from code_review.tui.app import ReviewApp
from code_review.tui.input_relay import InputRelay

app = typer.Typer(help="Agentic code-review/gating pipeline.")

PACKAGE_NAME = "code-review"

_UV_NOT_FOUND_MESSAGE = (
    "error: 'uv' is not installed or not on PATH.\n"
    "code-review is managed via uv -- install it first, then retry:\n"
    "  https://docs.astral.sh/uv/getting-started/installation/"
)

# Matches uv's own "+ code-review==<version> (from <source>[@<git-rev>])" line, printed
# for both a fresh reinstall and a real version bump -- the same shape either way.
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
    """Run a `uv tool ...` subcommand, echoing a clear message and exiting on failure.

    Shared by `update`/`uninstall`, which differ only in which `uv tool` subcommand they
    run and how they interpret a successful result.
    """

    uv = _require_uv()
    # `--color never` keeps stderr plain text regardless of environment/terminal color
    # detection -- observed in practice: `uv` still emits ANSI escapes in captured
    # (non-TTY) output, which breaks `_UPGRADE_LINE`'s regex match on the version line.
    result = subprocess.run([uv, "--color", "never", *args], capture_output=True, text=True)
    if result.returncode != 0:
        typer.echo(f"{failure_prefix}: {result.stderr.strip()}", err=True)
        raise typer.Exit(code=result.returncode)
    return result


def _complete_branch(ctx: object, args: list[str], incomplete: str) -> list[str]:
    """List local git branch names for `review BRANCH` shell completion.

    Returns no candidates (not an error) if `git` is missing or the cwd isn't a repo --
    shell completion must never crash the shell it runs inside.
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
        # `uv` reported success but not in the shape this parses -- still don't claim
        # nothing happened, since the command's own exit code says it succeeded.
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


def _verify_branch(git: str, branch: str) -> None:
    """Raise a clear, code-review-specific error if BRANCH isn't a valid ref in this repo.

    Checked separately from the `diff` call in `_diff_against_head` so a bad BRANCH gets
    this message instead of `git diff`'s own "ambiguous argument" phrasing, which talks
    about revision/path syntax that has nothing to do with this CLI's own arguments.
    """
    verify = subprocess.run(
        [git, "rev-parse", "--verify", "--quiet", f"{branch}^{{commit}}"],
        cwd=Path.cwd(),
        capture_output=True,
        text=True,
    )
    if verify.returncode != 0:
        typer.echo(f"error: '{branch}' is not a valid branch or ref in this repository.", err=True)
        raise typer.Exit(code=1)


def _verify_branch_exists(branch: str) -> None:
    """Fast, synchronous pre-flight check `review` runs before starting the TUI: only
    `git rev-parse --verify` (cheap), not the full `git diff` capture (`_diff_against_head`
    below) -- that one can be slow on a large diff and is deferred until after the TUI's
    own event loop is running (see `_run_pipeline`), so a bad BRANCH still fails instantly
    with no TUI flash, while a large diff no longer blocks the TUI from appearing at all.
    """
    _verify_branch(_require_git(), branch)


def _diff_against_head(branch: str) -> str:
    """Return `git diff HEAD...<branch>`: BRANCH's changes since its merge-base with the
    current HEAD. Diff-base semantics beyond "against current HEAD" (e.g. against a
    configured default branch instead) are explicitly out of scope here - Rebase/Review own
    that once they land (see docs/ROADMAP.md milestones 4-5)."""
    git = _require_git()
    _verify_branch(git, branch)

    result = subprocess.run(
        [git, "diff", f"HEAD...{branch}"], cwd=Path.cwd(), capture_output=True, text=True
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
    activity_relay: ActivityRelay | None = None,
) -> AsyncIterator[StepEvent]:
    """Build the events `ReviewApp` renders: fetch the diff, then run every implemented
    step against it, in order.

    `_diff_against_head` runs in a worker thread (`asyncio.to_thread`), not on the main
    thread -- by the time this generator's first iteration reaches it, `ReviewApp.run()`
    (`review` below) is already driving the terminal, so a slow `git diff` capture no
    longer delays the TUI's own first paint (all steps "pending"); it only delays that
    first paint from progressing to `IntentStep` actually starting.

    `activity_relay` (issue #66) becomes `ctx.activity_reporter` -- optional, defaulting to
    `None`, matching `StepContext.activity_reporter`'s own default so existing callers of
    this generator (e.g. `tests/test_cli_review.py`'s event-loop test) keep working
    unchanged. `review` below always passes a real one.
    """
    diff = await asyncio.to_thread(_diff_against_head, branch)
    ctx = StepContext(
        cwd=Path.cwd(),
        agent=agent,
        diff=diff,
        intent=intent,
        on_input_needed=relay.request_input,
        activity_reporter=activity_relay,
    )
    steps = [cls() for cls in IMPLEMENTED_STEPS]
    async for event in run_steps(steps, ctx):
        yield event


@app.command()
def review(
    branch: str = typer.Argument(
        ..., help="Branch or ref to review.", autocompletion=_complete_branch
    ),
    intent: str = typer.Option(..., "--intent", help="What this change is trying to do."),
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
    activity_relay = ActivityRelay()

    tui_app = ReviewApp(
        STEP_REGISTRY,
        _run_pipeline(branch, parsed_intent, agent, relay, activity_relay),
        input_relay=relay,
        activity_relay=activity_relay,
    )
    tui_app.run()
    asyncio.run(agent.close())

    if tui_app.error is not None:
        typer.echo(f"code-review review failed: {tui_app.error}", err=True)
        raise typer.Exit(code=1)


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
