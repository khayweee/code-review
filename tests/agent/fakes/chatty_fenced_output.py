#!/usr/bin/env python3
"""Fake Claude CLI whose JSON envelope is wrapped in a fenced code block."""

from __future__ import annotations

import json
import sys

from _shared import build_response

prompt = sys.stdin.read()
response = build_response(prompt)

print("Sure, here is the result you asked for:")
print("```json")
print(json.dumps(response))
print("```")
print("Let me know if you need anything else.")
