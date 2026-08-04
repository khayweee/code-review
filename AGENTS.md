# AGENTS.md

This file is for agentic coding tools working in this repo. `CLAUDE.md` is a one-line
pointer to this file — always edit AGENTS.md directly, never duplicate its content into
CLAUDE.md or let the two drift.

## What this repo is

A personal, from-scratch Python rebuild of a no-mistakes-style agentic code-review/gating
pipeline: detect the intent behind a change, review it for correctness and risk, check
test sufficiency, and open a PR with evidence, with a human approval gate whenever an
agent isn't confident enough to act alone. It is not a port of any other tool — design
decisions here may diverge where Python idioms or this user's preferences differ. See
`docs/ROADMAP.md` for the full build order and the design lessons this project carries
over from studying a prior Go implementation.

**Read `docs/GLOSSARY.md` before writing code, issues, or docs here.** It is the domain
vocabulary: what an Agent, a backend, a step, a finding, a park, and a deterministic
fallback are, plus the four words this repo overloads (`agent`, `fallback`, `review`,
`gate`). Use those terms as defined rather than coining synonyms, and when a term's
meaning changes, edit the glossary in the same commit. It owns what words mean;
`docs/ROADMAP.md` owns why the design is that way.

## Repo layout map

- `src/code_review/cli.py` — Typer entry point (`code-review` command).
- `src/code_review/config.py` — trusted-vs-descriptive config split (not built yet).
- `src/code_review/agent/` — the Agent abstraction: shells out to a coding-agent CLI
  (starting with `claude`), one call in / one result out.
- `src/code_review/pipeline/` — `Step` protocol, `StepContext`/`StepOutcome`, and the
  executor (fixed step order; fix/approval loop added later).
- `src/code_review/steps/` — the actual pipeline steps: intent, rebase, review,
  test_sufficiency, pr.
- `src/code_review/prompt/` — pure prompt-construction helpers (sanitize/wrap/clause
  builders) used by the steps above; a leaf package depended on by `steps/`, not the
  reverse.
- `src/code_review/scm/` — SCM host wrapper (GitHub via the `gh` CLI).
- `src/code_review/tui/` — live pipeline-progress view (Textual), wired into `cli.py review`.
- `tests/` — mirrors `src/code_review/` package-for-package.

## Local verification sequence

- `uv sync`
- `uv run ruff format .`
- `uv run ruff check .`
- `uv run mypy src`
- `uv run pytest`

(All wired into `make check`.)

## Core design invariants (condensed from the design study)

These are carried over from studying a prior implementation of the same kind of tool.
Full rationale for each lives in `docs/ROADMAP.md` — don't re-explain it here, just keep
this list accurate as the invariants actually get implemented.

- **Step order is fixed and hard-coded**, never data-driven or config-reorderable — the
  safety property "nothing ships without being reviewed first" depends on order being
  something no input can rearrange.
- **Fail-safe default**: when a finding's `action` is unclassified, it defaults to
  `ask-user`, never to `auto-fix`. Closing this in the wrong direction is a fail-open hole.
- **The Agent abstraction is "one call in, one result out."** Retry (same backend,
  transient errors) and fallback (different backend, unavailability) are two separate
  mechanisms, not one generic retry loop.
- **Provenance changes trust-weight, never whether sanitization applies.** Explicit vs.
  inferred intent both get identically redacted/wrapped; only the framing of authority
  differs.
- **Every LLM-dependent step needs a deterministic fallback** for when the agent call
  itself fails outright (e.g. a PR body built from `git diff --name-status` if drafting fails).
- **Risk verdict is a required field on the Review step's own output schema**, not a
  separate pipeline step — schema-level `required` gets you "the model cannot skip this"
  for free, which prompt wording alone doesn't.

## Current milestone

