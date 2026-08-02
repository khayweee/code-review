#!/usr/bin/env python3
"""Fake Claude CLI that prints a prompt, then blocks waiting for an answer on stdin.

Does not validate the initial prompt the parent writes to stdin, but does discard it (via
`drain_available_stdin`) before waiting for the real answer -- the parent
(`claude_cli._run_with_stdin_relay`) deliberately never closes its write end for this
path, so those bytes have no EOF or delimiter of their own; left unread, they would sit in
the pipe and get picked up by the next `readline()` call, corrupting it with a
prompt+answer run-together (a `sys.stdin.read()` to consume them properly would, for the
same reason, block forever waiting for an EOF that never comes). Once discarded, this
prints a known prompt to stdout, flushing explicitly (buffering could otherwise hold the
bytes back long enough that the parent's idle-timeout detection never sees them), then
blocks on `sys.stdin.readline()` for one line answering it. Once that line arrives, it
builds and prints the usual JSON envelope with the received answer embedded (via
`build_response`'s ``answer`` field), so a test can assert the answer flowed all the way
through, and exits 0.

Serves both required tests for issue #41: with `on_input_needed` supplied, the parent
detects the stall, relays `PROMPT`, and unblocks this script with the callback's answer.
With no callback supplied, this script blocks forever (it never receives an answer) and
the parent's own idle timeout fires instead, raising `StdinBlockedError`.
"""

from __future__ import annotations

import sys

from _shared import build_response, drain_available_stdin, print_json

PROMPT = "Allow file write to notes.txt? (y/n): "

drain_available_stdin()

print(PROMPT, end="", flush=True)

answer = sys.stdin.readline().strip()

print_json(build_response(answer))
