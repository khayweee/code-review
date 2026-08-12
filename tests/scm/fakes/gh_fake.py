#!/usr/bin/env python3
"""Fake `gh` CLI for `tests/scm/test_github.py` and `tests/steps/test_pr.py`: never talks
to a real GitHub host. Reads `--body-file -` stdin when present, and always appends one
JSON line (`{"args": [...], "stdin": "..."}`) to the path in `FAKE_GH_LOG_FILE` (if set) so
a test can assert on exactly what `scm/github.py`/`steps/pr.py` sent -- e.g. that `--repo`
was always explicit, or what body text a create/edit call carried.

Env vars controlling the answer:
- `FAKE_GH_FAIL`: if set, every subcommand fails, printing this string to stderr, exit 1.
- `FAKE_GH_EXISTING_PR_JSON`: if set, `pr view` succeeds and prints this JSON verbatim.
  Unset: `pr view` fails with "no pull requests found for branch ..." on stderr, exit 1
  (mirroring real `gh`'s own message for a branch with no PR).
- `FAKE_GH_NEW_PR_NUMBER`: PR number `pr create` reports back in its printed URL, default
  "1" (mirrors real `gh pr create`, which prints only the new PR's URL on success).
"""

from __future__ import annotations

import json
import os
import sys

args = sys.argv[1:]

stdin_text = ""
if "--body-file" in args:
    body_file_index = args.index("--body-file") + 1
    if body_file_index < len(args) and args[body_file_index] == "-":
        stdin_text = sys.stdin.read()

log_path = os.environ.get("FAKE_GH_LOG_FILE")
if log_path:
    with open(log_path, "a") as log_file:
        log_file.write(json.dumps({"args": args, "stdin": stdin_text}) + "\n")

if os.environ.get("FAKE_GH_FAIL"):
    print(os.environ["FAKE_GH_FAIL"], file=sys.stderr)
    sys.exit(1)

subcommand = args[0] if args else ""
action = args[1] if len(args) > 1 else ""

if subcommand == "pr" and action == "view":
    existing = os.environ.get("FAKE_GH_EXISTING_PR_JSON")
    if existing:
        print(existing)
        sys.exit(0)
    branch = args[2] if len(args) > 2 else ""
    print(f'no pull requests found for branch "{branch}"', file=sys.stderr)
    sys.exit(1)

if subcommand == "pr" and action == "create":
    repo = args[args.index("--repo") + 1]
    number = os.environ.get("FAKE_GH_NEW_PR_NUMBER", "1")
    print(f"https://github.com/{repo}/pull/{number}")
    sys.exit(0)

if subcommand == "pr" and action == "edit":
    sys.exit(0)

print(f"fake gh: unhandled invocation {args}", file=sys.stderr)
sys.exit(1)
