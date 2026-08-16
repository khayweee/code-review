#!/usr/bin/env python3
"""Fake Claude CLI returning a real `TestSufficiencyOutput`-shaped answer (issue #59): only
"info"/"no-op" findings, alongside a full `tested`/`testing_summary`/`artifacts` payload --
proving `TestSufficiencyStep` reports `needs_approval=False` when nothing resolves to
"ask-user".

Also reports `usage`/`total_cost_usd` (see `claude_cli.py`'s `_usage_from`), proving
`TestSufficiencyStep.run` threads `Result.usage` onto its returned `StepOutcome.usage`.
"""

from __future__ import annotations

import json
import sys

sys.stdin.read()  # drain the prompt; this fixture's answer doesn't depend on its contents

response = {
    "structured_output": {
        "findings": [
            {
                "severity": "info",
                "description": "test naming could be more descriptive",
                "action": "no-op",
                "review_scope": "source",
            }
        ],
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
    "usage": {"input_tokens": 900, "output_tokens": 210},
    "total_cost_usd": 0.0198,
}
print(json.dumps(response))
