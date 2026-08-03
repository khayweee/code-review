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

A later structural refactor moved the prompt-construction functions themselves --
`wrap_intent`/`redact_secrets`/`strip_adversarial` out of `intent.py`, and
`intent_conformance_clause`/the prompt-assembly function out of `review.py` -- into the new
sibling package [`prompt/`](../prompt/AGENTS.md), since they are pure string builders with
no step-orchestration concern. `intent.py` now holds only the `Intent` dataclass and
`IntentStep`; `review.py` now holds only `ReviewOutput` and `ReviewStep`, importing
`build_review_prompt` from `code_review.prompt.review`. The deterministic
pipeline-owned-delivery scope filter (`filter_pipeline_owned_delivery_findings`) moved out
of `review.py` too, into `pipeline/findings.py`, alongside its `Finding`-processing
siblings -- see that package's own `AGENTS.md`.

`review.py` is implemented (Milestone 5, issues #26/#27); `test_sufficiency.py` and `pr.py`
remain unimplemented (Milestones 6 and 8, see
[docs/ROADMAP.md](../../../docs/ROADMAP.md)). Once their real prompts/schemas land, record
here: the blocking-findings gate's definition and any PR-body byte-budget/truncation rules.
