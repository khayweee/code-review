# AGENTS.md — src/code_review/steps/

Scope: this package only. See the [root AGENTS.md](../../../AGENTS.md) for repo-wide
conventions.

## intent.py

Holds the `Intent` dataclass and `IntentStep`, the pipeline's first step — carries
`ctx.intent` (built once by `cli.py` from `--intent`) forward with no agent call.

- `IntentStep` never calls `wrap_intent` and never hands wrapped intent text forward
  through its `StepOutcome` — it carries `ctx.intent` as-is (Milestone 3, issue #19).
- Each later step (`review.py`, `test_sufficiency.py`, `pr.py`) calls
  `wrap_intent(ctx.intent.summary, ctx.intent.source)` itself, at its own prompt site, off
  the shared `ctx.intent` — never via a prior step's outcome. Get this backwards (threading
  wrapped text through `StepOutcome.payload` instead of re-deriving it) and a step
  downstream of a hypothetical future intent-mutating step would silently see stale wrapped
  text.
- The prompt-construction functions themselves — `wrap_intent`/`redact_secrets`/
  `strip_adversarial` — moved out to the sibling [`prompt/`](../prompt/AGENTS.md) package in
  a later structural refactor, since they're pure string builders with no step-orchestration
  concern. `intent.py` now holds only the `Intent` dataclass and `IntentStep`.

## review.py

The correctness/alignment review step: one agent call producing findings plus a required
risk verdict, with an added fix-mode round for driving edits before parking for a human.

- `review.py` now holds only `ReviewOutput` (the schema) and `ReviewStep` (orchestration) —
  `intent_conformance_clause`/the prompt-assembly functions moved to
  `code_review.prompt.review`, and `filter_pipeline_owned_delivery_findings` moved to
  `code_review.pipeline.findings` alongside its `Finding`-processing siblings (Milestone 5,
  issues #26/#27).
- Owns Milestone 7's fix-mode addition (issue #81): the first (and, until #82, only) step to
  set `supports_fix_round: ClassVar[bool] = True` (`pipeline/step.py`), so
  `pipeline/executor.py`'s bounded auto-fix-before-park round and the uncapped human-"fix"
  park response both apply.
- `ReviewStep.run`'s own shape does not change for a fix round — still exactly one
  `ctx.agent.run` call per invocation, still the same
  `filter_pipeline_owned_delivery_findings`/`has_blocking_finding`/`auto_fixable`
  post-processing applied every round. The only new branch is which prompt-assembly function
  to call: `code_review.prompt.review.build_review_fix_prompt(ctx)` when
  `ctx.fix_round is not None`, `build_review_prompt(ctx)` otherwise — steering the agent at
  the live working tree rather than the stale `ctx.diff` string a prior round's own edits
  invalidate (see `prompt/AGENTS.md`'s `review.py` section for what the fix-mode prompt asks
  the agent to do differently).

## test_sufficiency.py

Decides whether existing tests would catch a regression, mirroring `review.py`'s
schema/orchestration split and its fix-mode support.

- `TestSufficiencyOutput`/`TestArtifact` and `TestSufficiencyStep` (Milestone 6, issue #59);
  prompt construction (`build_test_sufficiency_prompt`) lives in
  `code_review.prompt.test_sufficiency`, which `TestSufficiencyStep.run` imports and calls.
- Reuses `has_blocking_finding`/`action_or_default` (`pipeline/findings.py`) unmodified — the
  same shared blocking-findings gate `ReviewStep` uses — but does **not** call
  `filter_pipeline_owned_delivery_findings`; that scope filter is Review-specific (it resets
  a `ReviewOutput`-only `risk_level` field `TestSufficiencyOutput` deliberately does not
  have).
- Owns Milestone 7's fix-mode addition (issue #82, mirroring `review.py`'s own #81): the
  second step (after `ReviewStep`) to set `supports_fix_round: ClassVar[bool] = True`, so
  the same bounded auto-fix-before-park round and uncapped human-"fix" park response apply.
  `TestSufficiencyStep.run`'s shape does not change either — still one `ctx.agent.run` call
  per invocation, still no `filter_pipeline_owned_delivery_findings` call either way — only
  the prompt-assembly branch differs:
  `code_review.prompt.test_sufficiency.build_test_sufficiency_fix_prompt(ctx)` when
  `ctx.fix_round is not None`, `build_test_sufficiency_prompt(ctx)` otherwise, kept as a
  separately-defined, same-named local constant rather than an import from `review.py`, per
  issue #58's no-cross-step-sharing decision.
- Registered in `STEP_REGISTRY`'s `IMPLEMENTED_STEPS` and wired into `cli.py` (issue #60).

## pr.py

Assembles PR evidence and opens the pull request — the pipeline's last step.

- Remains unimplemented (Milestone 8, see [docs/ROADMAP.md](../../../docs/ROADMAP.md)).
- Once its real prompt/schema lands, record here any PR-body byte-budget/truncation rules.

## rebase.py

Updates the branch onto the latest default branch before review, so later steps never
answer against a stale diff.

- Owned only the guard/orchestration logic deciding *when* a git result becomes a blocking
  `Finding` (`_unpushed_local_default_finding`, `RebaseStep.run`) even before the extraction
  below — it never held generic git-subprocess plumbing itself.
- Its own call sites needed zero changes when `gitutils.py`'s `run_git` picked up ambient
  activity reporting (issue #64) — see `gitutils.py` below.

## gitutils.py

Shared `git`-subprocess plumbing with no step-orchestration or `Finding`-construction logic
of its own.

- Extracted out of `steps/rebase.py` (Milestone 4, issues #23/#24): a staff-engineer audit
  found `_run_git`, `_rebase_in_progress`, `_ref_sha`, `_is_ancestor`, and
  `_conflicted_files` were pure git-subprocess primitives with no knowledge of `RebaseStep`,
  `StepOutcome`, or `Finding`.
- Left unprefixed (no leading underscore), matching `steps/intent.py`'s
  `wrap_intent`/`redact_secrets`/`strip_adversarial` convention, signalling "safe for a
  sibling step module to import" — anticipated future consumer is `steps/pr.py`'s
  deterministic fallback, which will need the same kind of `git diff --name-status` call.
- `run_git` reports itself as a timed activity (Milestone 14, issue #64) for every call it
  makes, with zero changes at any of `rebase.py`'s own call sites — it reaches the running
  step's `ActivityReporter` ambiently (`pipeline.step.current_activity_reporter`), not
  through a parameter, since `run_git` has no `StepContext`. Any *new* function added here
  that shells out to `git` inherits this for free by calling `run_git` internally, the same
  way `ref_sha`/`is_ancestor`/`conflicted_files` do now — do not add a second, parallel
  subprocess-spawning path that bypasses `run_git` (e.g. a raw
  `asyncio.create_subprocess_exec` call elsewhere in this package), or it silently loses both
  the non-blocking (#62) and activity-reporting (#64) guarantees this module exists to
  centralize. See `pipeline/AGENTS.md`'s "Ambient reporting (issue #64)" section for the
  ContextVar's own contract and lifetime.
