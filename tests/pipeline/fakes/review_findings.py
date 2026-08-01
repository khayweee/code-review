#!/usr/bin/env python3
"""Fake Claude CLI for the Step/StepContext/StepOutcome round-trip test.

`tests/agent/fakes/valid_output.py` proves Milestone 1's subprocess mechanics (argv, cwd,
process group); it isn't reused here because its response shape is tied to that test's
`Answer` schema. This script proves a different thing: that the real diff text a `Step`
embeds in its prompt actually reaches the agent call over stdin, by echoing back whether
it saw the diff's added line.
"""

from __future__ import annotations

import json
import sys

prompt = sys.stdin.read()

response = {
    "structured_output": {
        "summary": f"reviewed a {len(prompt)}-character prompt",
        "saw_added_line": "+world" in prompt,
    }
}
print(json.dumps(response))
