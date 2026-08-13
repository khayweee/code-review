#!/usr/bin/env python3
"""Fake Claude CLI emitting a real stream-json NDJSON transcript: one `tool_use` block,
its matching `tool_result`, then the final `"result"`-type line -- proving
`claude_cli._run_streaming`/`_parse_stream_line` against the real line shapes the CLI
sends in `--output-format stream-json --verbose` mode, not a stand-in.

Only reachable when the caller attaches `on_stream_event` (`claude_cli._build_args` then
sets `--output-format stream-json`); this fake doesn't handle the legacy `--output-format
json` path at all, since nothing in `tests/agent/test_streaming.py` calls it that way.
"""

from __future__ import annotations

import json

from _shared import build_response, read_prompt

prompt = read_prompt()
response = build_response(prompt)

print(
    json.dumps(
        {
            "type": "assistant",
            "session_id": "sess_fake",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "tool_1",
                        "name": "Read",
                        "input": {"file_path": "/tmp/fake.txt"},
                    }
                ]
            },
        }
    )
)
print(
    json.dumps(
        {
            "type": "user",
            "session_id": "sess_fake",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tool_1",
                        "content": "fake file contents",
                        "is_error": False,
                    }
                ]
            },
        }
    )
)
print(json.dumps({"type": "result", "session_id": "sess_fake", **response}))
