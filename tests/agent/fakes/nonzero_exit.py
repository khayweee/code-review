#!/usr/bin/env python3
"""Fake Claude CLI that starts, then exits non-zero without an answer."""

from __future__ import annotations

import sys

sys.stdin.read()
print("permission denied: could not write to workspace", file=sys.stderr)
sys.exit(2)
