"""Tests for the PR step's prompt construction (`build_pr_draft_prompt`).

Pure function tests -- no agent, no subprocess. Clause presence is asserted against the
module's own constants rather than against copied wording, so rewording a guardrail clause
doesn't break these tests while dropping one still does.
"""

from __future__ import annotations

import re
from pathlib import Path

from code_review.pipeline.step import StepContext
from code_review.prompt.pr import (
    _DEMONSTRATION_GROUNDING_RULE,
    _DEMONSTRATION_RULE,
    _GENERATED_SECTIONS_PROHIBITION,
    _NO_MARKDOWN_HEADINGS_RULE,
    _TITLE_FORMAT_RULE,
    _WHAT_CHANGED_RULE,
    build_pr_draft_prompt,
)
from code_review.steps.intent import Intent

_EXPLICIT_INTENT = Intent(summary="use a queue, not polling", source="explicit", score=1.0)
_INFERRED_INTENT = Intent(summary="use a queue, not polling", source="claude", score=0.4)


class _SpyAgent:
    async def run(self, opts: object) -> object:  # pragma: no cover - not exercised
        raise AssertionError("build_pr_draft_prompt must not call the agent")

    async def close(self) -> None:  # pragma: no cover - not exercised
        pass


def _ctx(intent: Intent, diff: str = "diff --git a/f b/f\n+hello\n") -> StepContext:
    return StepContext(  # type: ignore[arg-type]
        cwd=Path("."),
        branch="unused-placeholder",
        agent=_SpyAgent(),
        diff=diff,
        intent=intent,
    )


def test_build_pr_draft_prompt_includes_the_diff() -> None:
    prompt = build_pr_draft_prompt(_ctx(_EXPLICIT_INTENT))

    assert "diff --git a/f b/f\n+hello\n" in prompt


def test_build_pr_draft_prompt_puts_the_diff_before_the_intent() -> None:
    prompt = build_pr_draft_prompt(_ctx(_EXPLICIT_INTENT))

    assert prompt.index("+hello") < prompt.index("-----BEGIN USER INTENT-----")


def test_build_pr_draft_prompt_wraps_the_intent_rather_than_inlining_it() -> None:
    prompt = build_pr_draft_prompt(_ctx(_EXPLICIT_INTENT))

    assert "-----BEGIN USER INTENT-----\nuse a queue, not polling\n-----END USER INTENT-----" in (
        prompt
    )


def test_build_pr_draft_prompt_frames_inferred_intent_differently_from_explicit() -> None:
    """`wrap_intent`'s provenance framing must reach this prompt site too -- the PR step
    calls it itself off `ctx.intent`, so a source change has to show up here."""

    explicit = build_pr_draft_prompt(_ctx(_EXPLICIT_INTENT))
    inferred = build_pr_draft_prompt(_ctx(_INFERRED_INTENT))

    assert explicit != inferred


def test_build_pr_draft_prompt_carries_every_guardrail_clause() -> None:
    prompt = build_pr_draft_prompt(_ctx(_EXPLICIT_INTENT))

    for clause in (
        _TITLE_FORMAT_RULE,
        _WHAT_CHANGED_RULE,
        _DEMONSTRATION_RULE,
        _GENERATED_SECTIONS_PROHIBITION,
        _NO_MARKDOWN_HEADINGS_RULE,
    ):
        assert clause in prompt


def test_pr_draft_guardrails_forbid_the_deterministically_generated_sections() -> None:
    """The agent's copy of Intent/Risk/Testing would be a paraphrase of state `steps/pr.py`
    already renders itself, so the prompt has to name all three as off-limits."""

    for section in ("Intent", "Risk Assessment", "Testing"):
        assert section in _GENERATED_SECTIONS_PROHIBITION


# --- _WHAT_CHANGED_RULE ---------------------------------------------------------------------

# The rule has no code-level enforcement behind it (see its own comment in `prompt/pr.py`),
# so these assertions are the only thing pinning the properties a real `claude` run was
# observed to violate: unbounded bullet length and a file/test-inventory bullet.


def _worked_example_line(prefix: str) -> str:
    """The `Bad:`/`Good:` demonstration bullet from `_WHAT_CHANGED_RULE`, unquoted."""

    line = next(
        candidate
        for candidate in _WHAT_CHANGED_RULE.splitlines()
        if candidate.startswith(f"{prefix}:")
    )
    return line[len(prefix) + 1 :].strip().strip('"')


