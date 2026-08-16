# AGENTS.md — src/code_review/steps/

Scope: this package only. See the [root AGENTS.md](../../../AGENTS.md) for repo-wide
conventions.

## worktree.py

`WorktreeStep`, the pipeline's actual first step (`STEP_REGISTRY`'s first entry): creates a
throwaway `git worktree`, checked out **detached** at `ctx.branch`'s tip commit, and
redirects every later step's `ctx.cwd` at it via `StepOutcome.cwd_override` -- see
`pipeline/AGENTS.md`'s WorktreeStep section for the full mechanism and why it needed a new
field rather than reusing `step_outcomes`.

- Moved here from a short-lived top-level `src/code_review/worktree.py` the moment it grew a
  real `Step` -- that placement's whole justification (no `pipeline`/`steps` dependency) no
  longer held once it depended on both.
- `resolve_branch_head_short_sha`/`create_worktree` are async, built on `gitutils.py`'s
  `run_git`, exactly like every other step's git subprocess work (non-blocking, reported as
  ambient TUI activity). `remove_worktree` stays sync and does NOT go through `run_git`:
  `cli.py`'s `review` calls it directly after the TUI has fully exited (no running step, so
  no ambient `ActivityReporter` for it to report through), the same pre/post-TUI sync
  convention `cli.py`'s own `_verify_branch`/`_diff_against_default_branch` already use.
  `--force`d, since a run may leave uncommitted edits behind (e.g. an unfinished fix round
  never committed) -- `--keep-worktree` is the escape hatch for a user who wants those
  preserved instead of discarded by this cleanup.
- `create_worktree` checks `ctx.branch` out with `git worktree add --detach`, never by name.
  Reviewing the branch you're currently on (so `ctx.branch` is already HEAD in the user's
  real checkout) is the ordinary workflow, not an edge case, and a real by-name checkout
  would either refuse (git's own "already checked out" collision) or, forced past, share the
  branch *ref* across both worktrees -- `RebaseStep`'s in-place rebase inside the throwaway
  worktree would then silently rewrite the user's own checkout's branch ref out from under
  its stale index/working tree. Detached, that ref is never touched at all, so it can never
  collide with (or corrupt) any other checkout of the same branch. See `pr.py`'s own bullet
  below for what this costs `PRStep`.
- Naming: `<state_dir>/worktrees/code_review_<branch_name>_<short_hash_head>`, where
  `branch_name` is `ctx.branch` with `/` (and other filesystem-unsafe characters) replaced
  so the directory name is always one valid path segment, and `short_hash_head` is `ctx.
  branch`'s own tip commit's short SHA, resolved in the user's real repo before the worktree
  exists.

## intent.py

Holds the `Intent` dataclass and `IntentStep`, the first step to actually answer "what is
this change trying to do" (`worktree.py`'s `WorktreeStep` runs before it -- pure environment
setup, not part of answering that question) — carries `ctx.intent` (built once by `cli.py`
from `--intent`) forward with no agent call.

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
- Also threads `result.usage` onto its returned `StepOutcome.usage` (see
  `pipeline/AGENTS.md`'s "Run report" section) -- no shape change to the step itself.

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
- Also threads `result.usage` onto its returned `StepOutcome.usage`, mirroring `review.py`'s
  identical addition (see `pipeline/AGENTS.md`'s "Run report" section).

## pr.py

Assembles PR evidence and opens the pull request via `gh` - the pipeline's last step
(Milestone 8, issue #119).

- Fully deterministic for this ticket - no agent/LLM call anywhere in `PRStep.run`. Title
  is a static conventional-commit-style placeholder (`"chore: update pull request"`);
  "What Changed" comes straight from `gitutils.run_git(["diff", "--name-status",
  f"origin/{default_branch}...{branch}"], ...)`, called directly here per `gitutils.py`'s
  own anticipated-consumer note rather than needing a second gitutils primitive. #121
  (blocked by this ticket) replaces the title/"What Changed" section with an agent draft
  plus this same deterministic fallback; #122 adds a PR-body byte-budget/truncation rule
  and screenshot/video artifacts. Neither exists yet.
- Diffs against `origin/<default_branch>`, never the literal local `<default_branch>` ref -
  mirrors `RebaseStep`'s own `git rebase origin/<default_branch>` for the identical reason:
  `RebaseStep` runs earlier in this same pipeline and already does `git fetch origin
  <default_branch>`, which updates the remote-tracking ref, not the local one, so the local
  ref can be arbitrarily stale (never pulled) by the time `PRStep` runs. Diffing against it
  would over-report files - everything origin gained since the local ref was last updated,
  on top of the real feature-branch delta (pinned by a regression test in
  `tests/steps/test_pr.py`).
- **"The branch under review" is `ctx.branch`**, unlike `rebase.py` (which needs no branch
  name at all). `WorktreeStep` checks its worktree out detached (`steps/worktree.py`'s
  module docstring), so `gitutils.current_branch(ctx.cwd)` would just return `None` here;
  `gh pr create --head`/`gh pr view` only ever needed the branch as a name, never an actual
  local checkout of it. Equal to `default_branch` (a constructor field, same "not
  auto-detected" reasoning as `RebaseStep`'s own) means skip immediately - no `git diff`/
  `gh` call at all.
- Intent/Risk/Testing sections are assembled from the pipeline's own already-computed
  state: `ctx.intent.summary` for Intent, and `ctx.step_outcomes.get("ReviewStep")` /
  `ctx.step_outcomes.get("TestSufficiencyStep")` (see `pipeline/AGENTS.md`'s
  `StepContext.step_outcomes` section) narrowed via `isinstance` against
  `ReviewOutput`/`TestSufficiencyOutput` imported directly from their sibling step
  modules - the same runtime import `tui/state.py`'s `latest_findings` already does for
  identical isinstance-narrowing, not the cross-step prompt-function sharing issue #58
  prohibited. A missing or wrong-shaped entry (e.g. a `StepContext` built directly in a
  test with `step_outcomes={}`) omits that section entirely rather than raising or
  rendering a placeholder heading - `PRStep` must work standalone, not only when driven
  through the full executor.
- Find-or-create/update goes through `scm/github.py`: `find_pull_request_for_branch` first,
  then `create_pull_request` or `update_pull_request` depending on whether one already
  exists for the branch. `gh_executable: str | Path = "gh"` is the subprocess test seam,
  mirroring `ReviewStep.executable`.
- Returns the same empty-findings-shape `StepOutcome(needs_approval=False,
  auto_fixable=False, payload=[])` on every path (skip, create, update) - a PR isn't a
  review target with findings to fix, so `supports_fix_round` stays at `Step`'s default
  `False`.

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
  sibling step module to import" - realized by `steps/pr.py` (issue #119), which calls
  `run_git(["diff", "--name-status", ...])` directly for its deterministic "What Changed"
  section. `scm/github.py`'s `resolve_repo_slug` also calls `run_git` directly, the first
  consumer of this module *outside* `steps/` -- `scm/` importing `steps.gitutils.run_git`
  (rather than a new `gh`-only subprocess helper) is a deliberate, narrow exception, not a
  general "scm/ may import steps/" license; see `scm/AGENTS.md`. `current_branch` (mirrors
  `ref_sha`'s `None`-on-failure convention) has no consumer today -- `pr.py` used it until
  `WorktreeStep` started checking worktrees out detached, at which point it switched to
  reading `ctx.branch` directly (see `worktree.py`'s and `pr.py`'s own bullets above).
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
