# AGENTS.md — src/code_review/prompt/

Scope: this package only. See the [root AGENTS.md](../../../AGENTS.md) for repo-wide
conventions.

This package was carved out of `steps/` in a structural refactor (not its own milestone):
a staff-engineer audit found `steps/intent.py` and `steps/review.py` mixing
prompt-construction logic in with step-orchestration logic, so the pure,
string-in/string-out prompt builders moved here and the `steps/` modules kept only their
`Step` subclasses and pydantic schemas.

`prompt/` is a leaf package: it depends on `pipeline/` (for `StepContext`, which
`build_review_prompt` reads) but nothing in `steps/` needs to be imported here, and nothing
here imports from `steps/`. `intent_conformance_clause` deliberately takes `source: str`
rather than the `steps/intent.py` `Intent` object, precisely to keep this package's only
external dependency `pipeline/`, never `steps/`.

## `intent.py`

`wrap_intent`, `redact_secrets`, and `strip_adversarial` (moved from `steps/intent.py`,
Milestone 3): the one sanitize-and-wrap pipeline reused at every prompt site that embeds
intent text. Provenance (`source`) changes only the framing sentence, never whether
sanitization runs -- see the module docstring for the regression this pins against.

## `review.py`

`intent_conformance_clause(source: str)` and `build_review_prompt(ctx)` (moved from
`steps/review.py`, Milestone 5): the intent-conformance obligation clause and the single
prompt assembly used by `ReviewStep`. `build_review_prompt` calls `wrap_intent` from
`intent.py` above and appends the clause only when it is non-empty.

## `test_sufficiency.py`

`build_test_sufficiency_prompt(ctx)` and its guardrail-clause constants (Milestone 6,
issue #59): the decision-ladder text plus the not-sufficient-evidence/complete-suite-
prohibition/test-quality-rule clauses used by `TestSufficiencyStep`. Got its own module
rather than sharing `review.py`, because none of this guardrail text branches on intent
provenance the way `intent_conformance_clause` does -- there is no per-provenance clause
here to keep separate from an always-present one, so folding it into `review.py` would
have bought nothing and made that module's one conditional clause harder to spot among
several unconditional ones.

Once PR's own prompt builder lands (Milestone 8), record here whether it gets its own
module in this package or shares one of the above.
