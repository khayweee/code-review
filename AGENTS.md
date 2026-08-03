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
4 sub-issues complete. The interactive approve/fix/skip/abort layer from the reference
screenshot waits on Milestone 7's approval loop, which isn't specced yet, and stays
documented in `docs/ROADMAP.md` rather than sliced into a ticket until then.

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
blocked by #66, independent of #64). All three are open, not yet started. Blocked on a
prerequisite standalone bugfix, #62 (found incidentally while scoping this milestone, same
pattern as #47 under Milestone 5): `gitutils.run_git` currently blocks the asyncio event
loop for the duration of every git call, freezing the Pipeline box's elapsed-duration tick
today.

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
