"""Executor: linear step runner now, fix/approval loop later.

Milestone 2 (see docs/ROADMAP.md): `for step in steps: step.run(ctx)` — no auto-fix, no
approval gates yet. Milestone 6 adds the fix/park state machine around that loop, plus
the fail-safe default: an unclassified finding action resolves to "ask-user," never
"auto-fix." Milestone 8 adds the head-continuity ("last approved SHA") guard.
"""
