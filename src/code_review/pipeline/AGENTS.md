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

Once the fix/approval loop (Milestone 7) lands, record here: the fail-safe-default
regression test name(s) and the bounded-vs-unbounded fix-round asymmetry. Milestone 9
owns the head-continuity guard's exact comparison rule.
