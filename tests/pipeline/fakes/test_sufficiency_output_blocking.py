#!/usr/bin/env python3
"""Fake Claude CLI returning a real `TestSufficiencyOutput`-shaped answer with a blocking
"ask-user" finding (issue #59) -- proving `TestSufficiencyStep` reports
`needs_approval=True` (and `auto_fixable=False`) whenever a finding resolves to
"ask-user", mirroring `review_output_blocking.py`'s equivalent case for `ReviewStep`.
"""

from __future__ import annotations

import json
import sys

sys.stdin.read()  # drain the prompt; this fixture's answer doesn't depend on its contents

response = {
    "structured_output": {
        "findings": [
            {
                "severity": "error",
                "description": "changed retry behavior has no test and no manual "
                "verification was performed",
                "action": "ask-user",
                "review_scope": "source",
            }
        ],
        "tested": [],
        "testing_summary": "The change is unverified; no test or manual check was done.",
        "artifacts": [],
    }
}
print(json.dumps(response))
