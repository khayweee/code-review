# AGENTS.md — src/code_review/pipeline/

Scope: this package only. See the [root AGENTS.md](../../../AGENTS.md) for repo-wide
conventions.

## Milestone 2 (#13, #14)

Runs a fixed sequence of review steps end to end for the first time.

- `step.py` defines `Step`/`StepContext`/`StepOutcome`.
- `executor.py` runs every step in `steps`, in list order, unconditionally — a plain loop
  with no branching on any prior step's `StepOutcome`; order is a property of the caller's
  list, never of anything a step returns.
- `Step.run` is `async`, mirroring the `agent/` layer's async contract.
- `StepOutcome.findings` was typed as bare `object` (pre-dates Milestone 5's `Finding`
  schema); a step narrowed it back via `isinstance` (see `tests/pipeline/test_executor.py`'s
  `ReviewFindings`/`OrderProbe`). A later refactor renamed it to `payload` and closed the
  type to `list[Finding] | ReviewOutput | TestSufficiencyOutput | Intent` -- see "Findings
  rename + closed union" below.

## Milestone 5 (#26)

Findings get a shared shape and a safe default, so a step can never silently drop a risky
finding.

- `findings.py` adds `Finding`, `action_or_default`, `has_blocking_finding`.
- Fail-safe-default rule: `Finding.action` unset/null/unrecognized always resolves to
  `"ask-user"`, never `"no-op"`/`"auto-fix"` (pinned by `tests/pipeline/test_findings.py`).
- A later structural refactor moved a fourth function here from `steps/review.py`:
  `filter_pipeline_owned_delivery_findings`, which strips pipeline-owned-delivery-scoped
  findings from a `ReviewOutput` and resets `risk_level` to `"low"` when that was the sole
  basis for an elevated verdict. It imports `ReviewOutput` under `TYPE_CHECKING` only, to
  avoid inverting the `steps/` depends on `pipeline/` invariant (same narrow exception
  `pipeline/step.py` uses for `steps.intent.Intent`).

## Milestone 13 (#39)

The TUI can now show steps running live instead of only a final result.

- `executor.py`'s sole entry point changes from a coroutine returning `list[StepOutcome]`
  to an async generator: `run_steps(steps, ctx) -> AsyncIterator[StepEvent]`.
