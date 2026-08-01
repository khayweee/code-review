#!/usr/bin/env python3
"""Fake Claude CLI for step "a" in the multi-step ordering test (issue #14).

Runs in the shared repo checkout (`RunOpts.cwd`), so it can leave a real, on-disk marker
that a later step's own fake CLI process can observe -- proving, from inside a real
subprocess call rather than from test-harness bookkeeping, which step actually ran first.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.stdin.read()  # drain the prompt; ordering here doesn't depend on its contents

Path("step-a-ran").touch()
saw_other = Path("step-b-ran").exists()

response = {"structured_output": {"step": "a", "saw_other": saw_other}}
print(json.dumps(response))
