"""Backend-agnostic contract for one Agent call."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Generic, Protocol, TypeVar

from pydantic import BaseModel

OutputT = TypeVar("OutputT", bound=BaseModel)


@dataclass(frozen=True, slots=True)
class RunOpts(Generic[OutputT]):
    """Everything a backend needs to perform one isolated Agent call.

    Whether `on_input_needed` (see its field comment below) is ever reachable depends on
    `tools_allowlist` and `permission_mode` together, since both feed `claude_cli.py`'s
    `_build_args` decision to append `--dangerously-skip-permissions` or route the call
    through the stdin-relay path instead (see `agent/AGENTS.md`):

    | `tools_allowlist` | `permission_mode` | CLI flag(s)                            | reachable? |
    |---|---|---|---|
    | empty (`()`) | `None` | `--dangerously-skip-permissions`                  | No -- fast path |
    | empty (`()`) | set    | `--permission-mode <value>`                       | Yes |
    | non-empty    | `None` | `--allowedTools ... --permission-mode auto`       | Yes |
    | non-empty    | set    | `--allowedTools ... --permission-mode <value>`    | Yes |

    In short: only the pure default (`tools_allowlist` empty and `permission_mode` `None`)
    skips permissions and stays on the untouched fast path. Setting either one at all opts
    into the stdin-relay path -- the specific non-default `permission_mode` string doesn't
    change reachability, only which flag value is passed to the CLI.
    """

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
    # Invoked with the detected prompt text when the backend subprocess appears blocked
    # waiting on stdin, and expected to return the human's answer to write back. Only
    # reachable once `permission_mode` opts out of the skip-permissions default above --
    # the default `--dangerously-skip-permissions` path never blocks on stdin, so this is
    # never consulted there. `None` (the default for every existing call site) means no
    # relay is available: the backend fails closed with `StdinBlockedError` instead of
    # hanging or fabricating an answer. Consumer: `claude_cli.py`'s non-default-permission
    # read/write loop; supplied by `tui.input_relay.InputRelay.request_input` for
    # interactive runs (see `cli.py`'s `review` command).
    on_input_needed: Callable[[str], Awaitable[str]] | None = None


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
