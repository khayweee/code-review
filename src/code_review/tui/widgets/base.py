"""Shared base class for bordered, auto-height boxes: `PipelineBox` and `StatusBox`.

`FindingBox` needs a `Vertical` instead of a `Static`, so it duplicates this CSS
rule rather than subclassing this.
"""

from __future__ import annotations

from pathlib import Path

from textual.widgets import Static


class _BorderedBox(Static):
    """Shared border/padding rule for `PipelineBox`/`StatusBox`.

    Textual resolves `DEFAULT_CSS` against a widget's whole class hierarchy, so defining
    it here reaches every subclass automatically.
    """

    DEFAULT_CSS = (
        Path(__file__).with_name("tokens.tcss").read_text()
        + "\n"
        + Path(__file__).with_suffix(".tcss").read_text()
    )
