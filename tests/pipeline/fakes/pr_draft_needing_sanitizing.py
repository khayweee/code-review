#!/usr/bin/env python3
"""Fake Claude CLI returning a `PRDraft`-shaped answer that breaks every rule the prompt
asks for, so `PRStep`'s code-level sanitizing is what has to fix it: a title carrying a
markdown heading marker, an embedded newline and more than GitHub's 256 characters, plus
`what_changed` entries that echo a generated section heading, carry their own bullet
marker, and are blank.
"""

from __future__ import annotations

import json
import sys

sys.stdin.read()  # drain the prompt; this fixture's answer doesn't depend on its contents

response = {
    "structured_output": {
        "title": "## feat(agent): retry transient backend failures\nand also " + "x" * 300,
        "what_changed": [
            "## Testing",
            "- Transient backend failures are now retried.",
            "   ",
            "Backoff grows exponentially\nbetween attempts.",
        ],
    }
}
print(json.dumps(response))
