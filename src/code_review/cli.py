"""CLI entry point (`code-review`).

Milestone 1+ wires this up to a real pipeline run. For now it only proves the Typer
app is installed and importable.
"""

from __future__ import annotations

import typer

app = typer.Typer(help="Agentic code-review/gating pipeline.")


@app.command()
def review(
    branch: str = typer.Argument(..., help="Branch or ref to review."),
    intent: str = typer.Option(..., "--intent", help="What this change is trying to do."),
) -> None:
    """Run the review pipeline against BRANCH (not implemented yet)."""
    raise NotImplementedError(
        "review pipeline not implemented yet — see docs/ROADMAP.md milestones 1-7"
    )


if __name__ == "__main__":
    app()
