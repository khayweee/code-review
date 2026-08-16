#!/usr/bin/env python3
"""Fake Claude CLI whose stream-json `tool_result` line is far larger than asyncio's
64 KiB `StreamReader` limit -- the shape a real `Read`/`Edit` of a big file produces, and
the one that made `StreamReader.readline()` raise "Separator is found, but chunk is
longer than limit" mid-review.

The payload is random-ish rather than a single repeated byte so a reassembly bug that
drops or reorders a chunk can't pass by accident.
"""

from __future__ import annotations

import hashlib
import json

from _shared import build_response, read_prompt

# Several times asyncio's 2**16 limit, so the line spans many bounded reads.
_PAYLOAD = hashlib.sha256(b"large tool result").hexdigest() * 8000

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
                        "input": {"file_path": "/tmp/big.txt"},
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
                        "content": _PAYLOAD,
                        "is_error": False,
                    }
                ]
            },
        }
    )
)
print(json.dumps({"type": "result", "session_id": "sess_fake", **response}))
