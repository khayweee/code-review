"""Intent representation and `IntentStep`, the first step to actually answer "what is this
change trying to do" -- `worktree.py`'s `WorktreeStep` runs before it in `STEP_REGISTRY`
(pure environment setup, not part of answering that question).

Requires `--intent` explicitly; no transcript inference yet. Prompt-construction helpers
(`wrap_intent`, `redact_secrets`, `strip_adversarial`) live in `code_review.prompt.intent`,
not here -- this module has no dependency on `prompt/`.
"""

from __future__ import annotations

from dataclasses import dataclass

from code_review.pipeline.step import Step, StepContext, StepOutcome

# --- Intent ------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Intent:
    """What a change should achieve, with metadata describing where that claim came from.

    Only `summary` and `source` have runtime behavior today; `score` and `session_id` are
    reserved for future transcript-based inference.
    """

    # Acceptance criteria in human-readable form.
    summary: str

    # Provenance of summary: "explicit" for CLI input, or a future inference backend name
    # (e.g. "claude"). Open string so adding a backend needs no schema change.
    source: str

    # Confidence in summary, 0.0-1.0. Explicit CLI input is always 1.0. Reserved for
    # future inference; no current consumer.
    score: float

    # Agent session the intent was inferred from. None for explicit CLI input. Reserved
    # for future inference; no current consumer.
    session_id: str | None = None


# --- IntentStep ------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class IntentStep(Step):
    """The pipeline's first step: reports `ctx.intent` (set from the CLI's `--intent`
    flag) as its findings. No agent call, no prompt wrapping -- downstream steps call
    `wrap_intent` themselves off `ctx.intent`.
    """

    async def run(self, ctx: StepContext) -> StepOutcome:
        # Defense in depth: the type system already requires ctx.intent, but a caller
        # constructing StepContext directly could bypass that.
        if ctx.intent is None:
            raise ValueError("StepContext.intent is required but was None")

        return StepOutcome(
            needs_approval=False,
            auto_fixable=False,
            payload=ctx.intent,
        )
