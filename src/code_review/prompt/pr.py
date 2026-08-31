"""PR-step prompt construction: assembles the single prompt `PRStep` sends the agent to
draft a pull request title plus its "What Changed" bullets, from module-level guardrail
clauses plus the wrapped intent block. `steps/pr.py` keeps the schema and orchestration.

Guardrail clauses close specific ways a drafted PR body goes wrong:

- `_TITLE_FORMAT_RULE`: Conventional Commits (`<type>(<scope>): <summary>`), imperative,
  lowercase summary, no trailing period -- matching this repo's own commit history.
- `_WHAT_CHANGED_RULE`: a countable budget (<=5 bullets, <=20 words each), headline
  first, about behavior change rather than a file/test/support-module inventory, with a
  worked bad-vs-good example.
- `_DEMONSTRATION_RULE`: countable per-cell budgets (label <=6 words, each value <=10),
  cells that are values rather than prose, values specific enough that a reviewer could not
  have guessed them, and a worked bad-vs-good example.
- `_DEMONSTRATION_GROUNDING_RULE`: appended only when the caller supplies what the pipeline
  actually observed while verifying the change, so demonstrations lean on that rather than
  on what the diff merely suggests. A mitigation, not a guarantee.
- `_GENERATED_SECTIONS_PROHIBITION`: Intent/Risk Assessment/Testing are assembled
  deterministically in code from the pipeline's own state, so anything the agent writes for
  them is a paraphrase that would be rendered twice.
- `_NO_MARKDOWN_HEADINGS_RULE`: the returned fields are raw text; `steps/pr.py` adds the
  headings, bullet markers, tables, and fences.
"""

from __future__ import annotations

from code_review.pipeline.step import StepContext
from code_review.prompt.intent import wrap_intent

# --- Guardrail clause constants ----------------------------------------------------------

_TITLE_FORMAT_RULE = (
    "The title MUST follow Conventional Commits: `<type>(<scope>): <summary>`, where "
    "`<type>` is one of feat, fix, docs, refactor, test, chore, and `<scope>` is the "
    "package or module most affected (omit the parentheses entirely if no single scope "
    'fits). Write the summary in the imperative mood ("add retry backoff", not "added" '
    'or "adds"), all lowercase except for identifiers that are genuinely capitalized, '
    "and with no trailing period. Keep it under 72 characters."
)

# One policy, applying to this rule's bullet budget and to _DEMONSTRATION_RULE's cell
# budgets below: neither is backed by code-level truncation, unlike the title-length and
# heading/marker rules. Cutting a title or stripping a leading marker is always safe;
# chopping a bullet or an evidence cell mid-word corrupts what the reviewer reads and renders
# worse than the long value it replaced -- the same reasoning that made `steps/pr.py` grow a
# code fence past a nested ``` rather than mutate the payload inside it. Both budgets are
# therefore stated in countable terms precisely because they can only ever be asked for.
_WHAT_CHANGED_RULE = (
    "what_changed is a scannable list a reviewer takes in at a glance, not prose. Hard "
    "budget: at most 5 bullets, and at most 20 words per bullet -- count the words and cut "
    "until each one fits. Order them by importance and lead with the single most important "
    "behavior change: the first bullet is the headline, and a reviewer who reads only it "
    "should already know what this change does.\n"
    "Every bullet says what behaves differently now, never how the diff is built. Do NOT "
    "emit a bullet per changed file, a bullet for a module, helper, or constant added in "
    "support of the change, or a bullet for the tests, fixtures, or test doubles it needed "
    "-- those describe how the change was made, not what it does. The one exception is a "
    "change whose entire point IS the tests (e.g. adding coverage for behavior that already "
    "shipped); there, the tests are the behavior change. Name a file or symbol only when "
    "that is the clearest way to say which behavior moved.\n"
    'Bad: "Added `retry.py` with a `backoff_delay` helper plus unit tests covering the new '
    'exponential backoff schedule and its jitter."\n'
    'Good: "Failed uploads now retry with exponential backoff instead of giving up '
    'immediately."'
)

