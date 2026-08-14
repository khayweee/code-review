#!/usr/bin/env python3
"""Fake Claude CLI proving issue #81's automatic fix round end to end, through a real
`code-review review` run (`tests/test_cli_review.py`): the FIRST call this fake `claude`
ever receives -- `ReviewStep`'s own initial (non-fix) round -- answers with one "auto-fix"
finding and no "ask-user" finding (`ReviewStep.run`'s `auto_fixable=True`, `needs_approval=
False`), which `pipeline/executor.py`'s round loop re-runs automatically, no park, no human
keypress. Every call after the first -- `ReviewStep`'s own automatic fix round, and
`TestSufficiencyStep`'s later, separate call, both resolving this same fake `claude` on
`PATH` (see `tests/test_cli_review.py`'s `_env_with_fake_claude` docstring: `cli.py` builds
both steps via `cls()` with no executable override) -- answers clean.

Shaped as the same `ReviewOutput`/`TestSufficiencyOutput` superset `claude_superset_clean.py`
already uses (see that script's own docstring for why one payload covers both schemas via
pydantic's default `extra="ignore"`).

Call count is tracked via an on-disk marker file, not an in-process counter -- each
invocation is a fresh subprocess (`RunOpts.executable` shells out per call), so nothing
survives in memory between calls. The marker lives in this script's own `cwd`
(`RunOpts.cwd`, the repo under review), matching `tests/pipeline/fakes/order_step_a.py`'s
own on-disk-marker pattern for the same reason.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.stdin.read()  # drain the prompt; this fixture branches on call count, not content

marker = Path("fake-claude-call-count")
call_index = int(marker.read_text()) if marker.exists() else 0
marker.write_text(str(call_index + 1))

# `_env_with_fake_claude` copies this script's *text* into a standalone file named
# "claude" with no sibling modules alongside it, so this can't import a shared helper --
# each fake inlines its own stream-json "result" line (see `cli.py`'s always-on
# `ActivityRelay`, which forces `ClaudeCLI` into `--output-format stream-json` here).
if call_index == 0:
    structured_output = {
        "findings": [
            {
                "severity": "warning",
                "description": "extract a helper function",
                "action": "auto-fix",
                "review_scope": "source",
            }
        ],
        "risk_level": "medium",
        "risk_rationale": "initial pass: one auto-fixable style finding",
        "tested": [],
        "testing_summary": "not assessed yet",
        "artifacts": [],
    }
else:
    structured_output = {
        "findings": [],
        "risk_level": "low",
        "risk_rationale": "clean",
        "tested": [],
        "testing_summary": "clean",
        "artifacts": [],
    }
print(json.dumps({"type": "result", "structured_output": structured_output}))
