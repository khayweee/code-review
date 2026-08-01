"""Backend-agnostic contract for one Agent call."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Generic, Protocol, TypeVar

from pydantic import BaseModel

OutputT = TypeVar("OutputT", bound=BaseModel)


@dataclass(frozen=True, slots=True)
class RunOpts(Generic[OutputT]):
    """Everything a backend needs to perform one isolated Agent call."""

    prompt: str  # sent over stdin, never argv, to avoid per-argument size limits
    cwd: Path  # working directory the backend subprocess runs in
    # pydantic model the answer must validate against
    output_schema: type[OutputT]
    # subprocess test seam; swap for a fake CLI in tests
    executable: str | Path = "claude"
    model: str = "sonnet"  # backend model alias/name for this call
    # replaces the backend's default system prompt when set
    system_prompt: str | None = None
    # adds instructions, keeps the default; prefer this
    append_system_prompt: str | None = None
    # scopes permissions to this list via --allowedTools; empty means no scoped list
    tools_allowlist: tuple[str, ...] = ()
    # None: no permission mode pinned by the caller, so the backend defaults to
    # --dangerously-skip-permissions
    # Set this to opt out of that default.
    permission_mode: str | None = None


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
