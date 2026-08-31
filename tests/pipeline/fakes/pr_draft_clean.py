#!/usr/bin/env python3
"""Fake Claude CLI returning a well-formed `PRDraft`-shaped answer: a conventional-commit
title and clean "What Changed" bullets that need no sanitizing.

Also reports `usage`/`total_cost_usd` (see `claude_cli.py`'s `_usage_from`), proving
`PRStep.run` threads `Result.usage` onto its returned `StepOutcome.usage`.
"""

from __future__ import annotations

import json
import sys

sys.stdin.read()  # drain the prompt; this fixture's answer doesn't depend on its contents

response = {
    "structured_output": {
        "title": "feat(agent): retry transient backend failures with exponential backoff",
        "what_changed": [
            "Transient backend failures are now retried instead of surfacing immediately.",
            "Backoff grows exponentially between attempts, with a bounded attempt count.",
            "A permanently failing call still raises the original error once retries run out.",
        ],
    },
    "usage": {"input_tokens": 900, "output_tokens": 120},
    "total_cost_usd": 0.0175,
}
print(json.dumps(response))
