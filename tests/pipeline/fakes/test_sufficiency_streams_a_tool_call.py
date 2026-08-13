#!/usr/bin/env python3
"""Fake Claude CLI proving `TestSufficiencyStep`'s tool-call activity relay (the shared
`tool_stream_relay` in `steps/tool_activity.py`): emits one `tool_use`/`tool_result` pair
over stream-json before its final clean `TestSufficiencyOutput` answer, so a test with a
real `ActivityReporter` attached can assert a `Tool: Read(/fake/path.txt)` activity appears
alongside the outer "Agent: assessing test sufficiency via claude" one.

A standalone copy of `tests/pipeline/fakes/review_streams_a_tool_call.py`'s shape, adapted
to `TestSufficiencyOutput`'s schema -- deliberately not shared, mirroring this repo's own
convention of separate fake CLI scripts per fixture (see `tests/steps/test_test_sufficiency.
py`'s module docstring).

Only reachable in stream-json mode -- `TestSufficiencyStep` only sets `on_stream_event` when
a `StepContext.activity_reporter` is attached, which is exactly when this fixture's tool
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
                "tested": ["greeting message includes the new line"],
                "testing_summary": "Existing test suite already covers the changed behavior.",
                "artifacts": [
                    {
                        "kind": "existing-test",
                        "description": "test_greeting_includes_world exercises the new line",
                        "location": "tests/test_greeting.py:12",
                    }
                ],
            },
        }
    )
)
