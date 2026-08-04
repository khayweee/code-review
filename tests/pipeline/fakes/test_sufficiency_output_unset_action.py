#!/usr/bin/env python3
"""Fake Claude CLI returning a `TestSufficiencyOutput`-shaped answer with a finding whose
`action` is left unset (issue #82's fail-safe-default regression) -- mirrors
`test_sufficiency_output_blocking.py`'s shape but omits `action` entirely instead of
setting it to `"ask-user"` explicitly, so this proves `pipeline/findings.py`'s
`action_or_default` fail-safe default (unset resolves to "ask-user", never "auto-fix")
holds for `TestSufficiencyStep` through the full `run_steps`/`pipeline/executor.py` loop,
not just the pure-function tests in `tests/pipeline/test_findings.py`.
"""

from __future__ import annotations

import json
import sys

sys.stdin.read()  # drain the prompt; this fixture's answer doesn't depend on its contents

response = {
    "structured_output": {
        "findings": [
            {
                "severity": "warning",
                "description": "no action set for this finding",
                "review_scope": "source",
            }
        ],
        "tested": [],
        "testing_summary": "The change's verification status is unclear.",
        "artifacts": [],
    }
}
print(json.dumps(response))