- Yields a "running" `StepEvent` immediately before each `step.run(ctx)` and a "completed"
  one (that step's `StepOutcome` plus `time.monotonic()`-based timing) immediately after,
  still strictly in list order — pure infrastructure so the TUI has a live signal to
  render; no change to which steps run or what they produce.
- `step_name` comes from `step.get_name()`, a concrete method on `Step` (now a nominal
  `ABC`, not a structural `Protocol`) defaulting to `type(self).__name__`.
- Events are not held anywhere beyond the caller's own iteration — no database, no resume
  path (deliberate, see `docs/GATE-MODEL.md`).
- Test convention: real `Step` implementations, a real temporary git checkout with a real
  `git diff`, and the real Milestone 1 `ClaudeCLI` pointed at fake CLI scripts — never a
  mocked `Step` or `Agent`.

## Milestone 14 (#66, #64)

Long-running steps can now surface a "what's happening right now" label to the TUI,
including from code that has no direct access to the step's own context.

- `step.py`'s `ActivityReporter` is a structural, one-method `Protocol` —
  `pipeline/`/`steps/` depend only on its `activity(label)` signature and never import
  `tui/` directly.
- `StepContext.activity_reporter` carries an optional instance; `StepContext.
  report_activity(label)` is the single-line call site (`async with ctx.report_activity(
  "..."): ...`) that delegates to the module-level `report_activity` (renamed from this
  milestone's original `activity_or_nullcontext`; a later milestone added `log_activity`,
  its one-shot sibling).
- `tui.activity.ActivityRelay` satisfies the Protocol purely structurally.
- Ambient reporting (#64): `steps/gitutils.py`'s `run_git` has no `StepContext` to read
  `activity_reporter` off of. `step.py`'s module-level `current_activity_reporter`
  (a `contextvars.ContextVar[ActivityReporter | None]`) carries whichever reporter is in
  scope for the currently running step; `executor.run_steps` is the sole writer,
  `.set()`/`.reset()`-ing it immediately around each `step.run(ctx)` call, so the ambient
  value never outlives one step's execution. `run_git` reads it directly via `.get()`.
  This is the only case so far where `pipeline/` exposes ambient (non-`ctx`-threaded) state
  across the `steps/` boundary — a second such seam is a sign `StepContext` itself should
  grow instead.
- A later increment (pass/fail signal on activities) changed `activity()`'s return type
  from `AbstractAsyncContextManager[None]` to `AbstractAsyncContextManager[ActivityHandle]`
  — a small `@dataclass(slots=True)` (`error: str | None = None`, `.fail(detail)`) the
  block's own body can call to mark its "finished" event as failed, e.g. `run_git`/
  `scm/github.py`'s `_run_gh` calling `activity.fail(f"exit {process.returncode}")` on a
  nonzero subprocess exit — a log-visible signal only; neither ever raises on an ordinary
  command failure. `report_activity`'s no-reporter branch changed from `nullcontext()` to
  `nullcontext(ActivityHandle())` so `.fail(...)` never needs a null check at the call site.
  `tui.schemas.ActivityEvent` gained a matching `error: str | None = None`, set only on a
  "finished" event whose block called `.fail(...)`; `ActivityRelay.__init__` also gained an
  `on_event` callback, invoked synchronously wherever an event would be queued — the seam
  `run_log.py`'s `RunLogWriter` (see its own module docstring) hooks to persist the same
  stream to disk. See `pipeline/README.md`'s "Reporting sub-step activity" section for the
  full usage contract.
- `steps/tool_activity.py`'s `tool_stream_relay` (shared between `ReviewStep`/
  `TestSufficiencyStep`) is the other consumer of the one-shot `log`/`log_activity` path
  above, translating a streamed agent call's `StreamEvent`s into activity log lines --
  tool calls, errored tool results, and (a later increment) the model's own streamed
  narration text. See that module's own docstring for the full mapping.

## Milestone 7, ticket 1: the approval park (#80)

A run can now pause and wait for a human to approve, skip, or abort a risky step, instead
of always running unattended.

- `executor.py`'s `run_steps` now stops right after yielding a step's "completed"
  `StepEvent` whenever that step's `StepOutcome.needs_approval` is `True`, awaiting
  `StepContext.on_approval_needed(step_name, outcome)` and blocking for one of
  "approve"/"skip"/"abort".
- `executor.ApprovalNotAttachedError` (no relay attached; fails closed) and
  `executor.RunAbortedError` (a human chose "abort") both live in `executor.py` itself.
- `approve`/`skip` are otherwise identical from this loop's perspective; only the caller
  (`tui/app.py`'s `ReviewApp`) tells them apart, for display purposes only.
- Proven both with a synthetic parked `StepOutcome` and end to end against `steps/
  rebase.py`'s already-shipped issue #24 guard.

## Milestone 7, ticket 2: the fix-round loop (#81, #82)

Instead of only parking on a fixable finding, a step can now attempt bounded automatic
fixes first, and a human can request as many manual fix rounds as they want at a park.

- `pipeline/step.py` extends `StepContext` immutably with `fix_round: FixRound | None`
  (`FixRound` -- a frozen dataclass wrapping `instructions: str`, collapsing the automatic
  and human-typed paths to a single shape -- lives in `pipeline/schemas.py` alongside every
  other passive plumbing type `pipeline/` shares with its callers).
- The approval seam extends from a bare `Decision` string to `ApprovalResponse(decision:
  ApprovalDecision, instructions: str | None)`, where `ApprovalDecision` gains a fourth
  value, `"fix"`, alongside `"approve"`/`"skip"`/`"abort"`.
- `executor.py`'s per-step body becomes an inner `while True` loop, nested inside the outer
  `for step in steps:` loop, replacing an evolving `round_ctx` each round via
  `round_ctx.with_fix_round(instructions)` (`pipeline/step.py`, a thin `dataclasses.replace`
  wrapper) rather than mutating the caller's own `ctx`.
- Gated entirely on a new `Step.supports_fix_round: ClassVar[bool]` (default `False`) so
  that `outcome.auto_fixable` alone never drives the loop.
- `pipeline/findings.py`'s `describe_auto_fix_findings` renders the automatic path's
  `FixRound.instructions` from whichever auto-fix findings triggered the round.
- The automatic path is capped by `executor.py`'s `_MAX_AUTO_FIX_ROUNDS` module-level
  constant (Milestone 10 owns making the cap configurable); the human "fix" response at a
  park is uncapped by design.
- Once the automatic cap is exhausted on a still-`auto_fixable` outcome, or a step's
  outcome is `needs_approval` in the first place, the loop parks exactly as #80 already
  does — `needs_approval`/cap-exhausted-`auto_fixable` are the only two conditions that
  ever park, and are otherwise mutually exclusive by construction.
- Landed first for `steps/review.py`'s `ReviewStep` (#81); `steps/test_sufficiency.py`'s
  `TestSufficiencyStep` mirrors it in #82 (see `steps/AGENTS.md`).
- Proven with a synthetic `supports_fix_round=True` step (exactly-one-automatic-round, cap
  exhaustion parks, the fail-safe-default regression through the full loop,
  `supports_fix_round=False` staying inert to `auto_fixable`, and the human "fix"
  response's own uncapped repeat) and, end to end, against a real `ReviewStep`
  (`tests/steps/test_review.py`'s "Fix mode" section) and a real `code-review review` run.
- Issue #98 later added a sibling pure function, `describe_finding_decisions`, reusing
  `describe_auto_fix_findings`'s exact rendering convention for a different producer: a
  human's own per-finding "fix" instructions from `tui.widgets.FindingsList`'s per-finding
  approval-park decisions, not the automatic auto-fix path. `ApprovalResponse`/`FixRound`
  themselves stay untouched by that issue -- both remain a single flat `instructions: str |
  None` -- since the aggregation happens entirely on the `tui/` side before a park resolves;
  see `tui/AGENTS.md`'s "Findings box" section for the full per-finding decision model.

## Milestone 8: `StepContext.step_outcomes`, a read-only cross-step reporting channel (#119)

`PRStep` (issue #119) needs to summarize `ReviewStep`'s risk verdict and
`TestSufficiencyStep`'s testing summary in its PR body, but no existing seam let a step
read a sibling's already-computed `StepOutcome` -- `executor.run_steps`'s `ctx` was never
reassigned across steps (only `round_ctx` evolved, within one step's own fix-round loop,
then got discarded). This adds a narrow, read-only, additive extension, deliberately
mirroring `fix_round`/`with_fix_round`'s own precedent (same file, same shape, low risk):

- `step.py`: `StepContext` gains `step_outcomes: Mapping[str, StepOutcome] =
  field(default_factory=dict)` -- earlier steps' final outcomes, keyed by
  `step.get_name()`.
- `executor.py`'s `run_steps`: once a step's slot fully settles -- the inner `while True`
  loop has broken, whether via "no park needed" or a park resolving to "approve"/"skip" --
  one line folds `{step_name: outcome}` into the *outer* `ctx.step_outcomes` via
  `dataclasses.replace` and reassigns `ctx` for the next outer-loop iteration, exactly once
  per step slot (never once per fix round, and never onto `round_ctx`, which the next
  step's slot rebuilds from this updated `ctx` anyway).
- `pr.py` reads it via `ctx.step_outcomes.get("ReviewStep")` /
  `ctx.step_outcomes.get("TestSufficiencyStep")`, narrowed with `isinstance` against
  `ReviewOutput`/`TestSufficiencyOutput` imported directly from their sibling step modules
  -- the same runtime import `tui/state.py`'s `latest_findings` already does for identical
  isinstance-narrowing, not the cross-step prompt-function sharing issue #58 prohibited (a
  plain data-type import is a different thing from importing another step's
  prompt-assembly function). A missing or wrong-shaped entry is treated as absent -- the
  section is omitted, never rendered as a placeholder -- so `PRStep` still works when
  driven directly against a hand-built `StepContext` in a test, not only through the full
  executor.

**Why this doesn't reverse the "no step decides control flow from a sibling's outcome"
invariant** (this file's Milestone 2 entry, and `executor.py`'s own module docstring):
nothing here changes which steps run, their order, or whether/how a step re-runs itself --
`step_outcomes` is data a step may choose to read for its own *output* (what it puts in its
own `StepOutcome.payload`), never a signal consulted to decide *whether or how to run*.
Contrast `fix_round`, which does drive control flow (which prompt-assembly function a step
calls) but only ever for that same step re-running itself, never a sibling. `step_outcomes`
crosses that boundary in the opposite, safer direction: read-only, cross-step, but
execution-inert.

Proven in `tests/pipeline/test_executor.py`'s "step_outcomes threading" section: a
synthetic `_ReportingStep` records exactly what `ctx.step_outcomes` it was called with,
proving (a) a later step sees an earlier step's real `StepOutcome`, (b) the first step in a
run sees an empty `step_outcomes`, and (c) a step that goes through automatic fix rounds
before settling contributes exactly one entry -- the final, settled outcome -- never a
stale intermediate round's outcome.

## Findings rename + closed union

`StepOutcome.findings: object` was accurate but unclear -- the field holds four genuinely
different shapes (bare `list[Finding]`, `ReviewOutput`, `TestSufficiencyOutput`, or
`IntentStep`'s bare `Intent`, which isn't findings at all), and every consumer already
duck-typed its way back to one of those four via `isinstance`/`getattr`.

- Renamed to `payload` (it isn't always findings -- `IntentStep` uses it to carry `ctx.intent`
  forward) and typed as the closed union `list[Finding] | ReviewOutput |
  TestSufficiencyOutput | Intent` instead of bare `object`.
- `step.py` `TYPE_CHECKING`-imports `Finding`/`ReviewOutput`/`TestSufficiencyOutput` alongside
  the existing `Intent` import, same narrow exception to `steps/` depends on `pipeline/`.
- `pipeline/findings.py`'s `describe_auto_fix_findings` still can't `isinstance`-check
  `ReviewOutput`/`TestSufficiencyOutput` at runtime (they're `TYPE_CHECKING`-only imports
  here too), so it keeps a `getattr(payload, "findings", None)` duck-typed fallback for that
  branch even though the parameter's static type is now the closed union -- the closed type
  documents the contract for callers; the layering rule still forces duck-typing at this one
  call site.
- `tui/state.py`'s `latest_findings` already imports `ReviewOutput`/`TestSufficiencyOutput` at
  runtime (`tui/` depends on `steps/`), so its `isinstance` narrowing there needed no
  duck-typing workaround, and the closed union let it drop a now-redundant
  `isinstance(findings[0], Finding)` check on the list branch.
- Issue #119's follow-up (surfacing the PR link) added a fifth member, `steps/pr.py`'s
  `PullRequestOutcome` (`url: str`, `number: int`, `created: bool`) -- `PRStep`'s own
  payload once it actually opens/updates a PR, same non-`Finding` precedent as `Intent`.
  `step.py` `TYPE_CHECKING`-imports it from `code_review.steps.pr` alongside the other four,
  same narrow exception. It carries no findings at all, so `describe_auto_fix_findings`'s
  `getattr(payload, "findings", None)` fallback yields `None` for it (same as `Intent`), and
  `tui/state.py`'s `latest_findings` isinstance-narrowing simply never matches it -- neither
  needed a code change for the new member.

## Worktree isolation: `StepContext.branch`, `StepOutcome.cwd_override`, `WorktreeStep`

`cli.py review` used to create the run's throwaway `git worktree` itself, pre-pipeline, and
pass the resulting path straight into `StepContext(cwd=...)`. Moved into a real, registered
first step (`steps/worktree.py`'s `WorktreeStep`) so that *any* caller of `run_steps` --
not only `cli.py`'s specific `review` command -- gets worktree isolation for free, at the
cost of two new, honestly-different additions to the shared types `step_outcomes` alone
didn't cover:

- `StepContext.branch: str` -- required, alongside `cwd`/`agent`/`diff`/`intent`.
  `WorktreeStep` needs the branch under review before any worktree (and so before `ctx.cwd`'s
  HEAD) exists to derive it from. **Only `WorktreeStep` reads this.** `RebaseStep`/`PRStep`
  still re-derive "the branch under review" from `ctx.cwd`'s HEAD (`gitutils.current_branch`)
  exactly as before -- that trick keeps working unmodified because `WorktreeStep` makes
  `ctx.cwd`'s HEAD equal to `ctx.branch` by the time either of them runs. Do not thread
  `ctx.branch` into another step without a real reason; the existing HEAD-derivation trick is
  the intended, single source of truth for every step downstream of `WorktreeStep`.
- `StepOutcome.cwd_override: Path | None = None` -- lets a step redirect `ctx.cwd` for every
  step after it. `executor.run_steps` folds a non-`None` `cwd_override` into the outer `ctx`
  at the exact same point it already folds `step_outcomes` (once a step's slot fully
  settles), via the same `dataclasses.replace(ctx, ...)` call.

**Why this needed a new mechanism instead of reusing `step_outcomes`, and why that doesn't
reverse the "no step branches on a sibling's outcome to decide whether/how to run" invariant
(this file's Milestone 2 entry, `executor.py`'s own module docstring):** every step still
runs, unconditionally, in the same fixed order -- nothing here changes *whether* or *when* a
step runs, only the resource path (`ctx.cwd`) it runs against, which `step_outcomes`'s own
read-only, opt-in-per-consumer contract was never designed to carry. Be honest that this is
narrower than `step_outcomes`'s justification, not a restatement of it: `step_outcomes` is
optional, per-consumer data a step *may choose* to read to shape its own output (e.g. `PRStep`
rendering `ReviewStep`'s risk verdict, or not, if `step_outcomes` is empty); `cwd_override` is
mandatory shared infrastructure state -- every step after `WorktreeStep` *must* see the
redirected `cwd`, or its `git`/agent calls would silently operate on the wrong (or, absent
`WorktreeStep`, nonexistent-until-now) working directory. Only `WorktreeStep` is expected to
ever set it; nothing in the type system stops another step from doing so, but nothing else
should.

**Fix-round interaction**: `WorktreeStep` does not set `supports_fix_round = True` and never
sets `needs_approval`/`auto_fixable`, so `executor.py`'s inner `while True` loop runs it
exactly once per pipeline run -- `round_ctx` and the outer `ctx` are identical for its one
and only round, so there is no meaningful "which `ctx` does `cwd_override` apply to"
ambiguity to resolve. The fold happens on the outer `ctx` at the same point `step_outcomes`
already does, immediately after `WorktreeStep`'s single round settles, before `RebaseStep`
(the next step in `STEP_REGISTRY`) ever builds its own `round_ctx` from that updated `ctx`.

**Failure surface**: `WorktreeStep.run` raises straight through on any git failure --
including `steps/worktree.py`'s `BranchAlreadyCheckedOutError` when `<branch>` is already
checked out elsewhere -- exactly like e.g. `RebaseStep`'s own unclassified-`git`-failure
`RuntimeError`. `executor.run_steps` does nothing special with it; it propagates out of the
async generator, caught by `tui/app.py`'s `_consume_events` and surfaced via `ReviewApp.error`
same as any other step failure, rendered as a normal failed-step Status message through
`cli.py`'s already-generic `if tui_app.error is not None` path. This is a genuine behavior
change from the pre-`WorktreeStep` design (a pre-TUI `typer.Exit` with no TUI flash) but a
deliberate one: it makes worktree-collision failures look and behave like every other
pipeline-step failure, rather than a special case.
