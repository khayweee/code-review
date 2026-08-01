# AGENTS.md — src/code_review/pipeline/

Scope: this package only. See the [root AGENTS.md](../../../AGENTS.md) for repo-wide
conventions.

Milestone 2 slice 1 (issue #13) is implemented: `step.py` defines `Step`/`StepContext`/
`StepOutcome`, `executor.py` exposes `run_step(step, ctx)` as the sole entry point. Both
are `async` throughout — `Step.run` and `run_step` mirror the `agent/` layer's async
contract rather than wrapping it in sync code. `StepOutcome.findings` is typed as bare
`object`, not the Milestone 4 `Finding` schema; a step's own code narrows it back via
`isinstance` (see `tests/pipeline/test_executor.py`'s `ReviewFindings` for the pattern).
`run_step` holds outcomes in memory only — no database, no resume path (deliberate, see
`docs/GATE-MODEL.md`). Test convention: real `Step` implementations, a real temporary git
checkout with a real `git diff`, and the real Milestone 1 `ClaudeCLI` pointed at a fake
CLI script — never a mocked `Step` or `Agent`.

Slice 2 (issue #14) still needs to land: generalize `run_step` to a fixed-order
`list[Step]`. Once the executor and fix/approval loop (Milestone 6) land, record here: the
fail-safe-default regression test name(s), the bounded-vs-unbounded fix-round asymmetry,
and the head-continuity guard's (Milestone 8) exact comparison rule.
