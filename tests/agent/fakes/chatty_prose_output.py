#!/usr/bin/env python3
"""Fake Claude CLI whose JSON envelope is buried in a paragraph of preamble."""

from __future__ import annotations

import json
import sys

from _shared import build_response

prompt = sys.stdin.read()
response = build_response(prompt)

print("I looked into this and here is what I found: " + json.dumps(response) + " Hope that helps!")
