"""Subprocess adapter for the `claude` CLI — Milestone 1 (see docs/ROADMAP.md).

Planned shape: spawn `claude` per call in its own process group (so cancellation can
kill the whole group, not just the leader), stream structured JSON off stdout, and
enforce a wait-timeout backstop so a surviving grandchild holding stdout/stderr can't
wedge shutdown forever.
"""
