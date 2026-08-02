"""CLI entry point (`code-review`).

Milestone 1+ wires this up to a real pipeline run. For now it only proves the Typer
app is installed and importable.

Milestone 3 (issue #19) adds validation and construction of the explicit `Intent` from
`--intent`: `Intent` is fully known before the pipeline starts (it's a CLI flag, not
something discovered mid-run), so it is constructed once here, before anything else, and
threaded through the same immutable `StepContext` every step receives. Wiring
`run_steps`/`StepContext` construction into this command is out of scope for #19 (see
issue #17's Out of Scope list) -- `review` still raises `NotImplementedError` after intent
validation.

Milestone 12 (issues #31-#33) adds `update` and `uninstall` alongside `review`, so the
full install lifecycle is discoverable via `code-review --help` once installed (see
`scripts/install.sh` for first-time install). Both shell out to `uv tool
upgrade`/`uv tool uninstall` for this package -- no dependency resolution, virtual
environment management, or binary replacement is reimplemented here; `uv` already owns
all of that.
"""

from __future__ import annotations

import re
import shutil
import subprocess

import typer

from code_review.install_state import state_dir
from code_review.steps.intent import Intent

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
    result = subprocess.run([uv, *args], capture_output=True, text=True)
    if result.returncode != 0:
        typer.echo(f"{failure_prefix}: {result.stderr.strip()}", err=True)
        raise typer.Exit(code=result.returncode)
    return result


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


@app.command()
def review(
    branch: str = typer.Argument(..., help="Branch or ref to review."),
    intent: str = typer.Option(..., "--intent", help="What this change is trying to do."),
) -> None:
    """Run the review pipeline against BRANCH (not implemented yet)."""
    stripped_intent = intent.strip()
    if not stripped_intent:
        raise typer.BadParameter("must be non-empty and not just whitespace", param_hint="--intent")

    # Proves the flag round-trips into a validated Intent end to end. `run_steps`/
    # `StepContext` construction is not part of this milestone (see module docstring),
    # so the constructed Intent isn't used yet -- bind to `_` rather than an unused named
    # variable so this reads as intentional, not dead code.
    _ = Intent(summary=stripped_intent, source="explicit", score=1.0)

    raise NotImplementedError(
        "review pipeline not implemented yet — see docs/ROADMAP.md milestones 1-7"
    )


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
