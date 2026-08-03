"""Intent representation and the pipeline's first step -- Milestone 3 (see
docs/ROADMAP.md).

v1 shortcut: require `--intent` explicitly; skip transcript inference. This module holds
only the `Intent` dataclass and `IntentStep`; the sanitize-and-wrap prompt-construction
functions (`wrap_intent`, `redact_secrets`, `strip_adversarial`) that used to live here
moved to `code_review.prompt.intent` in a later structural refactor, since they operate on
plain strings and never needed the `Intent` type -- import them from there. `IntentStep`
itself makes no agent call and never wraps a prompt (see its own docstring), so this
module has no dependency on `prompt/` at all.

Transcript-based inference (non-"explicit" provenance) is v2 (Milestone 11); nothing in
this module reads a transcript or calls an agent.
"""

from __future__ import annotations

from dataclasses import dataclass

from code_review.pipeline.step import Step, StepContext, StepOutcome

# --- Intent ------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Intent:
    """What a change should achieve, with metadata describing where that claim came from.

    Each field documents both its meaning and its consumer. In the explicit-only
    milestone, `summary` and `source` are the only fields with runtime behavior; `score`
    and `session_id` preserve the shape needed by future transcript-based inference.
    """

    # The acceptance criteria in human-readable form. Later Review, test-sufficiency,
    # and PR steps read this from StepContext and embed a sanitized copy in their prompts.
    summary: str

    # Provenance of the summary: currently "explicit" for CLI input; a future inference
    # step may record a backend name such as "claude" or "codex". `wrap_intent` uses it
    # to treat explicit input as authoritative and inferred input only as a hint. This is
    # deliberately an open string so adding a backend does not require a schema change.
    source: str

    # Confidence in the summary, on a 0.0-to-1.0 scale by convention. Explicit CLI input
    # is registered as 1.0; future transcript inference will assign lower values for
    # uncertain interpretations. No pipeline decision consumes this field yet.
    score: float

    # Identifier of the agent session from which intent was inferred, allowing future
    # diagnostics to trace the summary back to its source transcript. Explicit CLI input
    # has no session and leaves this as None. No pipeline decision consumes it yet.
    session_id: str | None = None


# --- IntentStep ------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class IntentStep(Step):
    """The pipeline's first step: proves `ctx.intent` is what the CLI's `--intent` flag
    supplied, with no agent call involved.

    `Intent` is fully known before the pipeline starts -- it's a CLI flag, not something
    discovered mid-run -- so `cli.py` constructs it once and `StepContext` carries it for
    every step. `IntentStep` therefore does no work of its own: it reports `ctx.intent` as
    its findings and makes no call through `ctx.agent`. It deliberately does NOT call
    `wrap_intent` here and does NOT hand wrapped text forward through `StepOutcome` --
    each downstream step calls `wrap_intent` (from `code_review.prompt.intent`) itself,
    off `ctx.intent`, at its own prompt site, when its own milestone lands.
    """

    async def run(self, ctx: StepContext) -> StepOutcome:
        # The type system already makes `ctx.intent` required; this check is defense in
        # depth for callers that construct a `StepContext` directly (e.g. tests, or a
        # future caller) and might bypass that typing.
        if ctx.intent is None:
            raise ValueError("StepContext.intent is required but was None")

        return StepOutcome(
            needs_approval=False,
            auto_fixable=False,
            findings=ctx.intent,
        )
