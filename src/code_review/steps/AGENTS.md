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

`review.py` is implemented (Milestone 5, issues #26/#27); `pr.py` remains unimplemented
(Milestone 8, see [docs/ROADMAP.md](../../../docs/ROADMAP.md)). Once its real prompt/schema
lands, record here any PR-body byte-budget/truncation rules.

`test_sufficiency.py` is implemented (Milestone 6, issue #59): `TestSufficiencyOutput`/
`TestArtifact` and `TestSufficiencyStep`, mirroring `review.py`'s split of schema/
orchestration from prompt construction (`build_test_sufficiency_prompt` lives in
`code_review.prompt.test_sufficiency`). `TestSufficiencyStep.run` reuses
`has_blocking_finding`/`action_or_default` (`pipeline/findings.py`) unmodified -- the same
shared blocking-findings gate `ReviewStep` uses -- but does NOT call
`filter_pipeline_owned_delivery_findings`; that scope filter is Review-specific (it resets
a `ReviewOutput`-only `risk_level` field `TestSufficiencyOutput` deliberately does not
have). Not yet registered in `STEP_REGISTRY`'s `IMPLEMENTED_STEPS` or wired into `cli.py`
(issue #60) or the TUI (issue #61).

`gitutils.py`'s `run_git` reports itself as a timed activity (Milestone 14, issue #64) for
every call it makes, with zero changes at any of `rebase.py`'s own call sites -- it reaches
the running step's `ActivityReporter` ambiently (`pipeline.step.current_activity_reporter`),
not through a parameter, since `run_git` has no `StepContext`. Any *new* function added
here that shells out to `git` inherits this for free by calling `run_git` internally, the
same way `ref_sha`/`is_ancestor`/`conflicted_files` do now -- do not add a second, parallel
subprocess-spawning path that bypasses `run_git` (e.g. a raw `asyncio.create_subprocess_exec`
call elsewhere in this package), or it silently loses both the non-blocking (#62) and
activity-reporting (#64) guarantees this module exists to centralize. See
`pipeline/AGENTS.md`'s "Ambient reporting (issue #64)" section for the ContextVar's own
contract and lifetime.
