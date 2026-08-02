# AGENTS.md — src/code_review/pipeline/

Scope: this package only. See the [root AGENTS.md](../../../AGENTS.md) for repo-wide
conventions.

Milestone 2 (issues #13, #14) is implemented: `step.py` defines `Step`/`StepContext`/
`StepOutcome`, `executor.py` exposes `run_steps(steps, ctx) -> list[StepOutcome]` as the
sole entry point. It runs every step in `steps`, in list order, unconditionally — a plain
loop with no branching on any prior step's `StepOutcome`; step order is a property of the
list the caller passes, never of anything a step returns. Both are `async` throughout —
`Step.run` and `run_steps` mirror the `agent/` layer's async contract rather than wrapping
it in sync code. `StepOutcome.findings` is typed as bare `object`, not the Milestone 5
`Finding` schema; a step's own code narrows it back via `isinstance` (see
`tests/pipeline/test_executor.py`'s `ReviewFindings`/`OrderProbe` for the pattern).
`run_steps` holds outcomes in memory only — no database, no resume path (deliberate, see
`docs/GATE-MODEL.md`). Test convention: real `Step` implementations, a real temporary git
checkout with a real `git diff`, and the real Milestone 1 `ClaudeCLI` pointed at fake CLI
scripts — never a mocked `Step` or `Agent`. The fixed-order regression
(`test_executor_runs_steps_in_fixed_list_order_against_real_diff`) proves ordering through
a real on-disk side effect (one fake CLI's marker file, checked by the next one) rather
than by relabeling — a reordered or dropped step flips or omits an assertion.

Once the fix/approval loop (Milestone 7) lands, record here: the fail-safe-default
regression test name(s) and the bounded-vs-unbounded fix-round asymmetry. Milestone 9
owns the head-continuity guard's exact comparison rule.
