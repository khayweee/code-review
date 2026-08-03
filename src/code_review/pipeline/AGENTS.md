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

Once the fix/approval loop (Milestone 7) lands, record here: the fail-safe-default
regression test name(s) and the bounded-vs-unbounded fix-round asymmetry. Milestone 9
owns the head-continuity guard's exact comparison rule.
