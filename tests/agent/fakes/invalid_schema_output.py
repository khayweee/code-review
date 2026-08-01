#!/usr/bin/env python3
"""Fake Claude CLI whose structured answer does not fit the caller's schema."""

from __future__ import annotations

import json
import sys

sys.stdin.read()
print(json.dumps({"structured_output": {"answer": 12345}}))
