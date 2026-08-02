"""Subprocess adapter for non-interactive Claude CLI calls.

- Runs each Agent call in a fresh, non-interactive ``claude -p`` process.
- Sends the prompt over stdin and reads a structured JSON envelope back from stdout.
- Builds CLI args from ``RunOpts``: model, system prompt (replace or append), schema, and
  a tools allowlist or the default permission-skipping mode.
- Spawns the child in its own process group (session leader) so its PID doubles as the
  whole group's PGID, then hands teardown to ``process_group.terminate_process_group``
  (shared, backend-agnostic) so no descendant survives a call on any exit path.
- Raises one of four distinct errors (``ProcessStartError``, ``ProcessExitError``,
  ``NoStructuredOutputError``, ``OutputValidationError``) so callers can tell which stage
  failed.
"""

from __future__ import annotations

import asyncio
import json

from code_review.agent.base import OutputT, Result, RunOpts, Usage
from code_review.agent.errors import NoStructuredOutputError, ProcessExitError, ProcessStartError
from code_review.agent.process_group import terminate_process_group
from code_review.agent.schema import JsonValue, extract_json, validate_output


class ClaudeCLI:
    """Run each Agent call in a fresh Claude CLI process."""

    async def run(self, opts: RunOpts[OutputT]) -> Result[OutputT]:
        args = _build_args(opts)
        try:
            process = await asyncio.create_subprocess_exec(
                *args,
                cwd=opts.cwd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as exc:
            raise ProcessStartError(str(opts.executable), exc) from exc

        try:
            stdout, stderr = await process.communicate(opts.prompt.encode("utf-8"))
        finally:
            # Runs on every exit path - success, non-zero exit, a parse/validation
            # failure raised further down, and cancellation of this coroutine - so no
            # descendant the subprocess started is still running afterward. The process
            # group persists as long as any member is alive, regardless of whether the
            # direct child (the group leader) has already exited.
            await terminate_process_group(process)
        text = stdout.decode("utf-8")

        returncode = process.returncode
        assert returncode is not None  # communicate() only returns after the process exits
        if returncode != 0:
            stderr_text = stderr.decode("utf-8", errors="replace").strip()
            raise ProcessExitError(returncode, stderr_text)

        response = extract_json(text)
        output = validate_output(_structured_output(response, text), opts.output_schema)
        return Result(output=output, text=text, usage=_usage_from(response))

    async def close(self) -> None:
        """The per-call adapter owns no resources between calls."""


def _build_args(opts: RunOpts[OutputT]) -> list[str]:
    """Translate ``RunOpts`` into the ``claude -p`` argv for this call.

    Pure mapping, no I/O -- kept separate from ``ClaudeCLI.run`` so the flag-mapping
    policy (which RunOpts fields become which CLI flags) is testable and readable apart
    from process spawning/teardown.
    """

    schema_json = json.dumps(opts.output_schema.model_json_schema(), separators=(",", ":"))
    args = [
        str(opts.executable),
        "-p",
        "--output-format",
        "json",
        "--json-schema",
        schema_json,
        "--model",
        opts.model,
    ]
    if opts.system_prompt is not None:
        args += ["--system-prompt", opts.system_prompt]
    if opts.append_system_prompt is not None:
        args += ["--append-system-prompt", opts.append_system_prompt]
    if opts.tools_allowlist:
        # A scoped allowlist without an explicit mode still needs a
        # permission mode for --allowedTools to take effect.
        args += ["--allowedTools", *opts.tools_allowlist]
        args += ["--permission-mode", opts.permission_mode or "auto"]
    elif opts.permission_mode is not None:
        args += ["--permission-mode", opts.permission_mode]
    else:
        # Mirrors no-mistakes' claudeAgent.buildArgs: default to skipping
        # permission checks entirely unless the caller pinned a mode.
        args.append("--dangerously-skip-permissions")
    return args


def _structured_output(response: JsonValue, text: str) -> JsonValue:
    """Bridge Claude's JSON envelope to the backend-agnostic output schema."""

    if not isinstance(response, dict) or "structured_output" not in response:
        raise NoStructuredOutputError(text)
    return response["structured_output"]


def _usage_from(response: JsonValue) -> Usage | None:
    if not isinstance(response, dict):
        return None

    raw_usage = response.get("usage")
    usage = raw_usage if isinstance(raw_usage, dict) else {}
    parsed = Usage(
        input_tokens=_optional_int(usage.get("input_tokens")),
        output_tokens=_optional_int(usage.get("output_tokens")),
        total_cost_usd=_optional_float(response.get("total_cost_usd")),
    )
    if (
        parsed.input_tokens is None
        and parsed.output_tokens is None
        and parsed.total_cost_usd is None
    ):
        return None
    return parsed


def _optional_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _optional_float(value: object) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None
