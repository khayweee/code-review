#!/usr/bin/env python3
"""Fake Claude CLI emitting a real stream-json NDJSON transcript for two *parallel* tool
calls in one turn: one `"assistant"` line with two `tool_use` blocks, then one `"user"`
line with both matching `tool_result` blocks (the shape the real Claude CLI bundles
parallel tool-call results into), then the final `"result"`-type line.

Proves issue #132's fix end to end: both `tool_use` blocks in a single line, and both
`tool_result` blocks in a single subsequent line, each surface as their own `StreamEvent`
instead of only the first block per line.

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
                    },
                    {
                        "type": "tool_use",
                        "id": "tool_2",
                        "name": "Grep",
                        "input": {"pattern": "foo"},
                    },
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
                    },
                    {
                        "type": "tool_result",
                        "tool_use_id": "tool_2",
                        "content": "fake grep matches",
                        "is_error": False,
                    },
                ]
            },
        }
    )
)
print(json.dumps({"type": "result", "session_id": "sess_fake", **response}))