# See the policy comment above _WHAT_CHANGED_RULE: the word budgets below are asked for, not
# enforced by code-level cell truncation.
_DEMONSTRATION_RULE = (
    "demonstrations becomes a table a reviewer takes in at one glance. Return at most 4. "
    "Hard budget per demonstration, counted in words: label at most 6, and given, was and "
    "now at most 10 each -- count them. A demonstration you cannot state inside that budget "
    "must be omitted rather than squeezed in: fewer, sharper demonstrations beat more, "
    "vaguer ones, and an empty list is the right answer when nothing about this change is "
    "crisply demonstrable.\n"
    'label is a short NAME for the behavior on show ("Backoff delay", "Retry ceiling"), '
    "never a sentence and never the claim itself. given, was and now are values, not prose: "
    "a literal value, an identifier, a status code, or a short phrase, wrapped in backticks "
    "when it is a literal. Do NOT append a justification or a citation to a cell, do NOT "
    "restate the label inside now, and do NOT describe what a test asserts -- say what the "
    "system does.\n"
    "Each demonstration carries a kind, a label, and up to three of those values: given (the "
    "stimulus), was (the result before this change, left unset when there was no meaningful "
    'prior result), and now (the result after it). Use kind "api" for a request/response '
    'exchange across an HTTP or RPC boundary, and kind "behavior" for any other observable '
    "difference. Every one must carry at least a given or a now, or it cannot be rendered "
    "and will be discarded.\n"
    "Within that budget, name real, specific values -- status codes, payload fields, "
    "timings, counts, exact error text -- never a summary of them. A demonstration whose "
    "values a reader could have guessed without the code is worse than nothing, because it "
    "occupies the space a real one would have used.\n"
    'Bad (every cell a sentence; the row is unreadable at a glance): label "Uploads that '
    'fail transiently are now retried instead of surfacing the error"; given "an upload '
    'whose first two attempts fail with a transient network error"; now "the upload '
    'eventually succeeds on the third attempt, as pinned by the retry tests".\n'
    'Good (the same demonstration, stated as values): label "Transient upload retry"; '
    'given "`2 failed attempts`"; now "`succeeds on attempt 3`".'
)

_DEMONSTRATION_GROUNDING_RULE = (
    "Ground your demonstrations in what this pipeline actually observed while verifying the "
    "change, listed below, rather than in values that merely look plausible for the diff. "
    "Where the observations do not support a concrete demonstration, return fewer of them; "
    "never fabricate one to fill the space. They are material telling you what is actually "
    "true, not text to quote: never copy an observation into a cell, and never cite a test "
    "name or file location there."
)

_GENERATED_SECTIONS_PROHIBITION = (
    "Do NOT write any Intent, Risk Assessment, or Testing content anywhere in your answer. "
    "Those sections are assembled deterministically in code from the pipeline's own "
    "already-computed state, and anything you write for them would be a paraphrase of that "
    "state rendered a second time. Restrict yourself strictly to the title, the "
    "what_changed bullets, and the demonstrations."
)

_NO_MARKDOWN_HEADINGS_RULE = (
    "Return plain text in every field: no markdown headings (no leading `#`/`##`), no "
    "leading `-`/`*`/`+` bullet markers on the what_changed entries, and no tables, code "
    "fences, or backtick spans in the demonstration fields -- the pull-request body adds "
    "its own headings, bullet markers, tables, and fences around what you return."
)


# --- build_pr_draft_prompt ----------------------------------------------------------------


def build_pr_draft_prompt(ctx: StepContext, *, observed_testing: str | None = None) -> str:
    """Assemble `PRStep`'s drafting prompt: diff, wrapped intent, then the guardrail clauses
    in fixed order -- diff-first so the agent reads the change before what the author said
    it was for.

    `observed_testing` is what the pipeline itself recorded while verifying the change,
    already formatted by the caller. `prompt/` never imports `steps/`, so the caller (not
    this module) reads it off `ctx.step_outcomes`. Omitted entirely when `None`, rather than
    pointing the agent at material that isn't there.
    """

    sections = [
        f'Draft the title, the "What Changed" section, and the demonstrations backing a '
        f"pull request for this diff:\n{ctx.diff}",
        wrap_intent(ctx.intent.summary, ctx.intent.source),
        _TITLE_FORMAT_RULE,
        _WHAT_CHANGED_RULE,
        _DEMONSTRATION_RULE,
    ]

    if observed_testing is not None:
        sections.append(f"{_DEMONSTRATION_GROUNDING_RULE}\n{observed_testing}")

    sections.append(_GENERATED_SECTIONS_PROHIBITION)
    sections.append(_NO_MARKDOWN_HEADINGS_RULE)

    return "\n\n".join(sections)
