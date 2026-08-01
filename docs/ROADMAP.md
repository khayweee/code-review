# Build roadmap

This is the working plan for building `code_review` step by step. It's adapted from a
design study of a prior Go implementation of the same kind of tool, condensed and
rewritten for the choices actually made here: `uv` for packaging, Typer for the CLI, and
an Agent abstraction that shells out to a coding-agent CLI (starting with `claude`) rather
than calling a raw LLM API directly.

Build in this order. Each milestone should produce something runnable, and depends only
on the milestones before it.

## Milestones

0. **Scaffold** (done). Repo structure, tooling, harness docs. No pipeline logic.
1. **Agent adapter** (`src/code_review/agent/`). One backend — `claude_cli.py`, subprocess
   per call, structured JSON out, process-group cleanup verified with a deliberately
   hanging test command. Nothing else works without this.
2. **Linear step runner** (`src/code_review/pipeline/step.py`, `executor.py`). A
   `list[Step]`, a `for step in steps: step.run(ctx)` loop. No auto-fix, no approval gates
   yet — just prove one prompt → one schema-validated response → one recorded outcome, on
   a real diff. Outcomes live in memory for the run's duration; no database and no
   resume-after-crash machinery. This is not an oversight — see
   [`GATE-MODEL.md`](GATE-MODEL.md) for why the Go tool's SQLite-backed run-state machine
   exists to serve its daemon, and is cut here along with that daemon.
3. **Intent, explicit-only** (`src/code_review/steps/intent.py`). Require `--intent` on
   the CLI; skip transcript inference for now. Write the sanitize-and-wrap function once,
   reuse it everywhere the intent text gets embedded in a prompt.
4. **Rebase, keep the diff current** (`src/code_review/steps/rebase.py`). Before Review
   runs, sync the branch onto the latest default branch (fetch + rebase) so later steps
   never review a stale diff. On conflict, stop and surface it as a blocking finding
   rather than resolving silently — the fail-safe default (see below) points at a human,
   and a rebase conflict is exactly the kind of judgement call the pipeline shouldn't
   make for you. Also carry over the "bundled local commits" guard: if the branch's
   history includes commits that only exist on your local default branch (never pushed to
   `origin/<default>`), rebasing would silently drag another workstream's unrelated work
   into this PR — detect that and ask, don't guess. Skip the fork-remote tracking,
   force-push detection, and pushed-branch-mirror machinery the Go tool's Rebase step
   carries — that exists to serve the gate's push-interception model
   ([`GATE-MODEL.md`](GATE-MODEL.md)), which this project isn't building.
5. **Review, single-pass** (`src/code_review/steps/review.py`). One prompt, one schema —
   findings *and* risk fields together, no fix loop yet. This is the first milestone
   where the tool actually answers "is this correct, and how risky is it" on a real diff.
6. **Test sufficiency** (`src/code_review/steps/test_sufficiency.py`). Same
   schema-and-ladder pattern as review (existing test → write test → manual verification
   → honest warning); reuse the blocking-findings gate from milestone 5.
7. **Auto-fix + approval loop** (rest of `pipeline/executor.py`). Add the fix/park state
   machine around milestones 5-6. This is where the `Finding.action` fail-safe default
   and the bounded-(auto)-vs-unbounded-(human) fix-round asymmetry matter.
8. **PR creation** (`src/code_review/steps/pr.py`, `src/code_review/scm/github.py`).
   Deterministic fallback body first (raw `git diff --name-status`), then the
   agent-drafted title/"What Changed" bullets, then wire in the Intent section and the
   risk-flavored line from milestone 5. Requires the `gh` CLI (not installed as of the
   scaffold — install it when starting this milestone).
9. **Head continuity** (rest of `pipeline/executor.py`). Add the in-memory "last approved
   SHA" guard once more than one process/worktree can touch the same checkout. Not needed
   while everything is single-threaded and single-worktree.
10. **Trust boundary** (`src/code_review/config.py`). Partition config into
    code-executing/trusted-only fields vs. descriptive/pushed-branch-is-fine fields, pinned
    to a fetched-fresh exact commit. Add this the moment the tool points at a repo where
    someone other than you can open a PR — not before.
11. **v2 extras**: session reuse across pipeline rounds, transcript-based intent
    inference, multi-backend fallback. Real efficiency/UX improvements, but nothing in
    milestones 1-8 requires them.
12. **Install, update, uninstall** (`scripts/install.sh`, `src/code_review/install_state.py`,
    `cli.py`'s `update`/`uninstall` commands). Orthogonal to pipeline progress — doesn't
    touch `pipeline/`, `steps/`, or `scm/`, so it can land in any order relative to
    milestones 4-11. Delegates all packaging work to `uv tool install`/`upgrade`/
    `uninstall` (this project's already-chosen packaging tool) rather than reimplementing
    venv management or binary replacement the way a Go binary distribution (`no-mistakes`)
    has to. Deliberately excludes a background daemon/service — `no-mistakes`' daemon
    exists to serve its sub-second git-push-gate response and to keep slow-cold-start
    agent backends warm, neither of which applies here yet (see [`GATE-MODEL.md`](GATE-MODEL.md));
    revisit once a real async trigger exists. Also excludes automated version-bumping —
    the version stays manually set until this milestone's own release story matures.

## Module sketch

```
src/code_review/
  agent/
    base.py          # Agent protocol, RunOpts, Result
    claude_cli.py     # subprocess adapter for the `claude` CLI
    schema.py         # shared structured-output extraction + validation
  pipeline/
    step.py           # Step protocol, StepContext, StepOutcome
    executor.py        # the fix/approval loop
    findings.py         # Finding/Findings, action_or_default, merge_overrides
  steps/
    intent.py
    rebase.py           # sync onto latest default branch before review; conflicts block
    review.py           # correctness + risk fields live together
    test_sufficiency.py
    pr.py
  scm/
    github.py           # `gh` CLI wrapper
  config.py             # trusted-vs-descriptive split
  cli.py                # entry point: `review`, `update`, `uninstall`
  install_state.py       # install-lifecycle state directory (~/.code-review)
scripts/
  install.sh             # one-shot install script (`uv tool install` under the hood)
```

## Five goals → what to imitate first

| # | Goal | Imitate first |
|---|---|---|
| 1 | Detect intent behind recent diffs | Explicit `--intent` flag + one sanitize/wrap function reused everywhere. Transcript-based inference is v2. |
| 2 | Check changes are logically correct / aligned with intent | Single-pass full-diff review with a structured findings schema. |
| 3 | Check tests are sufficient for regression detection | The decision ladder (existing test → write test → manual verification → honest warning) as a prompt contract, backed by a shared blocking-findings gate. |
| 4 | Assess risk level for the change | Required `risk_level`/`risk_rationale` fields on the *same* schema as goal 2 — no separate risk step. |
| 5 | Create a PR with clear docs and evidence | Deterministic fallback body first; agent-drafted "What Changed" bullets plus deterministically-assembled Intent/Risk/Pipeline sections second. |

## Recurring design lessons to carry over

More transferable than any specific API:

- **Fail-safe defaults point toward a human, never toward auto-proceeding.**
- **`None`/unset means "unknown," never a fabricated zero or empty value.**
- **Provenance changes trust/weight, never whether guardrails apply.**
- **Prompt instructions are not enough for a hard invariant — pair them with a
  deterministic, code-level check on the structured output.**
- **Every LLM-dependent step needs a deterministic fallback for when the call itself fails.**
- **Partition trust by "can this execute code," and pin trusted sources to an exact commit
  fetched fresh** — but only once exposed to untrusted contributors, not before.
