"""Helpers for wiring StreamEvent callbacks into agent calls.

Decoupled module so steps don't need to know about streaming; just call
run_with_streaming(ctx, opts) instead of ctx.agent.run(opts).
"""

from __future__ import annotations

from typing import TypeVar

from pydantic import BaseModel

from code_review.agent.base import Result, RunOpts
from code_review.pipeline.step import StepContext

OutputT = TypeVar("OutputT", bound=BaseModel)


async def run_with_streaming(ctx: StepContext, opts: RunOpts[OutputT]) -> Result[OutputT]:
    """Call agent.run() with streaming wired to the pipeline context.

    If ctx.on_stream_event is set, passes it through to opts.on_stream_event.
    Otherwise, calls agent.run() normally (silent, no streaming).

    This is the single call site steps should use instead of ctx.agent.run(opts)
    directly, to enable streaming observability across all agent calls.
    """

    # Wire streaming from StepContext into RunOpts, preserving all other fields
    opts_with_streaming = RunOpts(
        prompt=opts.prompt,
        cwd=opts.cwd,
        output_schema=opts.output_schema,
        executable=opts.executable,
        model=opts.model,
        system_prompt=opts.system_prompt,
        append_system_prompt=opts.append_system_prompt,
        tools_allowlist=opts.tools_allowlist,
        permission_mode=opts.permission_mode,
        on_input_needed=opts.on_input_needed,
        on_stream_event=ctx.on_stream_event
        if opts.on_stream_event is None
        else opts.on_stream_event,
    )

    return await ctx.agent.run(opts_with_streaming)
