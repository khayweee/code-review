#!/usr/bin/env python3
"""Fake Claude CLI proving `ReviewStep`'s prompt assembly (issue #27): echoes back, inside
a valid `ReviewOutput` answer's `risk_rationale`, whether the prompt it received contains
the intent-conformance clause's distinctive opening sentence -- without itself needing to
know the full clause text. This is `review_findings.py`'s
`"saw_added_line": "+world" in prompt` pattern, adapted to a schema (`ReviewOutput`) that
has no room for an extra boolean field of its own.

Also used by `ReviewStep` tests that attach a real `ActivityReporter` (issue #65/#66's
activity-span/tool-streaming tests) -- those calls carry `on_stream_event`, which switches
`ClaudeCLI` to `--output-format stream-json`, so this fake emits its structured answer as
a single stream-json `"result"`-type line in that mode instead of a bare JSON object.
"""

from __future__ import annotations

import json
import sys

prompt = sys.stdin.read()
saw_clause = "Intent conformance is mandatory" in prompt

structured_output = {
    "findings": [],
    "risk_level": "low",
    "risk_rationale": (
        "saw intent-conformance clause" if saw_clause else "did not see intent-conformance clause"
    ),
}

if (
    "--output-format" in sys.argv
    and sys.argv[sys.argv.index("--output-format") + 1] == "stream-json"
):
    print(
        json.dumps(
            {
                "type": "result",
                "structured_output": structured_output,
            }
        )
    )
else:
    print(json.dumps({"structured_output": structured_output}))
