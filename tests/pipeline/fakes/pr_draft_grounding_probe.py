#!/usr/bin/env python3
"""Fake Claude CLI proving `PRStep` feeds `TestSufficiencyStep`'s own observations into the
drafting prompt: echoes back, as `what_changed` bullets, whether the prompt it received
carried the grounding instruction and the two observation strings.

Same technique as `review_prompt_probe.py`, and coupled the same way -- the strings below
are the `tested` entry and the artifact description that `tests/steps/test_pr.py`'s shared
`_reviewed_and_tested_step_outcomes` puts on the `TestSufficiencyOutput`.
"""

from __future__ import annotations

import json
import sys

prompt = sys.stdin.read()


def saw(label: str, needle: str) -> str:
    return f"saw {label}" if needle in prompt else f"missing {label}"


response = {
    "structured_output": {
        "title": "chore(pr): probe the drafting prompt",
        "what_changed": [
            saw("grounding instruction", "Ground your demonstrations in what this pipeline"),
            saw("tested behavior", "retry backoff on transient failure"),
            saw("test artifact", "test_retries_on_failure"),
        ],
        "demonstrations": [],
    }
}
print(json.dumps(response))
