#!/usr/bin/env python3
"""Fake Claude CLI proving `ReviewStep`'s automatic fix round end to end (issue #81),
directly against `ReviewStep`/`run_steps` (not the full `code-review review` command --
see `tests/pipeline/fakes/claude_superset_auto_fix_then_clean.py` for that one).

Round 1 (a normal run: `steps/review.py`'s `ReviewStep.run` calls `build_review_prompt`,
so the prompt contains no fix-round text) answers with one "auto-fix" finding and no
"ask-user" finding -- `auto_fixable=True`, `needs_approval=False`.

Round 2 (the automatic fix round: `ReviewStep.run` calls `build_review_fix_prompt`
instead, whose `_FIX_ROUND_INSTRUCTION` constant -- `prompt/review.py` -- opens with "You
are running a fix round") does two things a real edit-capable agent would: makes a real,
on-disk edit to the file under review (proving the fix-mode prompt really grants (and this
fixture really exercises) edit access, not just a second identical schema request) and
answers clean, with a distinct `risk_rationale` -- proving the fix round's own returned
`ReviewOutput` is a fresh verdict, not an echo of the finding that triggered it. The
fix-round `risk_rationale` also names whether the fix-round prompt actually carried the
auto-fix finding's own description text (via `FixRound.instructions`,
`pipeline/findings.py`'s `describe_auto_fix_findings`) -- proving the instructions this
fixture's own round 1 answer produced actually made it into round 2's prompt, not just
that a second round happened.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

prompt = sys.stdin.read()
is_fix_round = "You are running a fix round" in prompt

if is_fix_round:
    saw_instructions = "extract a helper function" in prompt

    greeting = Path("greeting.txt")
    greeting.write_text(greeting.read_text() + "fixed\n")

    response = {
        "structured_output": {
            "findings": [],
            "risk_level": "low",
            "risk_rationale": f"fix round: clean after edits (saw_instructions={saw_instructions})",
        }
    }
else:
    response = {
        "structured_output": {
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
        }
    }
print(json.dumps(response))
