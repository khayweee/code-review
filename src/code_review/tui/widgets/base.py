"""Shared base class for this app's bordered, auto-height boxes.

- `_BorderedBox` factors out the one `DEFAULT_CSS` rule every top-level box shares.
- Used by `PipelineBox` and `StatusBox` (both `Static` subclasses).
- `FindingsList` needs a `Vertical`, so it duplicates this rule instead of extending it.
"""

from __future__ import annotations

from pathlib import Path

from textual.widgets import Static


class _BorderedBox(Static):
    """Shared border/padding rule for `PipelineBox`/`StatusBox`.

    - Textual resolves `DEFAULT_CSS` against a widget's whole class hierarchy, so defining
      the rule once here, keyed to this base class's own name, reaches every subclass.
    - Styled from `base.tcss`, loaded next to this module.
    """

    DEFAULT_CSS = (
        Path(__file__).with_name("tokens.tcss").read_text()
        + "\n"
        + Path(__file__).with_suffix(".tcss").read_text()
    )
