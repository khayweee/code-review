"""Shared response-building helpers for fake Claude CLI scripts."""

from __future__ import annotations

import json
import os
import sys


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
    if prompt != "omit usage":
        response["usage"] = {"input_tokens": 12, "output_tokens": 4}
        response["total_cost_usd"] = 0.25
    return response


def print_json(response: dict[str, object]) -> None:
    print(json.dumps(response))
