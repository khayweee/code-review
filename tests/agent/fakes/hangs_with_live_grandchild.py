#!/usr/bin/env python3
"""Fake Claude CLI that spawns a live grandchild, then hangs forever itself.

Simulates an unresponsive agent process with a live descendant: never prints JSON,
never exits on its own. Used to prove cancellation cleanup reaches the whole process
group, not just the direct child.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

grandchild = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(3600)"])
Path("grandchild.pid").write_text(str(grandchild.pid))

sys.stdin.read()  # consume the prompt so the parent's write doesn't block
time.sleep(3600)
