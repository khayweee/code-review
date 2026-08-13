"""Correctness/alignment review + risk assessment schema, and `ReviewStep` itself.

`ReviewStep` is registered in `steps/registry.py`'s `IMPLEMENTED_STEPS`, which `cli.py`
builds its step list from directly.

Single prompt, single schema: `ReviewOutput` carries findings *and* required
`risk_level`/`risk_rationale` together, so the agent structurally cannot return a review
without a risk assessment.

This module holds only the schema (`ReviewOutput`) and step-orchestration
(`ReviewStep`). Prompt construction lives in `code_review.prompt.review`; the
pipeline-owned-delivery scope filter (`filter_pipeline_owned_delivery_findings`) lives in
`code_review.pipeline.findings`.

`ReviewStep` sets `supports_fix_round = True`, opting into `pipeline/executor.py`'s
fix-before-park round. `run`'s only branch on `ctx.fix_round` is which prompt-assembly
function to call (`build_review_fix_prompt` vs `build_review_prompt`); everything else is
identical between rounds.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar, Literal

from pydantic import BaseModel

from code_review.agent import RunOpts
from code_review.agent.streaming import StreamEvent, StreamEventType
from code_review.pipeline.findings import (
    Finding,
    action_or_default,
    filter_pipeline_owned_delivery_findings,
    has_blocking_finding,
)
from code_review.pipeline.step import ActivityReporter, Step, StepContext, StepOutcome
from code_review.prompt.review import build_review_fix_prompt, build_review_prompt

# --- ReviewOutput ----------------------------------------------------------------------


class ReviewOutput(BaseModel):
    """The Review step's schema: correctness findings plus a required risk verdict.
    `findings` is ordered first so the agent reasons about issues before summarizing risk.
    """

    # Structured observations (see `pipeline/findings.py`'s `Finding`).
    findings: list[Finding]

    # Overall risk level; required so the agent cannot skip a risk verdict.
    risk_level: Literal["low", "medium", "high"]

    # Justification for risk_level. Overwritten (not appended to) by
    # filter_pipeline_owned_delivery_findings when it resets risk_level.
    risk_rationale: str

    # Delivery scope the agent judges its risk assessment to be about. Reserved: no code
    # reads this field yet.
    risk_scope: Literal["source-or-external", "pipeline-owned-delivery"] | None = None


def _tool_activity_label(tool_name: str, tool_input: dict[str, Any]) -> str:
    """Render one tool call as an activity label, e.g. `Tool: Read(/path/to/file)`. Falls
    back to a bare `Tool: <name>` when none of the common single-argument shapes apply.
    """

    primary = (
        tool_input.get("file_path")
        or tool_input.get("command")
        or tool_input.get("pattern")
        or tool_input.get("path")
    )
    return f"Tool: {tool_name}({primary})" if primary else f"Tool: {tool_name}"


def _tool_stream_relay(
    reporter: ActivityReporter | None,
) -> Callable[[StreamEvent], Awaitable[None]]:
    """Build an `on_stream_event` callback that reports each `TOOL_USE`/`TOOL_RESULT` pair
    as its own nested `reporter.activity(...)` span, keyed by the tool call's own id.

    A `StreamEvent` carries a point-in-time moment, not a single `async with` block, so
    each tool's span is opened on `TOOL_USE` and closed later on its matching
    `TOOL_RESULT` via an `AsyncExitStack` kept alive in `open_tools` between the two calls.
    """

    open_tools: dict[str, AsyncExitStack] = {}

    async def relay(event: StreamEvent) -> None:
        if reporter is None:
            return
        if event.type is StreamEventType.TOOL_USE:
            tool_id = event.payload.get("tool_id")
            if tool_id is None:
                return
            stack = AsyncExitStack()
            tool_input = event.payload.get("input") or {}
            label = _tool_activity_label(event.payload["tool_name"], tool_input)
            await stack.enter_async_context(reporter.activity(label))
            open_tools[tool_id] = stack
        elif event.type is StreamEventType.TOOL_RESULT:
            result_tool_id = event.payload.get("tool_id")
            closing_stack = (
                open_tools.pop(result_tool_id, None) if result_tool_id is not None else None
            )
            if closing_stack is not None:
                await closing_stack.aclose()

    return relay


# --- ReviewStep --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ReviewStep(Step):
    """Single-pass correctness/alignment review and risk assessment: one prompt built by
    `build_review_prompt`, exactly one `ctx.agent.run` call against `ReviewOutput`, and the
    pipeline-owned-delivery scope filter applied to the parsed answer.

    Single agent call per round -- no retry, no session persistence. The fix/approval loop
    that decides whether to re-run this step lives in `pipeline/executor.py`; this class
    only reacts to `ctx.fix_round` by choosing which prompt-assembly function to call.

    The agent call is wrapped in one coarse `ctx.report_activity(...)` span (the `Agent`
    protocol has no finer-grained progress channel).
    """

    # Subprocess test seam for RunOpts.executable; tests point this at a fake CLI script.
    executable: str | Path = "claude"

    # Opts into pipeline/executor.py's fix-round loop.
    supports_fix_round: ClassVar[bool] = True

    async def run(self, ctx: StepContext) -> StepOutcome:
        prompt = (
            build_review_fix_prompt(ctx) if ctx.fix_round is not None else build_review_prompt(ctx)
        )

        # Static label ("via claude"), not self.executable -- that field is a test seam,
        # this names the production backend. Reports "finished" even if the call raises.
        async with ctx.report_activity("Agent: reviewing diff via claude"):
            # None with no reporter attached (rather than a relay that's a no-op at
            # runtime) so the call stays on claude_cli.py's legacy --output-format json
            # path when there's nothing to stream tool calls to -- e.g. every test that
            # runs ReviewStep against a fake CLI without a StepContext.activity_reporter.
            on_stream_event = (
                _tool_stream_relay(ctx.activity_reporter)
                if ctx.activity_reporter is not None
                else None
            )
            result = await ctx.agent.run(
                RunOpts(
                    prompt=prompt,
                    cwd=ctx.cwd,
                    output_schema=ReviewOutput,
                    executable=self.executable,
                    on_stream_event=on_stream_event,
                )
            )

        filtered = filter_pipeline_owned_delivery_findings(result.output)

        # True iff a surviving finding's resolved action is "ask-user".
        blocking = has_blocking_finding(filtered.findings)
        has_auto_fix = any(
            action_or_default(finding.action) == "auto-fix" for finding in filtered.findings
        )
        auto_fixable = has_auto_fix and not blocking

        return StepOutcome(
            needs_approval=blocking,
            auto_fixable=auto_fixable,
            payload=filtered,
        )
