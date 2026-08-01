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
"""

from __future__ import annotations

import typer

from code_review.steps.intent import Intent

app = typer.Typer(help="Agentic code-review/gating pipeline.")


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


if __name__ == "__main__":
    app()
