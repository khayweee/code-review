"""Design-token consistency checks for `tokens.tcss` (the palette of record) and its two
consumers: every `.tcss` file under `tui/widgets/`, and `styles.py`'s Rich-string color
constants -- see both files' own module docstrings (issue #111). Pure text parsing, no
Textual `App`/`Pilot` needed.
"""

from __future__ import annotations

import re
from pathlib import Path

from code_review.tui.widgets import styles

_WIDGETS_DIR = Path(styles.__file__).parent
_TOKENS_PATH = _WIDGETS_DIR / "tokens.tcss"

_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_VAR_DECLARATION = re.compile(r"\$([\w-]+)\s*:\s*([^;]+);")
_VAR_REFERENCE = re.compile(r"\$([\w-]+)")
_HEX_COLOR = re.compile(r"#[0-9a-fA-F]{3,8}\b")

_EXPECTED_TOKENS = {
    "bg",
    "bg-surface",
    "border-color",
    "border-color-muted",
    "fg",
    "fg-secondary",
    "fg-muted",
    "highlight",
    "status-completed",
    "status-failed",
    "status-parked",
    "status-skipped",
}


def _strip_comments(text: str) -> str:
    return _BLOCK_COMMENT.sub("", text)


def _tokens() -> dict[str, str]:
    return dict(_VAR_DECLARATION.findall(_strip_comments(_TOKENS_PATH.read_text())))


def test_tokens_tcss_defines_the_full_semantic_set_as_real_hex_values() -> None:
    """background/surface, border, text primary/secondary/muted, accent, and the four
    status colors each resolve to a real hex value, not a relative/auto Textual token."""

    tokens = _tokens()
    assert _EXPECTED_TOKENS <= tokens.keys()
    for name, value in tokens.items():
        assert _HEX_COLOR.fullmatch(value), f"${name} is not a real hex value: {value!r}"


def test_no_widget_tcss_file_hardcodes_a_color_or_uses_an_undefined_variable() -> None:
    """No `.tcss` file under `tui/widgets/` other than `tokens.tcss` itself may hardcode a
    hex color, or reference a `$variable` `tokens.tcss` doesn't define -- Textual's own
    builtins (`$primary`, `$text-muted`, ...) aren't defined there, so this also catches
    any reintroduced reliance on them."""

    token_names = _tokens().keys()
    for path in sorted(_WIDGETS_DIR.glob("*.tcss")):
        if path == _TOKENS_PATH:
            continue
        text = _strip_comments(path.read_text())
        assert not _HEX_COLOR.search(text), f"{path.name} hardcodes a hex color"
        for var_name in _VAR_REFERENCE.findall(text):
            assert var_name in token_names, (
                f"{path.name} references ${var_name}, not defined in tokens.tcss"
            )


def test_styles_py_color_constants_match_tokens_tcss_by_value() -> None:
    """Rich style strings can't reference Textual CSS variables at runtime, so `styles.py`
    keeps separate literal hex constants -- every one must match its `tokens.tcss`
    counterpart exactly."""

    token_values = set(_tokens().values())
    assert styles._ACTIVITY_STYLE in token_values
    for value in styles._STATUS_DOT_STYLES.values():
        assert value in token_values
    for value in styles._SEVERITY_DOT_STYLES.values():
        assert value in token_values
    for value in styles._DECISION_MARKER_STYLES.values():
        assert value in token_values
