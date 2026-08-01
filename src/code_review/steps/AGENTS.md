# AGENTS.md — src/code_review/steps/

Scope: this package only. See the [root AGENTS.md](../../../AGENTS.md) for repo-wide
conventions.

Milestone 3 (issue #19) landed `IntentStep` in `intent.py`: it carries `ctx.intent`
(constructed once by `cli.py` from `--intent`) as its findings and makes no agent call.
**`IntentStep` never calls `wrap_intent` and never hands wrapped intent text forward
through its `StepOutcome`.** Each later step (`review.py`, `test_sufficiency.py`, `pr.py`,
as their own milestones land) calls `wrap_intent(ctx.intent.summary, ctx.intent.source)`
itself, at its own prompt site, off the shared `ctx.intent` -- not via any prior step's
outcome. Get this backwards (e.g. threading wrapped text through `StepOutcome.findings`
instead of re-deriving it from `ctx.intent`) and a step downstream of a hypothetical
future intent-mutating step would silently see stale wrapped text.

`review.py`, `test_sufficiency.py`, and `pr.py` remain unimplemented (Milestones 4-5 and
7, see [docs/ROADMAP.md](../../../docs/ROADMAP.md)). Once real prompts/schemas land,
record here: the exact intent-conformance clause wording, the blocking-findings gate's
definition, and any PR-body byte-budget/truncation rules.
