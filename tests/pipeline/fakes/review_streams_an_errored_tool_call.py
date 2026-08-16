#!/usr/bin/env python3
"""Fake Claude CLI proving `ReviewStep`'s tool-call activity relay (the shared
`tool_stream_relay` in `steps/tool_activity.py`) finishes the tool-call span itself with
`error` set for an errored `tool_result` (mirroring `ActivityHandle.fail(detail)`'s
convention elsewhere), rather than logging a second, distinct activity.

Only reachable in stream-json mode -- `ReviewStep` only sets `on_stream_event` when a
`StepContext.activity_reporter` is attached, which is exactly when this fixture's tool
events matter.
"""

from __future__ import annotations

import json
import sys

sys.stdin.read()  # drain the prompt; this fixture's answer doesn't depend on its contents

print(
    json.dumps(
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "tool_1",
                        "name": "Read",
                        "input": {"file_path": "/fake/missing.txt"},
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
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tool_1",
                        "content": "file not found",
                        "is_error": True,
                    }
                ]
            },
        }
    )
)
print(
    json.dumps(
        {
            "type": "result",
            "structured_output": {
                "findings": [],
                "risk_level": "low",
                "risk_rationale": "clean",
            },
        }
    )
)
