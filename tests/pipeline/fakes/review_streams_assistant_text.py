#!/usr/bin/env python3
"""Fake Claude CLI proving `tool_stream_relay`'s (`steps/tool_activity.py`) `ASSISTANT_TEXT`
handling: emits one `text` content block before its `tool_use`/`tool_result` pair and its
final clean `ReviewOutput` answer, so a test with a real `ActivityReporter` attached can
assert a nested `Agent: Checking the auth module for a missing nil check` activity appears
alongside the tool-call one.

Only reachable in stream-json mode -- `ReviewStep` only sets `on_stream_event` when a
`StepContext.activity_reporter` is attached, which is exactly when this fixture's stream
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
                        "type": "text",
                        "text": "Checking the auth module for a missing nil check",
                    }
                ]
            },
        }
    )
)
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
                        "input": {"file_path": "/fake/path.txt"},
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
                        "content": "fake contents",
                        "is_error": False,
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
