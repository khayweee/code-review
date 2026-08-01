#!/usr/bin/env python3
"""Fake Claude CLI that ignores SIGTERM and spawns a live grandchild, then hangs.

Only SIGKILL can end this process, so it exercises the SIGTERM-to-SIGKILL escalation
path in ``_terminate_process_group``.
"""

from __future__ import annotations

import signal
import subprocess
import sys
import time
from pathlib import Path

signal.signal(signal.SIGTERM, signal.SIG_IGN)

grandchild = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(3600)"])
Path("grandchild.pid").write_text(str(grandchild.pid))

sys.stdin.read()  # consume the prompt so the parent's write doesn't block
time.sleep(3600)