Milestone 0 (scaffold) complete. Milestone 1 (agent adapter, issue #2) is functionally
done - sub-issues #3 (round trip) and #4 (chatty-output tolerance) are merged to main; #6
(process-group cleanup on cancellation) is implemented on branch
`feature/6-process-cleanup` but not yet merged despite the issue being closed - reconcile
that (merge or reopen) before treating #2 as closeable.

Milestone 2 (linear step runner, parent issue #12) is closed: #13 (Step/StepContext/
StepOutcome round trip) and #14 (fixed multi-step order via `run_steps`) are both merged.
Scope was deliberately narrower than the full orchestration core in
`no-mistakes/learning/02-orchestration-core-and-state-machine.md`: the auto-fix/approval
state machine is Milestone 7 and head continuity is Milestone 9, both out of scope for #12.

Milestone 3 (`src/code_review/steps/intent.py`, explicit-only intent) is closed: #17
(parent), sliced into #18 (sanitize-and-wrap) and #19 (wire `--intent` into
`StepContext`/`IntentStep`, `cli.py` validation), both merged to main.

Milestone 4 (`src/code_review/steps/rebase.py`, sync onto latest default branch before
Review) was missing from the roadmap entirely until this was caught by comparing against
`no-mistakes`'s fixed step order (`internal/pipeline/steps/common.go`) - see
`docs/ROADMAP.md` milestone 4. Specced as #22, sliced into #23 (rebase onto latest
default, abort cleanly on conflict) and #24 (block on unpushed local-default commits
before rebasing, blocked by #23). #23 is closed and merged to main (PR #52). #24 is
implemented on branch `feature/24-unpushed-commits-guard`, not yet merged.

