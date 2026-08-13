#!/usr/bin/env python3
"""Fake Claude CLI for the full-pipeline CLI test (`tests/test_cli_review.py`, issue #60):
a blocking, "ask-user" answer, shaped as the same `ReviewOutput`/`TestSufficiencyOutput`
superset `claude_superset_clean.py` uses (see that script's docstring for why one payload
covers both schemas). Proves acceptance criterion 3 -- a blocking finding is surfaced to
the user without the CLI run crashing -- for a real end-to-end run: since both
`ReviewStep`'s and `TestSufficiencyStep`'s `ClaudeCLI` calls resolve to this same fake
`claude` on `PATH`, both steps report `needs_approval=True` from this one script, exactly
as either one alone would.
"""

from __future__ import annotations

import json
import sys

sys.stdin.read()  # drain the prompt; this fixture's answer doesn't depend on its contents

# `_env_with_fake_claude` copies this script's *text* into a standalone file named
# "claude" with no sibling modules alongside it, so this can't import a shared helper --
# each fake inlines its own stream-json "result" line (see `cli.py`'s always-on
# `ActivityRelay`, which forces `ClaudeCLI` into `--output-format stream-json` here).
print(
    json.dumps(
        {
            "type": "result",
            "structured_output": {
                "findings": [
                    {
                        "severity": "error",
                        "description": "drops error handling required by the caller's contract",
                        "action": "ask-user",
                        "review_scope": "source",
                    }
                ],
                "risk_level": "high",
                "risk_rationale": "drops error handling on a path the caller depends on",
                "tested": [],
                "testing_summary": "not assessed -- blocked on the finding above",
                "artifacts": [],
            },
        }
    )
)
