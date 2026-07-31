"""Step protocol, StepContext, StepOutcome — Milestone 2 (see docs/ROADMAP.md).

Planned shape: `Step` is a Protocol/ABC with one method, `run(ctx) -> StepOutcome` — don't
build an elaborate step framework up front, everything else is composition on top of
that. `StepContext` is a bag of per-run dependencies/state. `StepOutcome` reports back
`needs_approval`, `auto_fixable`, and schema-validated `findings`.
"""
