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
  "..."): ...`) that delegates to `activity_or_nullcontext`.
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
  (`FixRound` is a frozen dataclass wrapping `instructions: str`, collapsing the automatic
  and human-typed paths to a single shape).
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
