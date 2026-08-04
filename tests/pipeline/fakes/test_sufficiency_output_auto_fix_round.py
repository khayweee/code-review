#!/usr/bin/env python3
"""Fake Claude CLI proving `TestSufficiencyStep`'s automatic fix round end to end (issue
#82), directly against `TestSufficiencyStep`/`run_steps` -- mirrors
`review_output_auto_fix_round.py`'s shape for `ReviewStep` (issue #81) exactly.

Round 1 (a normal run: `steps/test_sufficiency.py`'s `TestSufficiencyStep.run` calls
`build_test_sufficiency_prompt`, so the prompt contains no fix-round text) answers with one
"auto-fix" finding and no "ask-user" finding -- `auto_fixable=True`, `needs_approval=False`.

Round 2 (the automatic fix round: `TestSufficiencyStep.run` calls
`build_test_sufficiency_fix_prompt` instead, whose `_FIX_ROUND_INSTRUCTION` constant --
`prompt/test_sufficiency.py` -- opens with "You are running a fix round") does two things a
real test-writing agent would: writes a real, on-disk test file (proving the fix-mode
prompt really grants (and this fixture really exercises) edit access, not just a second
identical schema request) and answers with a fresh `TestSufficiencyOutput` -- new
`tested`/`testing_summary`/`artifacts` naming the newly-written test, and no findings --
proving the fix round's own returned output is a fresh verdict, not an echo of the finding
that triggered it. The fix-round `testing_summary` also names whether the fix-round prompt
actually carried the auto-fix finding's own description text (via `FixRound.instructions`,
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
    saw_instructions = "write a test for the new greeting line" in prompt

    test_file = Path("test_greeting_written_by_fix_round.py")
    test_file.write_text(
        "def test_greeting_includes_world() -> None:\n"
        "    from pathlib import Path\n\n"
        "    assert Path('greeting.txt').read_text() == 'hello\\nworld\\n'\n"
    )

    response = {
        "structured_output": {
            "findings": [],
            "tested": ["greeting message includes the new line"],
            "testing_summary": (
                f"fix round: wrote missing test (saw_instructions={saw_instructions})"
            ),
            "artifacts": [
                {
                    "kind": "written-test",
                    "description": "test_greeting_includes_world exercises the new line",
                    "location": "test_greeting_written_by_fix_round.py:1",
                }
            ],
        }
    }
else:
    response = {
        "structured_output": {
            "findings": [
                {
                    "severity": "warning",
                    "description": "write a test for the new greeting line",
                    "action": "auto-fix",
                    "review_scope": "source",
                }
            ],
            "tested": [],
            "testing_summary": "initial pass: no test found for the new greeting line",
            "artifacts": [],
        }
    }
print(json.dumps(response))
