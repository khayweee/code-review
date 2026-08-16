#!/usr/bin/env python3
"""Fake Claude CLI returning a real `ReviewOutput`-shaped answer (issue #27): one
"pipeline-owned-delivery"-scoped finding whose action is "ask-user" (proving
`ReviewStep` strips it via `filter_pipeline_owned_delivery_findings` before the
blocking-findings gate ever sees it) alongside two "source"-scoped findings whose actions
are "no-op" and "auto-fix" -- the only actions that survive filtering, so the resulting
`StepOutcome` is expected to have `needs_approval=False` and `auto_fixable=True`.

`risk_level` starts at "low" so the scope filter's risk-reset branch never triggers here;
that reset path already has its own regression coverage in `tests/steps/test_review.py`'s
Milestone 5/issue #26 tests.

Also reports `usage`/`total_cost_usd` (see `claude_cli.py`'s `_usage_from`), proving
`ReviewStep.run` threads `Result.usage` onto its returned `StepOutcome.usage`.
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
                "description": "variable name could be clearer",
                "action": "no-op",
                "review_scope": "source",
            },
            {
                "severity": "warning",
                "description": "consider extracting a helper function",
                "action": "auto-fix",
                "review_scope": "source",
            },
            {
                "severity": "warning",
                "description": "generated lockfile diff looks stale",
                "action": "ask-user",
                "review_scope": "pipeline-owned-delivery",
            },
        ],
        "risk_level": "low",
        "risk_rationale": "only style-level findings on author-written code",
    },
    "usage": {"input_tokens": 1200, "output_tokens": 340},
    "total_cost_usd": 0.0421,
}
print(json.dumps(response))