def test_what_changed_rule_states_a_countable_bullet_and_word_budget() -> None:
    """The earlier "aim for 3 to 6 bullets"/"one sentence per bullet" phrasing gave a model
    nothing it could hold to -- a 40-word sentence satisfied both. A count is checkable
    while writing."""

    assert "5 bullets" in _WHAT_CHANGED_RULE
    assert "20 words" in _WHAT_CHANGED_RULE


def test_what_changed_rule_asks_for_the_most_important_change_first() -> None:
    assert "headline" in _WHAT_CHANGED_RULE


def test_what_changed_rule_rules_out_support_scaffolding_bullets_not_just_per_file_ones() -> None:
    """A bullet about the tests/fixtures/module added *in support of* a change is the same
    inventory defect as one bullet per changed file, and was what slipped through."""

    for scaffolding in ("tests", "fixtures", "test doubles", "helper"):
        assert scaffolding in _WHAT_CHANGED_RULE


def test_what_changed_rule_keeps_an_exception_for_a_change_that_is_itself_the_tests() -> None:
    assert "IS the tests" in _WHAT_CHANGED_RULE


def test_what_changed_rule_carries_a_worked_bad_versus_good_example() -> None:
    """Prohibitions alone gave the model nothing to pattern-match against; the contrast pair
    is what it can imitate."""

    assert _worked_example_line("Bad")
    assert _worked_example_line("Good")


def test_what_changed_rules_own_good_example_obeys_its_own_word_budget() -> None:
    """An example that breaks the rule it illustrates is worse than no example."""

    assert len(_worked_example_line("Good").split()) <= 20


def test_what_changed_rules_bad_example_is_bad_in_shape_not_merely_in_length() -> None:
    """The bad bullet is inside the word budget on purpose: it isolates the
    file/test-inventory defect, so the contrast can't be misread as being about length."""

    bad = _worked_example_line("Bad")
    assert len(bad.split()) <= 20
    assert "tests" in bad


def test_what_changed_rules_worked_example_uses_an_unrelated_made_up_change() -> None:
    """The example must not describe this repo's own PR-drafting change, or it would bias
    the draft toward whatever diff the agent happens to be reading."""

    example = f"{_worked_example_line('Bad')} {_worked_example_line('Good')}".lower()
    for repo_vocabulary in ("pull request", "what_changed", "prstep", "intent", "diff"):
        assert repo_vocabulary not in example


# --- _DEMONSTRATION_RULE and grounding ---------------------------------------------------------


def _rule_line(rule: str, prefix: str) -> str:
    return next(line for line in rule.splitlines() if line.startswith(prefix))


def test_demonstration_rule_caps_how_many_demonstrations_may_come_back() -> None:
    """Same lesson as the what_changed budget: a countable limit is one a model can hold
    to."""

    assert "at most 4" in _DEMONSTRATION_RULE


def test_demonstration_rule_budgets_every_cell_by_a_word_count() -> None:
    """A real `claude` run produced rows whose every cell was a 35-word sentence: the rule
    demanded concrete values but never said how much room they had. Same defect, and same
    fix, as the earlier what_changed budget."""

    assert "label at most 6" in _DEMONSTRATION_RULE
    assert "now at most 10 each" in _DEMONSTRATION_RULE


def test_demonstration_rule_says_a_label_is_a_name_not_a_claim() -> None:
    """A label that is itself the claim comes back sentence-shaped, which is what blew the
    table's first column out."""

    assert "short NAME for the behavior" in _DEMONSTRATION_RULE
    assert "never a sentence and never the claim itself" in _DEMONSTRATION_RULE


def test_demonstration_rule_forbids_the_specific_shapes_that_turn_a_cell_into_prose() -> None:
    """Each prohibition here is a shape observed in the real run: a trailing "-- pinned by
    tests/..." citation, a `now` that restates its own label, and a cell describing what a
    test asserts instead of what the system does."""

    assert "Do NOT append a justification or a citation to a cell" in _DEMONSTRATION_RULE
    assert "do NOT restate the label inside now" in _DEMONSTRATION_RULE
    assert "do NOT describe what a test asserts" in _DEMONSTRATION_RULE


def test_demonstration_rule_says_to_omit_rather_than_squeeze_in() -> None:
    """Without this, a budget just compresses a bad demonstration into a cryptic one."""

    assert "omitted rather than squeezed in" in _DEMONSTRATION_RULE
    assert "empty list is the right answer" in _DEMONSTRATION_RULE


