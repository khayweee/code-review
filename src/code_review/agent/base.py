"""Backend-agnostic contract for one Agent call."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Generic, Protocol, TypeVar

from pydantic import BaseModel

from code_review.agent.streaming import StreamEvent

OutputT = TypeVar("OutputT", bound=BaseModel)


@dataclass(frozen=True, slots=True)
class RunOpts(Generic[OutputT]):
    """Everything a backend needs to perform one run (one isolated Agent call; see
    `docs/GLOSSARY.md`'s "run" vs. a whole pipeline run).

    `on_input_needed` is only reachable when `tools_allowlist` is non-empty or
    `permission_mode` is set -- either opts out of the default
    `--dangerously-skip-permissions` fast path and routes the call through the
    stdin-relay path instead (see `claude_cli.py`).

    `on_stream_event` enables live streaming of tool calls and results for TUI display.
    When set, uses Claude CLI's --verbose --output-format stream-json; when None,
    uses legacy --output-format json (silent mode, backward compatible).
    """

    prompt: str  # sent over stdin, not argv, to avoid per-argument size limits
    cwd: Path  # working directory the backend subprocess runs in
    output_schema: type[OutputT]  # pydantic model the answer must validate against
    executable: str | Path = "claude"  # subprocess test seam; swap for a fake CLI in tests
    model: str = "sonnet"  # backend model alias/name for this call
    system_prompt: str | None = None  # replaces the backend's default system prompt
    append_system_prompt: str | None = None  # adds instructions, keeps the default; prefer this
    tools_allowlist: tuple[
        str, ...
    ] = ()  # scopes permissions via --allowedTools; empty = no scoping
    # None: no mode pinned, so the backend defaults to --dangerously-skip-permissions.
    permission_mode: str | None = None
    # Called with the detected prompt text when the subprocess looks blocked on stdin;
    # must return the answer to write back. None means fail closed with
    # StdinBlockedError rather than hang or fabricate an answer.
    on_input_needed: Callable[[str], Awaitable[str]] | None = None
    # Called with each StreamEvent as the agent executes; enables TUI/observer live display.
    # None means no streaming callbacks (silent mode, backward compatible).
    on_stream_event: Callable[[StreamEvent], Awaitable[None]] | None = None


@dataclass(frozen=True, slots=True)
class Usage:
    """Usage values reported by a backend; ``None`` always means unknown."""

    input_tokens: int | None = None
    output_tokens: int | None = None
    total_cost_usd: float | None = None


@dataclass(frozen=True, slots=True)
class Result(Generic[OutputT]):
    """A schema-validated answer together with the backend's original response."""

    output: OutputT
    text: str
    usage: Usage | None = None


class Agent(Protocol):
    """One call in, one result out, plus teardown."""

    async def run(self, opts: RunOpts[OutputT]) -> Result[OutputT]:
        """Run one prompt and validate its answer against ``opts.output_schema``."""
        ...

    async def close(self) -> None:
        """Release resources owned by this Agent."""
        ...
