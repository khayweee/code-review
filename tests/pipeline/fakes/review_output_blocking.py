#!/usr/bin/env python3
"""Fake Claude CLI returning a real `ReviewOutput`-shaped answer with a blocking
"ask-user" finding (issue #27), alongside an "auto-fix" finding at lower severity --
proving `ReviewStep`'s `auto_fixable` stays `False` whenever any surviving finding
resolves to "ask-user", even though another finding resolves to "auto-fix". Both
findings are "source"-scoped, so the deterministic pipeline-owned-delivery scope filter
is a no-op here; `review_output_clean.py` covers the filter actually stripping something.
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
                "description": "removes error handling required by the caller's contract",
                "action": "ask-user",
                "review_scope": "source",
            },
            {
                "severity": "info",
                "description": "minor formatting nit",
                "action": "auto-fix",
                "review_scope": "source",
            },
        ],
        "risk_level": "high",
        "risk_rationale": "drops error handling on a path the caller depends on",
    }
}
print(json.dumps(response))
