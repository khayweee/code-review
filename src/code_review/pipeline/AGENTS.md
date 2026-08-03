# AGENTS.md — src/code_review/pipeline/

Scope: this package only. See the [root AGENTS.md](../../../AGENTS.md) for repo-wide
conventions.

Milestone 2 (issues #13, #14) is implemented: `step.py` defines `Step`/`StepContext`/
`StepOutcome`. It runs every step in `steps`, in list order, unconditionally — a plain loop
with no branching on any prior step's `StepOutcome`; step order is a property of the list
the caller passes, never of anything a step returns. `Step.run` is `async`, mirroring the
`agent/` layer's async contract rather than wrapping it in sync code. `StepOutcome.findings`
is typed as bare `object`, not the Milestone 5 `Finding` schema; a step's own code narrows
it back via `isinstance` (see `tests/pipeline/test_executor.py`'s `ReviewFindings`/
`OrderProbe` for the pattern).

Milestone 13's #39 changed `executor.py`'s sole entry point from a coroutine returning
`list[StepOutcome]` to an async generator: `run_steps(steps, ctx) -> AsyncIterator[StepEvent]`.
It yields a "running" `StepEvent` immediately before each `step.run(ctx)` call and a
"completed" one (that step's `StepOutcome` plus `time.monotonic()`-based timing)
immediately after, still strictly in list order — pure infrastructure so #40's TUI has a
live signal to render; no behavior change to which steps run or what they produce.
`step_name` comes from `step.get_name()`, a concrete method on `Step` (now a nominal `ABC`,
not a structural `Protocol`) that defaults to `type(self).__name__`; every real
implementation explicitly subclasses `Step`, and only a step needing a name distinct from
its class would override `get_name()`. Events are not held anywhere beyond the caller's own
iteration — no database, no resume path (deliberate, see `docs/GATE-MODEL.md`).
Test convention: real `Step` implementations, a real temporary git checkout with a real
`git diff`, and the real Milestone 1 `ClaudeCLI` pointed at fake CLI scripts — never a
mocked `Step` or `Agent`; tests drain the stream with `[e async for e in run_steps(...)]`
before asserting. The fixed-order regression
(`test_executor_runs_steps_in_fixed_list_order_against_real_diff`) proves ordering through
a real on-disk side effect (one fake CLI's marker file, checked by the next one) rather
than by relabeling — a reordered or dropped step flips or omits an assertion.

`findings.py` (Milestone 5, issue #26) defines `Finding`, `action_or_default`, and
`has_blocking_finding`. A later structural refactor moved a fourth function here from
`steps/review.py`: `filter_pipeline_owned_delivery_findings`, the deterministic filter that
strips pipeline-owned-delivery-scoped findings from a `ReviewStep` answer and resets
`risk_level` to `"low"` when that was the sole basis for an elevated verdict. It takes a
`ReviewOutput` (defined in `steps/review.py`) but avoids inverting the `steps/` depends on
`pipeline/` invariant by importing that type under `TYPE_CHECKING` only -- the same
narrow, non-circular exception `pipeline/step.py` already uses for `steps.intent.Intent`;
see this function's own docstring for the exact mechanics.

`step.py`'s `ActivityReporter` (Milestone 14, issue #66) is a structural, one-method
`Protocol` — `pipeline/`/`steps/` depend only on its `activity(label)` signature and never
import `tui/` directly, the same rule `on_input_needed` already follows. `StepContext.
activity_reporter` carries an optional instance; `StepContext.report_activity(label)` is
the single-line call site (`async with ctx.report_activity("..."): ...`) that delegates to
`activity_or_nullcontext(self.activity_reporter, label)` when a step has a `ctx` in hand
(issue #65's `ReviewStep`). `tui.activity.ActivityRelay` satisfies the Protocol purely
structurally — see `tui/AGENTS.md`'s "The `ActivityRelay` seam" section for the consuming
side.

**Ambient reporting (issue #64)**: `steps/gitutils.py`'s `run_git` has no `StepContext` to
read `activity_reporter` off of, and `steps/rebase.py`'s own call sites must not gain one
(that repo-wide constraint is exactly why this exists). `step.py`'s module-level
`current_activity_reporter` (a `contextvars.ContextVar[ActivityReporter | None]`) carries
whichever reporter is in scope for the currently running step; `executor.run_steps` is the
sole writer, `.set()`/`.reset()`-ing it immediately around each `step.run(ctx)` call from
that step's own `ctx.activity_reporter`, so the ambient value never outlives or leaks past
one step's execution. `run_git` reads it directly via `.get()`, and both this ContextVar
and `activity_or_nullcontext` are unprefixed (unlike `tui/activity.py`'s own, module-
private `_current_activity_id`) because a different package (`steps/`) needs to read them.
This is the only case so far where `pipeline/` exposes ambient (non-`ctx`-threaded) state
across the `steps/` boundary — keep it that way; a second such seam is a sign the
`StepContext` parameter itself should have grown instead.

**The approval park (Milestone 7's first ticket, issue #80)** is landed: `executor.py`'s
`run_steps` now stops right after yielding a step's "completed" `StepEvent` whenever that
step's `StepOutcome.needs_approval` is True, awaiting `StepContext.on_approval_needed(
step_name, outcome)` and blocking for one of "approve"/"skip"/"abort" -- see `executor.py`'s
own module docstring ("The approval park" section) for the full behavior and
`pipeline/step.py`'s `StepContext.on_approval_needed` field comment for the exact callable
shape. `executor.ApprovalNotAttachedError` (no relay attached; fails closed, mirroring
`agent/errors.py`'s `StdinBlockedError`) and `executor.RunAbortedError` (a human chose
"abort") both live in `executor.py` itself, not a dedicated `errors.py` -- this package has
no such module yet and two exceptions with exactly one raiser each didn't justify adding
one. `approve`/`skip` are otherwise identical from this loop's own perspective; only the
caller (`tui/app.py`'s `ReviewApp`, via its own `ApprovalRelay`-driven worker) tells them
apart, for display purposes only (`tui/AGENTS.md`'s "The `ApprovalRelay` seam" section).
Proven both with a synthetic parked `StepOutcome` (`tests/pipeline/test_executor.py`'s
"The approval park" section) and end to end against `steps/rebase.py`'s already-shipped
issue #24 guard (`tests/test_cli_review.py`'s `repo_with_unpushed_local_default_commits`).

**The fix-round loop (Milestone 7's second ticket, issue #81)** is landed: `pipeline/step.py`
extends `StepContext` immutably with a `fix_round: FixRound | None` field (`FixRound` is a
frozen dataclass wrapping one `instructions: str`, collapsing the automatic and human-typed
paths to a single shape) and extends the approval seam from a bare `Decision` string to
`ApprovalResponse(decision: ApprovalDecision, instructions: str | None)`, where
`ApprovalDecision` gains a fourth value, `"fix"`, alongside issue #80's `"approve"`/
`"skip"`/`"abort"`. `executor.py`'s per-step body is now an inner `while True` loop
(`dataclasses.replace`-ing an evolving `round_ctx`, never mutating the caller's own `ctx`)
nested inside the outer `for step in steps:` loop -- see that module's own docstring's "The
fix-round loop" section for the full shape. Gated entirely on a new `Step.supports_fix_round:
ClassVar[bool]` (default `False`, overridden `True` only by `steps/review.py`'s `ReviewStep`)
so that `outcome.auto_fixable` alone never drives the loop -- this is what keeps
`steps/test_sufficiency.py`'s `TestSufficiencyStep` (which already computes a genuine
`auto_fixable=True` today but doesn't yet consume `fix_round`, that's #82) on exactly its
pre-#81 park-only path; see this file's root sibling for the reasoning that led to that
mechanism. `pipeline/findings.py`'s `describe_auto_fix_findings` renders the automatic
path's `FixRound.instructions` from whichever auto-fix findings triggered the round. The
automatic path is capped by a small module-level constant in `executor.py`
(`_MAX_AUTO_FIX_ROUNDS`, a plain code constant, not a config field -- Milestone 10 owns
making the round cap configurable); the human "fix" response at a park is uncapped by
design -- a person can choose it as many times in a row as they want. Once the automatic
cap is exhausted on a still-`auto_fixable` outcome, or a step's outcome is `needs_approval`
in the first place, the loop parks exactly as issue #80 already does (`needs_approval`/
cap-exhausted-`auto_fixable` are the only two conditions that ever park; they are otherwise
mutually exclusive by construction, per `steps/review.py`'s own comment on that invariant).
Proven with a synthetic `supports_fix_round=True` step (`tests/pipeline/test_executor.py`'s
"The fix-round loop" section: exactly-one-automatic-round, cap exhaustion parks, the
fail-safe-default regression through the full loop, `supports_fix_round=False` staying
inert to `auto_fixable`, and the human "fix" response's own uncapped repeat) and, end to
end, against a real `ReviewStep` (`tests/steps/test_review.py`'s "Fix mode (issue #81)"
section: a real fake-CLI round-trip whose fix round makes a genuine on-disk edit and
returns a fresh `ReviewOutput`) and a real `code-review review` run (`tests/test_cli_review.
py`'s `test_review_reaches_success_via_reviewsteps_automatic_fix_round_with_no_park`).

The fail-safe-default rule itself (`Finding.action` unset/null/unrecognized always resolves
to `"ask-user"`, never `"no-op"`/`"auto-fix"`) is already pinned by `tests/pipeline/
test_findings.py`, from Milestone 5 -- #81 does not change it, only adds a bounded auto-fix
path that runs *before* a finding would otherwise reach this park (and a dedicated
regression test proves the two compose correctly -- see above). #82 (mirroring this
mechanism for `TestSufficiencyStep`) and #78 (suggestion-selection/`EditStep`/yolo-mode) are
both still open. Milestone 9 owns the head-continuity guard's exact comparison rule.
