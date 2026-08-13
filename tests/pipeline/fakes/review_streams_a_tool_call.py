#!/usr/bin/env python3
"""Fake Claude CLI proving `ReviewStep`'s tool-call activity relay (`_tool_stream_relay`
in `steps/review.py`): emits one `tool_use`/`tool_result` pair over stream-json before
its final clean `ReviewOutput` answer, so a test with a real `ActivityReporter` attached
can assert a nested `Tool: Read(/fake/path.txt)` activity span appears alongside the
outer "Agent: reviewing diff via claude" one.

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
