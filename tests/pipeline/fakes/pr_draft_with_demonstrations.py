#!/usr/bin/env python3
"""Fake Claude CLI returning a `PRDraft` carrying demonstrations: two `behavior` ones (which
must land in a single shared table) and one `api` one (a fenced request/response block).

The values deliberately match the shapes recorded in `steps/AGENTS.md`'s Evidence section
notes, so the body this produces can be compared against the rendering that was verified
against GitHub's own markdown renderer.
"""

from __future__ import annotations

import json
import sys

sys.stdin.read()  # drain the prompt; this fixture's answer doesn't depend on its contents

response = {
    "structured_output": {
        "title": "feat(upload): retry failed uploads with exponential backoff",
        "what_changed": [
            "Failed uploads now retry with exponential backoff instead of giving up.",
            "A retry ceiling stops the loop after five attempts and raises.",
        ],
        "demonstrations": [
            {
                "kind": "behavior",
                "label": "Backoff delay",
                "given": "`attempt=3`",
                "was": "`1s`",
                "now": "`4s`",
            },
            {
                "kind": "behavior",
                "label": "Retry ceiling",
                "given": "`attempt=6`",
                "was": "retries forever",
                "now": "raises after 5",
            },
            {
                "kind": "api",
                "label": "POST /api/retry rate-limits after 3 attempts",
                "given": 'POST /api/retry {"id": 1}',
                "now": '429 Too Many Requests\n{"retry_after": 30}',
            },
        ],
    }
}
print(json.dumps(response))
