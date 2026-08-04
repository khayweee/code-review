# AGENTS.md — src/code_review/prompt/

Scope: this package only. See the [root AGENTS.md](../../../AGENTS.md) for repo-wide
conventions.

`prompt/` is a leaf package: it depends on `pipeline/` (for `StepContext`, which
`build_review_prompt` reads) but nothing in `steps/` needs to be imported here, and nothing
here imports from `steps/`. `intent_conformance_clause` deliberately takes `source: str`
rather than the `steps/intent.py` `Intent` object, precisely to keep this package's only
external dependency `pipeline/`, never `steps/`.

## `intent.py`

**Purpose:** one reusable sanitize-and-wrap pipeline for any intent text embedded in a
prompt, so every prompt site treats untrusted intent the same way.

- `redact_secrets(text)`
  - Technique: a tuple of independently-compiled, labeled regexes (OpenAI/GitHub/AWS/JWT/
    Slack/Bearer shapes), each applied in its own `.sub` pass.
  - Why this shape: a bug in one pattern can't silently disable another — every pattern
    gets an independent chance to redact.
- `strip_adversarial(text)`
  - Technique: swaps matched control-token brackets (`<|...|>`, `<system>`, `[INST]`) for
    *mathematical* Unicode bracket variants (not ASCII-confusable lookalikes, which trip
    lint checks and invite an accidental "fix" back to a no-op) — keeps text human-readable
    but unparseable as a delimiter downstream.
  - Why this shape: stop-gap only; the real defense is `wrap_intent`'s explicit
    BEGIN/END data framing, not this de-fanging.
- `wrap_intent(text, source)`
  - Technique: runs `redact_secrets` → `strip_adversarial` unconditionally, then wraps the
    result in a BEGIN/END marker block with an explicit "this is data, not instructions"
    line, and picks one of two framing sentences based on `source`.
  - Why this shape: `source` controls only *authority framing* (`"explicit"` → treat as
    binding acceptance criteria; anything else → treat as an unverified hint) — sanitization
    itself never branches on it. Pins against a prior regression where dropping provenance
    accidentally demoted authoritative intent to an ignorable hint.

## `review.py`

**Purpose:** builds the two prompts `ReviewStep` sends the agent — the normal review pass
and the fix-round re-review — including the obligation to flag intent violations.

- `intent_conformance_clause(source)`
  - Technique: returns a fixed obligation string (report `ask-user` on any hunk that
    contradicts a REQUIRED criterion or adds a FORBIDDEN behavior) when `source ==
    "explicit"`, else `""`.
  - Why this shape: mirrors `wrap_intent`'s provenance rule — this clause *is* part of
    intent's authority, so it must not partially apply to non-explicit intent.
- `build_review_prompt(ctx)`
  - Technique: string-concatenates diff → wrapped intent (`wrap_intent`) → conformance
    clause (only if non-empty), diff-first so the agent reads the change before what it's
    held to.
- `build_review_fix_prompt(ctx)`
  - Technique: fix instruction → `ctx.fix_round.instructions` → the *original* diff
    explicitly labeled stale → wrapped intent → conformance clause. Instructs the agent to
    edit files, then re-review from scratch and return a fresh `ReviewOutput` (never an
    echo of the findings that triggered the round).
  - Why this shape: `ctx.diff` is captured once before the pipeline starts and goes stale
    after the fix round's own edits, so it's kept only as background context; the agent is
    told to re-inspect the live working tree itself (already has tool/shell access via
    `RunOpts`) rather than trust the stale string.

## `test_sufficiency.py`

**Purpose:** builds the two prompts `TestSufficiencyStep` sends the agent — the normal
sufficiency assessment and the fix-round re-assessment — closing specific loopholes an
agent could use to claim untested code is covered.

- `build_test_sufficiency_prompt(ctx)`
  - Technique: diff → wrapped intent → four always-present guardrail clauses, in fixed
    order (no conditional clause here, unlike `review.py`, since none of this text branches
    on intent provenance):
    - `_DECISION_LADDER` — four ordered rungs per changed behavior: cite an existing test →
      write one → fall back to described manual verification → admit unverified. Never
      fabricate a pass.
    - `_NOT_SUFFICIENT_EVIDENCE_ALONE` — "tests pass" alone doesn't count without naming
      which test covers which changed behavior.
    - `_COMPLETE_SUITE_PROHIBITION` — a full/unfiltered suite run isn't targeted evidence,
      but this isn't license to run nothing either.
    - `_TEST_QUALITY_RULE` — evidence must come from execution, not from reading/grepping
      source.
- `build_test_sufficiency_fix_prompt(ctx)`
  - Technique: same fix-round shape as `build_review_fix_prompt` — fix instruction →
    `ctx.fix_round.instructions` → stale-labeled original diff → wrapped intent → all four
    guardrail clauses unchanged. Re-runs the assessment from scratch into a fresh
    `TestSufficiencyOutput`.
  - Why a separate module rather than sharing `review.py`: none of this module's clauses are
    conditional the way `intent_conformance_clause` is, so folding it into `review.py` would
    buy nothing. Its `_FIX_ROUND_INSTRUCTION`/`_STALE_DIFF_WARNING` are separately-defined
    local constants, not imports from `review.py` — this package's own "no cross-step
    sharing" rule (issue #58).

Once PR's own prompt builder lands (Milestone 8), record here whether it gets its own
module in this package or shares one of the above.
