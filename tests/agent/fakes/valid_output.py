#!/usr/bin/env python3
"""Fake Claude CLI that proves real argv, cwd, schema, and process semantics."""

from __future__ import annotations

import sys

from _shared import build_response, print_json

prompt = sys.stdin.read()
response = build_response(prompt)

if prompt == "omit structured output":
    response.pop("structured_output")
    response["result"] = "unstructured answer"

print_json(response)
