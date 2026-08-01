#!/usr/bin/env python3
"""Fake Claude CLI for step "b" in the multi-step ordering test (issue #14).

Mirror of `order_step_a.py` with the roles reversed -- see that file's docstring.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.stdin.read()  # drain the prompt; ordering here doesn't depend on its contents

Path("step-b-ran").touch()
saw_other = Path("step-a-ran").exists()

response = {"structured_output": {"step": "b", "saw_other": saw_other}}
print(json.dumps(response))
