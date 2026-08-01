"""Subprocess adapter for non-interactive Claude CLI calls."""

from __future__ import annotations

import asyncio
import json

from code_review.agent.base import OutputT, Result, RunOpts, Usage
from code_review.agent.schema import JsonValue, extract_json, validate_output


class ClaudeCLI:
    """Run each Agent call in a fresh Claude CLI process."""

    async def run(self, opts: RunOpts[OutputT]) -> Result[OutputT]:
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
        process = await asyncio.create_subprocess_exec(
            *args,
            cwd=opts.cwd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        stdout, stderr = await process.communicate(opts.prompt.encode("utf-8"))
        text = stdout.decode("utf-8")

        if process.returncode != 0:
            error_text = stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"Claude CLI exited with status {process.returncode}: {error_text}")

        response = extract_json(text)
        output = validate_output(_structured_output(response), opts.output_schema)
        return Result(output=output, text=text, usage=_usage_from(response))

    async def close(self) -> None:
        """The per-call adapter owns no resources between calls."""


def _structured_output(response: JsonValue) -> JsonValue:
    """Bridge Claude's JSON envelope to the backend-agnostic output schema."""

    if not isinstance(response, dict) or "structured_output" not in response:
        raise ValueError("Claude CLI response did not contain structured_output")
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