Milestone 5 (`src/code_review/steps/review.py`, single-pass correctness and risk review)
is specced as #25, sliced into #26 (schema, intent-conformance clause, deterministic
pipeline-owned-delivery scope filter) and #27 (wire `ReviewStep` into the pipeline,
blocked by #26). Sequenced after Milestone 4 per the build order, though the two
milestones' code doesn't share files. Both #26 and #27 are closed and merged to main
(PRs #48 and #50): `pipeline/findings.py`'s `Finding`/`action_or_default`/
`has_blocking_finding`, `steps/review.py`'s `ReviewOutput`/`intent_conformance_clause`/
`filter_pipeline_owned_delivery_findings`, and `ReviewStep` itself - still not registered
in `steps/registry.py`'s `IMPLEMENTED_STEPS` or wired into `cli.py`; that wiring is a
later ticket, not part of #27's own scope.

Milestone 6 (`src/code_review/steps/test_sufficiency.py`, single-pass test-sufficiency
assessment) is specced as #58, sliced into #59 (schema, prompt, `TestSufficiencyStep`
itself), #60 (wiring into `steps/registry.py`'s `IMPLEMENTED_STEPS`/`cli.py`, blocked by
#59), and #61 (TUI display, blocked by #59; most useful once #60 also lands). #59's
`TestSufficiencyOutput`/`TestArtifact` (`steps/test_sufficiency.py`),
`build_test_sufficiency_prompt` (`prompt/test_sufficiency.py`), and `TestSufficiencyStep`
itself are implemented on branch `feature/59-test-sufficiency-schema-step`, not yet
merged. Per #58's Implementation
Decisions, `TestSufficiencyStep` reuses `has_blocking_finding`/`action_or_default`
unmodified but does not call the Review-specific `filter_pipeline_owned_delivery_findings`,
and a shared test-quality-rule constant across the two steps' prompts is explicitly
deferred. Not yet registered in `IMPLEMENTED_STEPS` or wired into `cli.py` (#60) or shown
in the TUI (#61).

Issue #47 (unrelated to Milestone 5 - a bug in Milestone 12's `cli.py` `update` command,
found incidentally while validating #26: `uv tool upgrade`'s stderr carries ANSI color
codes even when piped, which broke the version-line regex) is closed and merged (PR #49).

Milestone 12 (`scripts/install.sh`, `src/code_review/install_state.py`, `cli.py`'s
`update`/`uninstall` commands - orthogonal to pipeline progress, see `docs/ROADMAP.md`
milestone 12) is specced as #30, sliced into #31 (install via `uv tool install`), #32
(update, blocked by #31), and #33 (uninstall, blocked by #31). Implemented and merged
(PR #34). Deliberately excludes a background daemon (tracked in #29) and automated
version-bumping (tracked in #28) as separate future-work issues.

Milestone 13 (`src/code_review/tui/`, a new sibling package to `agent/`/`pipeline/`/
`steps/`/`scm/`, see `docs/ROADMAP.md` milestone 13) is specced as #38, sliced into #39
(executor emits a `StepEvent` stream instead of returning `list[StepOutcome]`), #40 (the
`tui` package itself: live pipeline-progress view, registry-driven backfill of
not-yet-implemented steps, wires `cli.py review` to `run_steps` for real), #41 (relay an
agent subprocess's interactive-input prompts through the TUI via a new
`RunOpts.on_input_needed` seam), and #42 (read-only findings display). All four are
closed and merged to main (PRs #43, #44, #45, #53): #44/#45 landed as `tui/state.py`'s
pure `backfill`, `tui/widgets.py`'s `PipelineBox`, `tui/app.py`'s `ReviewApp`,
`steps/registry.py`'s `STEP_REGISTRY`/`IMPLEMENTED_STEPS`,
`RunOpts.on_input_needed`/`StepContext.on_input_needed`, the stdin-relay seam in
`agent/claude_cli.py`, and `tui/input_relay.py`'s `InputRelay`; #42 landed as
`tui/widgets.py`'s `FindingsBox` and `tui/state.py`'s `latest_findings`, once #27
(Milestone 5) merged and cleared its blocker. Milestone 13 (issue #38) is closed - all
4 sub-issues complete. The interactive approve/skip/abort layer from the reference
screenshot landed as Milestone 7's #80 (see that milestone's own paragraph below); the
"fix" half of that reference screenshot (auto-fix, and a human "fix" response beyond
approve/skip/abort) landed for `ReviewStep` as #81; its mirror for `TestSufficiencyStep`
(#82) is also landed -- see Milestone 7's own paragraph below for the detail.

Milestone 14 (`src/code_review/tui/activity.py`, `pipeline/step.py`, `steps/gitutils.py`,
see `docs/ROADMAP.md` milestone 14) is specced as #63, sliced into #66 (a dedicated
`tui/activity.py` `ActivityRelay` module plus a `pipeline.step.ActivityReporter` Protocol
and `StepContext.report_activity` helper - a second event stream `ReviewApp` drains in its
own worker, not a new `StepEvent` status; proven with a synthetic reporter before any real
producer exists, mirroring #41's own precedent for `InputRelay`), #64 (wires
`gitutils.run_git` through that helper, so `RebaseStep`'s git calls render as nested,
individually-timed lines under the Rebase row; blocked by #66), and #65 (wires the same
helper into `ReviewStep`'s one agent call as a single coarse activity span, deliberately
not finer-grained per the `Agent` protocol's "no streaming" contract in `docs/GLOSSARY.md`;
blocked by #66, independent of #64). Its prerequisite standalone bugfix, #62 (found
incidentally while scoping this milestone, same pattern as #47 under Milestone 5) -
`gitutils.run_git` blocked the asyncio event loop for the duration of every git call,
freezing the Pipeline box's elapsed-duration tick - is closed: `run_git`/`ref_sha`/
`is_ancestor`/`conflicted_files` are `async def` via `asyncio.create_subprocess_exec`
(matching `agent/claude_cli.py`'s own precedent), `rebase_in_progress` stays sync (no
subprocess call), and `RebaseStep` awaits every call, mechanically. #66 is also closed:
`tui/activity.py`'s `ActivityRelay`/`ActivityEvent` (Textual-import-free, automatic
contextvar-based nesting), `pipeline/step.py`'s `ActivityReporter` Protocol and
`StepContext.activity_reporter`/`report_activity`, `tui/state.py`'s `ActivityRow`/
`backfill_activities` (folded into `StepRow.activities`/`backfill`), `tui/widgets.py`'s
nested-line rendering in `PipelineBox`, and the third `_consume_activities` worker plus
`activity_relay` wiring in `tui/app.py`/`cli.py` are all in place, proven end to end with a
synthetic reporter (`tests/tui/test_app.py`) exactly as #41 proved `InputRelay`. #64 is
closed: `steps/gitutils.py`'s `run_git` reports itself as a timed activity for every call it
makes, with zero changes at `steps/rebase.py`'s own call sites -- it reaches the reporter
ambiently via `pipeline.step.current_activity_reporter`, a `contextvars.ContextVar`
`executor.run_steps` binds from `ctx.activity_reporter` around each `step.run(ctx)` call
(see `pipeline/AGENTS.md`'s "Ambient reporting (issue #64)" section for the full design).
Wiring in real, fast-finishing producers for the first time also exposed a genuine bug in
#66's own consumer, `tui/app.py`'s `_consume_activities`: it tagged an activity's
"started"/"finished" halves independently with `self._running_step` at receipt time, which
crashed with a `KeyError` (seen against both a real `RebaseStep` run and, independently,
`ReviewStep`'s own call shape) since that worker and the `StepEvent` worker are separately
scheduled tasks with no ordering guarantee between them -- fixed by `app.py`'s
`_tag_activity_events`, which derives ownership purely from `StepEvent` timestamp windows at
render time rather than from live worker state, so it carries no dependency on scheduling
order at all (see `tui/AGENTS.md`'s "The `ActivityRelay` seam" section for the full race and
why this design, not the simpler "record the owner once, on `started`" one, was adopted).
#65 is also closed: `ReviewStep.run` wraps its one `ctx.agent.run(...)` call in `async with
ctx.report_activity("Agent: reviewing diff via claude")`. Milestone 14 (issue #63) is
closed -- all three sub-issues (#64, #65, #66) and prerequisite #62 are done.

Milestone 7 (`docs/ROADMAP.md` milestone 7: auto-fix + approval loop, the rest of
`pipeline/executor.py`) is specced as #79 (parent), sliced into #80 (approval park core),
#81 (the fix-round mechanism, blocked by #80), and #82 (the same mechanism's second
application to `TestSufficiencyStep`, blocked by #81). #80 is closed: `StepOutcome.
needs_approval` now actually stops `executor.run_steps` right after a step's "completed"
`StepEvent` is yielded, via a new `StepContext.on_approval_needed` seam mirroring
`on_input_needed`'s shape exactly (structural callable, `None`-default, fails closed with a
new `executor.ApprovalNotAttachedError` when unattached); "approve"/"skip" both let the run
continue (skip vs. approve is purely a `tui/`-side rendering distinction, not something
`run_steps` branches on), "abort" raises a new `executor.RunAbortedError` that unwinds the
run and surfaces through `cli.py`'s already-existing `ReviewApp.error` exit path with no
dedicated `except` clause needed. The human-facing side is a new `tui.approval_relay.
ApprovalRelay`/`tui.screens.ApprovalPromptScreen` pair (mirroring `InputRelay`/
`InputPromptScreen`'s issue #41 shape) and a fourth `ReviewApp` worker
(`_relay_approval`); `tui/state.py`'s `Status` Literal gained `"parked"`/`"skipped"`, both
overrides of an already-"completed" `StepEvent` (see `pipeline/AGENTS.md`'s and
`tui/AGENTS.md`'s own sections on this design nuance), not a third state alongside
pending/running/completed/failed. Proven against a synthetic parked `StepOutcome`
(`tests/pipeline/test_executor.py`, `tests/tui/test_app.py`) and, end to end, against
`steps/rebase.py`'s already-shipped issue #24 unpushed-local-default guard -- which, before
#80, silently rebased anyway despite returning `needs_approval=True`
(`tests/test_cli_review.py`'s `repo_with_unpushed_local_default_commits`).

#81 is closed: `pipeline/step.py` gained `FixRound` (a frozen dataclass wrapping one
`instructions: str`) and `StepContext.fix_round`, plus `ApprovalDecision`/`ApprovalResponse`
(extending #80's bare `Decision` string with a fourth choice, "fix", and optional
free-text `instructions`) and `Step.supports_fix_round: ClassVar[bool]` (default `False`).
`pipeline/executor.py`'s per-step body is now an inner round loop, gated on
`step.supports_fix_round` so `outcome.auto_fixable` alone never drives it (at the time #81
landed, this is what kept `TestSufficiencyStep` on its then-still-park-only path, since it
already computed a genuine `auto_fixable=True` but did not yet consume `fix_round` -- #82,
landed since, is what gave it that consumer, see below): an eligible step with an
`auto_fixable` outcome gets re-run automatically, up to a small module-level
`_MAX_AUTO_FIX_ROUNDS` cap, before any park; once that cap is exhausted (or the step never
supported fix rounds), it falls through to #80's park, whose fourth response, "fix",
re-runs the step with a human's own typed instructions -- uncapped, unlike the automatic
path. `steps/review.py`'s `ReviewStep` was the first step to opt in
(`supports_fix_round = True`), with a new fix-mode prompt
(`prompt/review.py`'s `build_review_fix_prompt`) that tells the agent to edit the working
tree and re-review from scratch, steering it at the live tree rather than the stale
`ctx.diff` a prior round's own edits invalidate. `tui/screens.py`'s `ApprovalPromptScreen`
gained a "fix" keybinding/button, and `tui/app.py`'s `ReviewApp._relay_approval` follows it
with `InputPromptScreen` to collect instructions before resolving the pending approval.
Proven with a synthetic `supports_fix_round` step (`tests/pipeline/test_executor.py`),
a real `ReviewStep` fix round that makes a genuine on-disk edit
(`tests/steps/test_review.py`), the "fix" modal round-trip (`tests/tui/test_app.py`), and a
real `code-review review` run reaching success purely via the automatic path with no human
interaction (`tests/test_cli_review.py`). #82 (the same mechanism's second application to
`TestSufficiencyStep`) is closed: `steps/test_sufficiency.py`'s `TestSufficiencyStep` sets
`supports_fix_round: ClassVar[bool] = True` and its `run` branches its prompt exactly the
way `ReviewStep.run` does -- `prompt/test_sufficiency.py`'s new
`build_test_sufficiency_fix_prompt(ctx)` when `ctx.fix_round is not None`,
`build_test_sufficiency_prompt(ctx)` otherwise -- with no other change to `run`'s shape (no
`filter_pipeline_owned_delivery_findings` call either way, unaffected by this ticket, as it
was before). `build_test_sufficiency_fix_prompt` mirrors `build_review_fix_prompt`'s shape:
a fix instruction, `ctx.fix_round.instructions`, `ctx.diff` behind a same-named,
separately-defined `_STALE_DIFF_WARNING` local constant (not imported from
`prompt/review.py`, per issue #58's no-cross-step-sharing decision), the wrapped intent
block, then all four of this module's guardrail clauses (unlike `build_review_prompt`'s
one conditional clause, all four here are unconditional, so the fix-mode prompt includes
all four too). No change to `pipeline/executor.py`, `pipeline/step.py`,
`pipeline/findings.py`, or `tui/` -- entirely a `steps/test_sufficiency.py`/
`prompt/test_sufficiency.py` change, proven with a real automatic fix round through
`run_steps`/`pipeline/executor.py` against a real `TestSufficiencyStep`
(`tests/steps/test_test_sufficiency.py`'s "Fix mode (issue #82)" section, mirroring
`test_review.py`'s own section), including the fail-safe-default regression (an
unset-`action` finding never reaches the automatic path) proven against a real
`TestSufficiencyStep` rather than the synthetic step `tests/pipeline/test_executor.py`
uses for its own version of that regression.

A `code-review --version` flag (`cli.py`'s eager `--version` callback, reading
`code_review.__version__`) and a version bump (`0.1.0` -> `0.2.0`, both `pyproject.toml`
and `src/code_review/__init__.py`) were added right after, prompted by a real debugging
session where a user's `uv tool install`-managed binary kept behaving like a stale build
after `make install-dev` with no way to confirm which version was actually running --
`code-review --version` now makes that a one-command check instead of grepping installed
site-packages for a known-new symbol.

Keep this line current - it's exactly the kind of fact this file's living-document policy
expects to be edited on every session that moves the project forward.

## Issue tracking

Work is tracked as GitHub issues, one parent per milestone with the tasks as sub-issues,
and `blocked by` relationships for real ordering constraints. Requires `gh` >= 2.94.0 for
the `--parent` / `--blocked-by` flags.

**How issues get written**: a milestone parent is a spec in PRD form, written with
`/to-specs` - problem statement, solution, user stories, implementation decisions, testing
decisions, out of scope. Its sub-issues are broken out with `/to-tickets` and are
**vertical tracer bullets**: each cuts a narrow but complete path through every layer and
is demoable on its own. Never slice a milestone by layer (protocol, then parser, then
adapter) - that shape leaves nothing verifiable until the last piece lands, which is why
the original milestone-1 breakdown was rewritten.

Sub-issues carry the `ready-for-agent` label and follow one template: **Parent**, **What to
build** (end-to-end behaviour, not a layer-by-layer list), **Acceptance criteria** as a
checklist, **Blocked by**. Keep file paths and code snippets out of issue bodies; they go
stale faster than the issue closes.

**One owner per fact**: `docs/ROADMAP.md` owns the design and rationale (durable); issues
own status and sequencing (mutable). Issue bodies link to the roadmap rather than
restating it - don't duplicate, or the two will drift.

## Branch and commit naming

Branches follow `<category>/<issue-number>-<short-description>` (e.g.
`feature/12-agent-retry`, `bugfix/34-timeout-crash`) - lowercase, hyphen-separated, no
author names. Use the `/create-gh-branch` skill to create one; it covers the full category
list and how to link the branch to its GitHub issue via `gh issue develop` so merging the
PR auto-closes the issue (see Issue tracking above).

Commit messages follow Conventional Commits: `<type>(<scope>): <summary>` (e.g.
`feat(agent): survive chatty agent output`), matching this repo's existing history.
`<type>` mirrors the branch category (`feature`→`feat`, `bugfix`/`hotfix`→`fix`, `docs`,
`refactor`, `test`, `chore`).

## Attribute documentation

When declaring fields on a dataclass, Pydantic model, config object, or other structured
record, document each field at its declaration with both its purpose and where or how it
is consumed. Say explicitly when a field is reserved for future work and has no current
consumer; do not make planned behavior sound implemented. Whenever a field's meaning,
consumer, validation, or lifecycle changes, update its declaration comment in the same
change so the documentation continues to describe actual usage.

## Living document policy

All AGENTS.md files are live: any chat session, user correction, or agent correction
should improve the relevant AGENTS.md so memories stay up to date. Where an AGENTS.md
file gets too long — i.e. it contains instructions that are not required for every
prompt — extract that section out and use `/skill-creator` to turn it into a
repo-specific skill instead of letting the file keep growing.

## Nested AGENTS.md convention

Package-scoped `AGENTS.md` + `CLAUDE.md` pairs live under `src/code_review/<package>/`
(`agent/`, `pipeline/`, `steps/`, `prompt/`, `scm/`, `tui/`). `scm/` is still a stub — "no
package-specific invariants yet, see root" — because that package isn't built out yet
(Milestone 8). As a package gains real design decisions, gotchas, or regressions worth
pinning, add that guidance to its own scoped file rather than growing this root file. The
root file stays for repo-wide concerns only.

## Maintaining this file

Keep this file for knowledge useful to almost every future agent session in this project.
Do not repeat what the codebase already shows; point to the authoritative file or command
instead. Prefer rewriting or pruning existing entries over appending new ones. When
updating this file, preserve this bar and keep entries concise.
