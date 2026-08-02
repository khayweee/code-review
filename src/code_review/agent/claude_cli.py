"""Subprocess adapter for non-interactive Claude CLI calls.

- Runs each Agent call in a fresh, non-interactive ``claude -p`` process.
- Sends the prompt over stdin and reads a structured JSON envelope back from stdout.
- Builds CLI args from ``RunOpts``: model, system prompt (replace or append), schema, and
  a tools allowlist or the default permission-skipping mode.
- Spawns the child in its own process group (session leader) so its PID doubles as the
  whole group's PGID, then hands teardown to ``process_group.terminate_process_group``
  (shared, backend-agnostic) so no descendant survives a call on any exit path.
- Raises one of five distinct errors (``ProcessStartError``, ``ProcessExitError``,
  ``NoStructuredOutputError``, ``OutputValidationError``, ``StdinBlockedError``) so
  callers can tell which stage failed.
- Two call paths, chosen by whether ``_build_args`` decided to skip permissions (see
  https://github.com/khayweee/code-review/issues/41): the default
  ``--dangerously-skip-permissions`` path (every call site today) uses
  ``process.communicate()`` unchanged, since that subprocess never blocks waiting on
  stdin. A call that opted out of that default (a non-empty ``tools_allowlist`` or a
  pinned ``permission_mode``) instead goes through ``_run_with_stdin_relay``, a hand-rolled
  read/write loop that can detect a stdin stall and relay it through
  ``RunOpts.on_input_needed`` -- see that function's docstring and `agent/AGENTS.md` for
  the full design. This second path is exercised only against the fake CLI test double in
  `tests/agent/fakes/blocks_on_stdin.py`; its prompt-detection framing has not been
  validated against the real `claude` CLI's actual stdin-blocking behavior.
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

# How long a read of the subprocess's stdout may go without producing a byte before it's
# treated as "the subprocess appears blocked waiting on stdin". Deliberately a
# module-level constant, not a public `RunOpts` parameter -- only tests need to shrink it,
# via monkeypatch (see `tests/agent/test_claude_cli.py`), matching `process_group.py`'s
# own timing constants.
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
            # Runs on every exit path - success, non-zero exit, a parse/validation
            # failure raised further down, a stdin stall with no relay available, and
            # cancellation of this coroutine - so no descendant the subprocess started is
            # still running afterward. The process group persists as long as any member is
            # alive, regardless of whether the direct child (the group leader) has already
            # exited.
            await terminate_process_group(process)
        text = stdout.decode("utf-8")

        returncode = process.returncode
        # Both paths above only return once the process has exited: `communicate()`
        # always waits for it, and `_run_with_stdin_relay` explicitly awaits
        # `process.wait()` on its EOF path.
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

    Unlike the ``process.communicate()`` fast path, this subprocess may legitimately block
    waiting for an answer on stdin (e.g. a permission prompt), so stdin is written and left
    open for the whole call rather than closed after the initial prompt -- once the parent
    closes its write end of a pipe there is no way to write to it again at the OS level,
    and ``on_input_needed`` may need to answer a prompt discovered later in the call.

    Stderr is pumped into a buffer by a background task for the same reason
    ``communicate()`` does this internally: a chatty child filling its stderr pipe must not
    deadlock a parent that's blocked reading stdout in the foreground.

    Stdout is read in bounded chunks with a per-read timeout. A timeout means the
    subprocess has gone quiet on stdout long enough that it looks blocked waiting on
    stdin: the bytes accumulated since the last such stall (if any) are treated as the
    prompt text. With ``opts.on_input_needed`` supplied, that text is relayed to it and the
    returned answer is written back (plus a newline) before the loop resumes -- the
    timeout simply renews. With no callback supplied, this raises ``StdinBlockedError``
    instead of hanging forever or fabricating an answer; the caller's ``finally`` block
    (`ClaudeCLI.run`) tears the process down, so no teardown happens here.
    """

    assert process.stdin is not None
    assert process.stdout is not None
    assert process.stderr is not None

    process.stdin.write(opts.prompt.encode("utf-8"))
    await process.stdin.drain()

    stderr_task: asyncio.Task[bytes] = asyncio.ensure_future(process.stderr.read())

    stdout_chunks: list[bytes] = []
    # Index into the concatenated stdout-so-far marking where the last prompt handoff
    # left off, so a second stall only reports what's new since then.
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

    # EOF: the child is finishing normally. Safe to close stdin now -- no more writes are
    # coming -- then wait for the stderr pump and the process itself, exactly like
    # `communicate()` would.
    process.stdin.close()
    stderr = await stderr_task
    await process.wait()
    return b"".join(stdout_chunks), stderr


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
