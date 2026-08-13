"""Pure unit tests for `steps/tool_activity.py`'s label-rendering helpers.

`tool_stream_relay` itself (the `on_stream_event` callback these helpers feed) is proven
end-to-end against real stream-json transcripts in `tests/steps/test_review.py`/
`tests/steps/test_test_sufficiency.py`, not here -- this file is only about the pure text
formatting `tool_activity_label`/`assistant_text_label` do.
"""

from __future__ import annotations

from code_review.steps.tool_activity import assistant_text_label, tool_activity_label


def test_tool_activity_label_renders_the_primary_argument_when_present() -> None:
    assert tool_activity_label("Read", {"file_path": "/tmp/example.py"}) == (
        "Tool: Read(/tmp/example.py)"
    )


def test_tool_activity_label_falls_back_to_a_bare_name_with_no_recognized_argument() -> None:
    assert tool_activity_label("Glob", {}) == "Tool: Glob"


def test_assistant_text_label_prefixes_with_agent_and_the_first_line() -> None:
    assert assistant_text_label("Let me check the auth module for a missing nil check") == (
        "Agent: Let me check the auth module for a missing nil check"
    )


def test_assistant_text_label_collapses_to_only_the_first_line() -> None:
    content = "Checking the diff first.\nThen I'll look at the tests."
    assert assistant_text_label(content) == "Agent: Checking the diff first."


def test_assistant_text_label_strips_leading_and_trailing_whitespace() -> None:
    assert (
        assistant_text_label("  \n  Reviewing the changes  \n  ") == "Agent: Reviewing the changes"
    )


def test_assistant_text_label_truncates_a_long_first_line() -> None:
    first_line = "x" * 200
    label = assistant_text_label(first_line)

    assert label.startswith("Agent: " + "x" * 159)
    assert label.endswith("…")
    assert len(label) == len("Agent: ") + 160


def test_assistant_text_label_renders_a_placeholder_for_empty_content() -> None:
    assert assistant_text_label("") == "Agent: (no text)"
    assert assistant_text_label("   \n  ") == "Agent: (no text)"
