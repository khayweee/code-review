#!/usr/bin/env python3
"""Fake Claude CLI proving `ReviewStep`'s prompt assembly (issue #27): echoes back, inside
a valid `ReviewOutput` answer's `risk_rationale`, whether the prompt it received contains
the intent-conformance clause's distinctive opening sentence -- without itself needing to
know the full clause text. This is `review_findings.py`'s
`"saw_added_line": "+world" in prompt` pattern, adapted to a schema (`ReviewOutput`) that
has no room for an extra boolean field of its own.
"""

from __future__ import annotations

import json
import sys

prompt = sys.stdin.read()
saw_clause = "Intent conformance is mandatory" in prompt

response = {
    "structured_output": {
        "findings": [],
        "risk_level": "low",
        "risk_rationale": (
            "saw intent-conformance clause"
            if saw_clause
            else "did not see intent-conformance clause"
        ),
    }
}
print(json.dumps(response))
