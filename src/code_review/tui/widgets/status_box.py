"""The Status box: a one-line outcome shown once the pipeline run finishes.

- Mounted dynamically, only once the run is done -- a still-running pipeline shows no
  Status box at all.
- Driven by `app.py`'s `_render_status`/`state.py`'s `final_status_message`.
"""

from __future__ import annotations

from code_review.tui.widgets.base import _BorderedBox


class StatusBox(_BorderedBox):
    """A bordered box shown once the pipeline run finishes, successfully or not.

    - Displays a one-line outcome plus the reminder that "e" now closes the app.
    """

    def __init__(
        self,
        message: str,
        *,
        id: (str | None) = None,  # noqa: A002 -- matches Textual's own Widget.__init__ shape
        classes: str | None = None,
    ) -> None:
        super().__init__(message, id=id, classes=classes)
        self.border_title = "Status"

    def update_status(self, message: str) -> None:
        """Replace the displayed status message with `message`."""

        self.update(message)
