"""Finding / Findings — Milestone 4 (schema), Milestone 6 (fix-loop helpers).

Planned shape: `Finding.action` in {no-op, auto-fix, ask-user}, defaulting to `ask-user`
when unset (the fail-safe default — the single most important rule to preserve in any
reimplementation). `risk_level`/`risk_rationale` are required fields on the same schema
as findings, not a separate risk step.
"""
