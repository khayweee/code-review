#!/usr/bin/env python3
"""Fake Claude CLI for a real-pty repro of a MULTI-finding blocking park (issue #98):
like `claude_superset_blocking.py`, but returns TWO "ask-user" findings instead of one, so
`ReviewStep`/`TestSufficiencyStep` each park with a multi-row `FindingsList` rather than
#98's degenerate single-row case."""

from __future__ import annotations

import json
import sys

sys.stdin.read()  # drain the prompt; this fixture's answer doesn't depend on its contents

response = {
    "structured_output": {
        "findings": [
            {
                "severity": "error",
                "description": "drops error handling required by the caller's contract",
                "action": "ask-user",
                "review_scope": "source",
            },
            {
                "severity": "warning",
                "description": "unclear naming for the new helper function",
                "action": "ask-user",
                "review_scope": "source",
            },
        ],
        "risk_level": "high",
        "risk_rationale": "drops error handling on a path the caller depends on",
        "tested": [],
        "testing_summary": "not assessed -- blocked on the findings above",
        "artifacts": [],
    }
}
print(json.dumps(response))