def test_demonstration_rule_demands_specific_checkable_values() -> None:
    """A demonstration reading "returns an error" backs nothing; the point is the values."""

    assert "status codes" in _DEMONSTRATION_RULE
    assert "never a summary of them" in _DEMONSTRATION_RULE


def _demonstration_example_fields(prefix: str) -> dict[str, str]:
    """The `label`/`given`/`was`/`now` values out of `_DEMONSTRATION_RULE`'s worked example
    line, so a test can measure them the way the rule asks the agent to."""

    line = _rule_line(_DEMONSTRATION_RULE, prefix)
    return dict(re.findall(r'(label|given|was|now) "([^"]*)"', line))


def test_demonstration_rules_good_example_obeys_its_own_word_budgets() -> None:
    """An example that breaks the budget it illustrates is worse than no example."""

    fields = _demonstration_example_fields("Good")

    assert len(fields["label"].split()) <= 6
    assert all(len(value.split()) <= 10 for name, value in fields.items() if name != "label")


def test_demonstration_rules_bad_example_fails_on_length_and_prose_shape() -> None:
    """The contrast has to isolate *this* defect: the bad row is wrong because every cell is
    an over-budget sentence, not because its values are vague or invented."""

    fields = _demonstration_example_fields("Bad")

    assert len(fields["label"].split()) > 6
    assert all(len(value.split()) > 10 for name, value in fields.items() if name != "label")


def test_demonstration_rules_bad_and_good_examples_show_the_same_demonstration() -> None:
    """A rewrite, not a swap for an unrelated demonstration -- otherwise the reader cannot
    see what tightening looks like."""

    bad = _rule_line(_DEMONSTRATION_RULE, "Bad").lower()
    good = _rule_line(_DEMONSTRATION_RULE, "Good").lower()

    assert "upload" in bad and "upload" in good
    assert "retr" in bad and "retr" in good


def test_demonstration_rules_worked_example_uses_an_unrelated_made_up_change() -> None:
    """As with the what_changed example: it must not bias the draft toward the diff the
    agent happens to be reading."""

    example = (
        f"{_rule_line(_DEMONSTRATION_RULE, 'Bad')} {_rule_line(_DEMONSTRATION_RULE, 'Good')}"
    ).lower()
    for repo_vocabulary in ("pull request", "what_changed", "prstep", "intent", "diff"):
        assert repo_vocabulary not in example


def test_demonstration_rule_names_the_minimum_a_renderable_demonstration_needs() -> None:
    """`steps/pr.py` discards a demonstration with neither `given` nor `now`; the prompt says
    so too, so the agent isn't silently losing work."""

    assert "at least a given or a now" in _DEMONSTRATION_RULE


def test_grounding_material_is_absent_when_the_caller_supplies_none() -> None:
    """No `TestSufficiencyStep` outcome means no grounding block, rather than pointing the
    agent at material that isn't there."""

    prompt = build_pr_draft_prompt(_ctx(_EXPLICIT_INTENT))

    assert _DEMONSTRATION_GROUNDING_RULE not in prompt


def test_grounding_material_is_appended_under_its_instruction_when_supplied() -> None:
    prompt = build_pr_draft_prompt(
        _ctx(_EXPLICIT_INTENT),
        observed_testing="- verified behavior: retries stop after 5 attempts",
    )

    assert _DEMONSTRATION_GROUNDING_RULE in prompt
    assert (
        f"{_DEMONSTRATION_GROUNDING_RULE}\n- verified behavior: retries stop after 5 attempts"
        in prompt
    )


def test_grounding_rule_asks_for_fewer_demonstrations_rather_than_invented_ones() -> None:
    """The mitigation only works if "I have nothing to show" is an allowed answer."""

    assert "never fabricate" in _DEMONSTRATION_GROUNDING_RULE.lower()


def test_grounding_rule_forbids_quoting_the_observations_into_a_cell() -> None:
    """The observations carry test names and file locations; the real run copied one into a
    cell as a trailing citation. They are material, not text to reproduce."""

    assert "not text to quote" in _DEMONSTRATION_GROUNDING_RULE
    assert "never cite a test name or file location" in _DEMONSTRATION_GROUNDING_RULE


def test_generated_sections_prohibition_still_allows_the_demonstrations() -> None:
    """The prohibition ends by naming what the agent *may* return -- demonstrations had to
    join that list, or it would read as forbidding them."""

    assert "demonstrations" in _GENERATED_SECTIONS_PROHIBITION
