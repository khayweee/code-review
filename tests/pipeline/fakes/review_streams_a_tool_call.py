#!/usr/bin/env python3
"""Fake Claude CLI proving `ReviewStep`'s tool-call activity relay (`tool_stream_relay`
in `steps/tool_activity.py`): emits one `tool_use`/`tool_result` pair over stream-json,
with a real sleep in between, before its final clean `ReviewOutput` answer, so a test with
a real `ActivityReporter` attached can assert a nested `Tool: Read(/fake/path.txt)`
activity span -- opened on `tool_use`, closed on the matching `tool_result` -- appears
alongside the outer "Agent: reviewing diff via claude" one, with a real, non-trivial
duration (not the ~0s a one-shot log would report).

Only reachable in stream-json mode -- `ReviewStep` only sets `on_stream_event` when a
`StepContext.activity_reporter` is attached, which is exactly when this fixture's tool
events matter.
"""

from __future__ import annotations

import json
import sys
import time

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
sys.stdout.flush()

time.sleep(0.05)  # real, measurable elapsed time between tool_use and its tool_result

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
