"""Subprocess adapter for non-interactive Claude CLI calls.

Runs each Agent call in a fresh ``claude -p`` process: sends the prompt over stdin,
reads a structured JSON envelope from stdout, and builds CLI args from ``RunOpts``.
Spawns the child in its own process group and hands teardown to
``process_group.terminate_process_group`` so no descendant survives on any exit path.

Two call paths depending on whether permissions are skipped: the default
``--dangerously-skip-permissions`` path uses ``process.communicate()`` since that
subprocess never blocks on stdin. A call that opts out (non-empty ``tools_allowlist``
or a pinned ``permission_mode``) goes through ``_run_with_stdin_relay`` instead, which
can detect a stdin stall and relay it via ``RunOpts.on_input_needed``. That relay path
is only exercised against the fake CLI in `tests/agent/fakes/blocks_on_stdin.py` and has
not been validated against the real `claude` CLI's stdin-blocking behavior.
"""

from __future__ import annotations

import asyncio
import json

from code_review.agent.base import OutputT, Result, RunOpts, Usage
from code_review.agent.errors import (
    NoStructuredOutputError,
    ProcessExitError,
    ProcessStartError,
    StdinBlockedError,
)
from code_review.agent.process_group import terminate_process_group
from code_review.agent.schema import JsonValue, extract_json, validate_output

# Idle time on stdout before the subprocess is treated as blocked waiting on stdin.
# Not a public RunOpts field; tests shrink it via monkeypatch.
_STDIN_IDLE_TIMEOUT_SECONDS = 30.0


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
            if "--dangerously-skip-permissions" in args:
                stdout, stderr = await process.communicate(opts.prompt.encode("utf-8"))
            else:
                stdout, stderr = await _run_with_stdin_relay(process, opts)
        finally:
            # Runs on every exit path so no descendant subprocess outlives this call.
            await terminate_process_group(process)
        text = stdout.decode("utf-8")

        returncode = process.returncode
        # Both paths above only return once the process has exited.
        assert returncode is not None
        if returncode != 0:
            stderr_text = stderr.decode("utf-8", errors="replace").strip()
            raise ProcessExitError(returncode, stderr_text)

        response = extract_json(text)
        output = validate_output(_structured_output(response, text), opts.output_schema)
        return Result(output=output, text=text, usage=_usage_from(response))

    async def close(self) -> None:
        """The per-call adapter owns no resources between calls."""


async def _run_with_stdin_relay(
    process: asyncio.subprocess.Process, opts: RunOpts[OutputT]
) -> tuple[bytes, bytes]:
    """Read/write loop for calls that did not opt into ``--dangerously-skip-permissions``.

    Stdin is left open for the whole call (not closed after the initial prompt) since
    this subprocess may block waiting for a permission answer, and once a pipe's write
    end is closed it can't be reopened. Stderr is pumped into a buffer by a background
    task so a chatty child can't deadlock the parent while it blocks reading stdout.

    Stdout is read in bounded chunks with a per-read timeout; a timeout means the
    subprocess looks blocked waiting on stdin, so the bytes accumulated since the last
    stall are treated as prompt text and relayed to ``opts.on_input_needed``, whose
    answer is written back before the loop resumes. With no callback, raises
    ``StdinBlockedError`` instead of hanging or fabricating an answer.
    """

    assert process.stdin is not None
    assert process.stdout is not None
    assert process.stderr is not None

    process.stdin.write(opts.prompt.encode("utf-8"))
    await process.stdin.drain()

    stderr_task: asyncio.Task[bytes] = asyncio.ensure_future(process.stderr.read())

    stdout_chunks: list[bytes] = []
    # Offset marking where the last prompt handoff left off; a second stall only
    # reports what's new since then.
    handoff_offset = 0
    while True:
        try:
            chunk = await asyncio.wait_for(
                process.stdout.read(4096), timeout=_STDIN_IDLE_TIMEOUT_SECONDS
            )
        except TimeoutError:
            accumulated = b"".join(stdout_chunks)
            if opts.on_input_needed is None:
                raise StdinBlockedError(accumulated.decode("utf-8", errors="replace")) from None

            prompt_text = accumulated[handoff_offset:].decode("utf-8", errors="replace")
            answer = await opts.on_input_needed(prompt_text)
            process.stdin.write((answer + "\n").encode("utf-8"))
            await process.stdin.drain()
            handoff_offset = len(accumulated)
            continue

        if chunk == b"":
            break
        stdout_chunks.append(chunk)

    # EOF: child is finishing normally, safe to close stdin and wait it out.
    process.stdin.close()
    stderr = await stderr_task
    await process.wait()
    return b"".join(stdout_chunks), stderr


def _build_args(opts: RunOpts[OutputT]) -> list[str]:
    """Translate ``RunOpts`` into the ``claude -p`` argv for this call."""

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
        # --allowedTools needs a permission mode to take effect.
        args += ["--allowedTools", *opts.tools_allowlist]
        args += ["--permission-mode", opts.permission_mode or "auto"]
    elif opts.permission_mode is not None:
        args += ["--permission-mode", opts.permission_mode]
    else:
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
