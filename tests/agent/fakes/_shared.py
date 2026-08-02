"""Shared response-building helpers for fake Claude CLI scripts."""

from __future__ import annotations

import json
import os
import select
import sys


def read_prompt() -> str:
    """Read the parent's full prompt from stdin, working for either call path.

    The default `--dangerously-skip-permissions` path uses `process.communicate()`,
    which sends EOF right after writing the prompt, so a blocking `sys.stdin.read()`
    returns once it has all arrived. The stdin-relay path (issue #41,
    `claude_cli._run_with_stdin_relay`) deliberately keeps stdin open for the rest of the
    call -- a fake exercising that path must not block waiting for an EOF that will never
    come, so this instead drains whatever is immediately available (see
    `drain_available_stdin`). `sys.argv` already carries
    `--dangerously-skip-permissions` when present, so that's the single source of truth
    for which path a given invocation is on -- matching how `claude_cli._build_args`
    itself decides.
    """
    if "--dangerously-skip-permissions" in sys.argv:
        return sys.stdin.read()
    return drain_available_stdin()


def drain_available_stdin(idle_seconds: float = 0.05) -> str:
    """Read and return whatever is immediately available on stdin, without an EOF.

    Polls with a short `select` timeout and stops once nothing new has arrived for
    `idle_seconds` -- fine for the small, single-write payloads these fakes are given.
    Used by fakes exercising the stdin-relay path (issue #41), where the parent never
    sends EOF, so plain `sys.stdin.read()`/`readline()` would hang forever.
    """
    chunks: list[str] = []
    while select.select([sys.stdin], [], [], idle_seconds)[0]:
        chunk = os.read(sys.stdin.fileno(), 65536)
        if not chunk:
            break
        chunks.append(chunk.decode("utf-8"))
    return "".join(chunks)


def option_value(name: str) -> str:
    index = sys.argv.index(name)
    return sys.argv[index + 1]


def optional_option_value(name: str) -> str | None:
    return option_value(name) if name in sys.argv else None


def variadic_option_values(name: str) -> list[str]:
    if name not in sys.argv:
        return []
    values = []
    for arg in sys.argv[sys.argv.index(name) + 1 :]:
        if arg.startswith("--"):
            break
        values.append(arg)
    return values


def build_response(prompt: str) -> dict[str, object]:
    schema = json.loads(option_value("--json-schema"))
    response: dict[str, object] = {
        "structured_output": {
            "answer": f"received: {prompt}",
            "cwd": os.getcwd(),
            "schema_title": schema["title"],
            "pid": os.getpid(),
            "process_group": os.getpgrp(),
            "model": option_value("--model"),
            "system_prompt": optional_option_value("--system-prompt"),
            "append_system_prompt": optional_option_value("--append-system-prompt"),
            "tools_allowlist": variadic_option_values("--allowedTools"),
            "permission_mode": optional_option_value("--permission-mode"),
            "dangerously_skip_permissions": "--dangerously-skip-permissions" in sys.argv,
        }
    }
    if prompt == "zero usage":
        response["usage"] = {"input_tokens": 0, "output_tokens": 0}
        response["total_cost_usd"] = 0.0
    elif prompt != "omit usage":
        response["usage"] = {"input_tokens": 12, "output_tokens": 4}
        response["total_cost_usd"] = 0.25
    return response


def print_json(response: dict[str, object]) -> None:
    print(json.dumps(response))
