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
- `src/code_review/steps/` — the actual pipeline steps: intent, review, test_sufficiency, pr.
- `src/code_review/scm/` — SCM host wrapper (GitHub via the `gh` CLI).
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

Milestone 0 (scaffold) complete. Milestone 1 (agent adapter) in progress - tracked as
GitHub issue #2 (the spec) and its sub-issues #3, #4, #6. Issue #3 (the round trip) is
merged to main; #4 (chatty-output tolerance, distinct error types) is implemented on
branch `4-survive-chatty-agent-output-and-name-the-failure-modes`, pending PR/merge; #6
(process-group cleanup on cancellation) is implemented on branch
`feature/6-process-cleanup`, pending PR/merge. Keep this line current - it's exactly the
kind of fact this file's living-document policy expects to be edited on every session
that moves the project forward.

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

## Living document policy

All AGENTS.md files are live: any chat session, user correction, or agent correction
should improve the relevant AGENTS.md so memories stay up to date. Where an AGENTS.md
file gets too long — i.e. it contains instructions that are not required for every
prompt — extract that section out and use `/skill-creator` to turn it into a
repo-specific skill instead of letting the file keep growing.

## Nested AGENTS.md convention

Package-scoped `AGENTS.md` + `CLAUDE.md` pairs live under `src/code_review/<package>/`
(`agent/`, `pipeline/`, `steps/`, `scm/`). Right now they're stubs — each says "no
package-specific invariants yet, see root" — because those packages aren't built out yet.
As a package gains real design decisions, gotchas, or regressions worth pinning, add that
guidance to its own scoped file rather than growing this root file. The root file stays
for repo-wide concerns only.

## Maintaining this file

Keep this file for knowledge useful to almost every future agent session in this project.
Do not repeat what the codebase already shows; point to the authoritative file or command
instead. Prefer rewriting or pruning existing entries over appending new ones. When
updating this file, preserve this bar and keep entries concise.
