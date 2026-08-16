"""The Findings box: the most recently completed step's findings, one row per finding.

Shows a `ReviewOutput`, `TestSufficiencyOutput`, or bare `list[Finding]`, one row per
finding plus a severity-count summary line. While parked, turns the highlighted row's
`FindingsSuggestion` into a live decision selector (suggestions plus "Chat about it");
confirming an entry records "fix" for that row, "s" records "skip". The park resolves
once every row has a decision, aggregating them into one `ApprovalResponse`. Abort ("x")
is a separate global control that stops the run immediately regardless of per-row
progress.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import cast

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.css.query import NoMatches
from textual.widgets import Input, ListView, Static

from code_review.pipeline.findings import Finding as FindingData
from code_review.pipeline.findings import describe_finding_decisions
from code_review.pipeline.schemas import ApprovalDecision, ApprovalResponse, FindingDecision
from code_review.steps.review import ReviewOutput
from code_review.steps.test_sufficiency import TestSufficiencyOutput
from code_review.tui.widgets.Findings.findings_list import Finding, _FindingsListView
from code_review.tui.widgets.Findings.findings_suggestion import _CUSTOM_ENTRY


def _findings_of(
    output: ReviewOutput | TestSufficiencyOutput | list[FindingData],
) -> list[FindingData]:
    """Extract the plain `list[Finding]` regardless of which of the three shapes was
    passed."""

    return output if isinstance(output, list) else output.findings


def _findings_summary(output: ReviewOutput | TestSufficiencyOutput | list[FindingData]) -> str:
    """Render `output`'s severity-count summary, e.g. `1 error, 2 warning, 0 info`."""

    counts = {severity: 0 for severity in ("error", "warning", "info")}
    for finding in _findings_of(output):
        counts[finding.severity] += 1
    return f"{counts['error']} error, {counts['warning']} warning, {counts['info']} info"


# `_set_footer_hint` appends a live "N/M decided" count after this while parked.
_FOOTER_HINT = (
    "Enter to confirm this finding  |  ←/→ browse options  |  f to chat"
    "  |  s to skip this finding  |  x to abort the run"
)


class FindingBox(Vertical):
    """A bordered box showing the most recently completed step's findings.

    Hosts a child `_FindingsListView` (one `Finding` per finding), a severity-count
    summary line, and a bound-key footer hint. Only the highlighted row shows anything in
    its `FindingsSuggestion` column. A `Vertical`, not a `_BorderedBox`, since it needs to
    host three children.
    """

    DEFAULT_CSS = (
        (Path(__file__).parent.parent / "tokens.tcss").read_text()
        + "\n"
        + Path(__file__).with_suffix(".tcss").read_text()
    )

    def __init__(
        self,
        output: ReviewOutput | TestSufficiencyOutput | list[FindingData],
        step_name: str,
        *,
        id: (str | None) = None,  # noqa: A002 -- matches Textual's own Widget.__init__ shape
        classes: str | None = None,
    ) -> None:
        super().__init__(id=id, classes=classes)
        self._output = output
        self.border_title = f"Findings -- {step_name}"
        self._parked = False
        self._pending: asyncio.Future[ApprovalResponse] | None = None
        # Row on_list_view_highlighted most recently hid, so it can be un-highlighted
        # without re-deriving it from the ListView's already-advanced index.
        self._last_highlighted: Finding | None = None
        # Authoritative row list; update_findings reconciles against this, not a fresh
        # _FindingsListView.children read (which may not have settled into the DOM yet).
        self._rows = [Finding(finding) for finding in _findings_of(output)]

    def compose(self) -> ComposeResult:
        yield _FindingsListView(*self._rows, owner=self)
        yield Static(_findings_summary(self._output), id="findings-summary")
        yield Static("", id="findings-footer", classes="footer-hint")

    def on_mount(self) -> None:
        # Explicitly prime row 0 rather than depend on Highlighted message-delivery order.
        self._prime_highlighted()

    def update_findings(
        self, output: ReviewOutput | TestSufficiencyOutput | list[FindingData], step_name: str
    ) -> None:
        """Replace the displayed findings with `output`'s, and update `border_title`.

        Called on every periodic render tick, whether or not `output` changed. The common
        case (finding count unchanged) updates every existing row in place, leaving cursor
        index and row modes untouched. A growing/shrinking count only mounts/removes rows
        beyond the overlap with the old list. No-ops the child rebuild (but still updates
        `border_title`) if `_FindingsListView` hasn't composed yet.
        """

        self._output = output
        self.border_title = f"Findings -- {step_name}"
        try:
            list_view = self.query_one(_FindingsListView)
            summary = self.query_one("#findings-summary", Static)
        except NoMatches:
            return

        new_findings = _findings_of(output)
        overlap = min(len(self._rows), len(new_findings))
        for item, finding in zip(self._rows[:overlap], new_findings[:overlap], strict=True):
            item.update_finding(finding)

        if len(new_findings) > overlap:
            added = [Finding(finding) for finding in new_findings[overlap:]]
            list_view.extend(added)
            self._rows.extend(added)
        elif len(self._rows) > overlap:
            removed = self._rows[overlap:]
            del self._rows[overlap:]
            for item in removed:
                item.remove()
            # Drop the reference now rather than later calling set_hidden() on an
            # already-unmounted Finding.
            if self._last_highlighted in removed:
                self._last_highlighted = None
            if list_view.index is not None and list_view.index >= len(new_findings):
                list_view.index = len(new_findings) - 1 if new_findings else None

        if new_findings and list_view.index is None:
            list_view.index = 0

        summary.update(_findings_summary(output))

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        """Hide the previously-highlighted row's suggestions and show the newly
        highlighted one's -- `plain` outside a park, or `decision` while parked.

        Refocuses `_FindingsListView` at the end of the parked branch: this handler also
        fires when up/down bubbles here from a row's live chat `Input`, and hiding the old
        row tears that focused `Input` down via `set_hidden`. Without refocusing,
        focus is stranded at `None` and no key binding can be reached -- the box reads as
        hung."""

        if self._last_highlighted is not None:
            self._last_highlighted.set_hidden()
        item = event.item
        self._last_highlighted = item if isinstance(item, Finding) else None
        if self._last_highlighted is None:
            return
        if self._parked:
            self._last_highlighted.reset_decision()
            self._last_highlighted.set_decision()
            self._last_highlighted.restore_chat_preview()
            list_view = self._list_view()
            if list_view is not None:
                list_view.focus()
        else:
            self._last_highlighted.set_plain()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        """Confirm whatever the cursor currently points at for the selected row's finding
        -- a no-op outside a park. A suggestion entry records itself as the fix verbatim;
        confirming "Chat about it" opens the inline chat instead."""

        if not self._parked:
            return
        item = event.item
        assert isinstance(item, Finding)
        entry = item.confirmed_entry()
        if entry == _CUSTOM_ENTRY:
            self._open_chat()
        else:
            self._record_decision("fix", entry)

    def _prime_highlighted(self) -> None:
        item = self._highlighted_finding()
        if item is None:
            return
        if self._parked:
            item.set_decision()
            item.restore_chat_preview()
        else:
            item.set_plain()
        self._last_highlighted = item

    def _list_view(self) -> _FindingsListView | None:
        """This box's `_FindingsListView`, or `None` when it hasn't composed yet."""

        try:
            return self.query_one(_FindingsListView)
        except NoMatches:
            return None

    async def _await_list_view(self) -> _FindingsListView | None:
        """Give `_FindingsListView` a few event-loop turns to exist if it doesn't yet, so
        `await_decision`'s `.focus()` call actually lands. Bounded: gives up and returns
        `None` rather than hang the whole park if it never turns up."""

        for _ in range(10):
            list_view = self._list_view()
            if list_view is not None:
                return list_view
            await asyncio.sleep(0)
        return None

    def _highlighted_finding(self) -> Finding | None:
        list_view = self._list_view()
        if list_view is None:
            return None
        item = list_view.highlighted_child
        # _FindingsListView.__init__ asserts every mounted child is a Finding.
        return cast("Finding | None", item)

    def _cycle_decision(self, delta: int) -> None:
        """Move the highlighted row's decision cursor by `delta`, then open the inline
        chat if it lands on "Chat about it"."""

        if not self._parked:
            return
        item = self._highlighted_finding()
        if item is None:
            return
        item.cycle_decision(delta)
        if item.confirmed_entry() == _CUSTOM_ENTRY:
            self._open_chat()

    def _jump_decision(self, index: int) -> None:
        """Digit-key counterpart to `_cycle_decision`: same auto-open-chat behavior."""

        if not self._parked:
            return
        item = self._highlighted_finding()
        if item is None:
            return
        item.jump_decision(index)
        if item.confirmed_entry() == _CUSTOM_ENTRY:
            self._open_chat()

    def _quick_decision(self, decision: ApprovalDecision) -> None:
        """ "s"/"x"'s shared entry point. "abort" resolves `self._pending` immediately
        regardless of per-row progress; any other value is "skip", recorded against the
        highlighted row only."""

        if not self._parked or self._pending is None:
            return
        if decision == "abort":
            self._pending.set_result(ApprovalResponse(decision="abort", instructions=None))
            return
        self._record_decision(decision, None)

    def _chat_prefill(self, item: Finding) -> str:
        """The text to seed a freshly (re)opened chat with: the row's previously recorded
        "fix" instructions, if any. Without this, a stray Enter on a revisited row would
        resubmit an empty `Input` and silently overwrite the real instructions with "" in
        the aggregated fix prompt. Empty for an undecided or "skip"-decided row."""

        response = item.row_decision
        if response is not None and response.decision == "fix":
            return response.instructions or ""
        return ""

    def _open_chat(self, prefill: str | None = None) -> None:
        """Open the highlighted row's chat, seeded with `prefill`, and focus the resulting
        `Input`. Idempotent. `prefill` defaults to `_chat_prefill`'s row-decision-aware
        guess; tests pass an explicit string to force a specific seed."""

        if not self._parked:
            return
        item = self._highlighted_finding()
        if item is None:
            return
        if prefill is None:
            prefill = self._chat_prefill(item)
        input_widget = item.open_chat(prefill)
        if input_widget is not None:
            input_widget.focus()

    def _close_chat(self) -> None:
        """Cancel the highlighted row's live chat without resolving `self._pending`, and
        refocus `_FindingsListView`."""

        if not self._parked:
            return
        item = self._highlighted_finding()
        if item is None:
            return
        item.close_chat()
        list_view = self._list_view()
        if list_view is not None:
            list_view.focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Handle the highlighted row's live chat `Input` being submitted."""

        self._resolve_chat(event.value)

    def _resolve_chat(self, instructions: str) -> None:
        """Records "fix" for the highlighted row only."""

        self._record_decision("fix", instructions)

    def _record_decision(self, decision: ApprovalDecision, instructions: str | None) -> None:
        """Record `decision`/`instructions` against the currently highlighted row only.

        Once every row has a decision, aggregates them and resolves the pending park
        (`_resolve_park`); otherwise moves the highlighted cursor to the next undecided
        row (`_advance_to_next_undecided`). The footer's progress count is recomputed
        either way.
        """

        if self._pending is None:
            return
        item = self._highlighted_finding()
        if item is None:
            return
        item.record_decision(ApprovalResponse(decision=decision, instructions=instructions))
        self._set_footer_hint(True)
        if all(row.is_decided() for row in self._rows):
            self._resolve_park()
        else:
            self._advance_to_next_undecided()

    def _advance_to_next_undecided(self) -> None:
        """Move the highlighted cursor to the next undecided row, searching forward from
        the current index and wrapping past the end, then refocus `_FindingsListView`."""

        list_view = self._list_view()
        if list_view is None or not self._rows:
            return
        current = list_view.index if list_view.index is not None else 0
        total = len(self._rows)
        for offset in range(1, total + 1):
            candidate = (current + offset) % total
            if not self._rows[candidate].is_decided():
                list_view.index = candidate
                list_view.focus()
                return

    def _resolve_park(self) -> None:
        """Aggregate every row's final decision into the one `ApprovalResponse` that
        resolves `self._pending`. Builds one `FindingDecision(finding, response)` per
        decided row -- pairing the finding itself with the human's `ApprovalResponse`
        against it, instead of a bare positional tuple, so `describe_finding_decisions`
        reads as `decision.finding`/`decision.response` rather than `decision[0]`/
        `decision[1]`. A single-row park resolves with that row's own `response`,
        unwrapped; otherwise every "fix"-decided row's instructions are combined via
        `describe_finding_decisions`, or if every row chose "skip", resolves
        `decision="skip"`.

        Example, 3 rows all confirmed with a suggestion (each row's own
        `FindingDecision.response` was `decision="fix", instructions="<picked suggestion
        text>"`):

            ApprovalResponse(
                decision="fix",
                instructions=(
                    "- [error] Off-by-one in pagination (pages.py:42): "
                    "Change range(offset, limit) to range(offset, limit+1)\\n"
                    "- [warning] Missing null check on user.email (users.py:18): "
                    "Return 400 with a validation message\\n"
                    "- [info] Inconsistent naming: userId vs user_id (models.py:9): "
                    "Rename to user_id for consistency"
                ),
            )

        One `ApprovalResponse` for the whole park, never one per row -- a "skip"-decided
        row contributes no line to `instructions` (see `describe_finding_decisions`), and
        if every row skipped, `combined` is `""` and this falls back to
        `ApprovalResponse(decision="skip", instructions=None)` instead of an
        empty-string `instructions="fix"`.
        """

        assert self._pending is not None
        decided: list[FindingDecision] = []
        for row in self._rows:
            response = row.row_decision
            if response is not None:
                decided.append(FindingDecision(finding=row.finding, response=response))

        if len(self._rows) == 1:
            resolution = decided[0].response
        else:
            combined = describe_finding_decisions(decided)
            resolution = (
                ApprovalResponse(decision="fix", instructions=combined)
                if combined
                else ApprovalResponse(decision="skip", instructions=None)
            )
        self._pending.set_result(resolution)

    def _set_footer_hint(self, parked: bool) -> None:
        """Show/clear `#findings-footer`'s bound-key copy. While parked, also appends a
        live "N/M decided" progress count."""

        try:
            footer = self.query_one("#findings-footer", Static)
        except NoMatches:
            return
        if not parked:
            footer.update("")
            return
        decided = sum(1 for row in self._rows if row.is_decided())
        footer.update(f"{_FOOTER_HINT}  |  {decided}/{len(self._rows)} decided")

    async def await_decision(self) -> ApprovalResponse:
        """Turn the highlighted row's `FindingsSuggestion` into a live decision selector
        until every row has its own decision and the park resolves.

        Confirming a suggestion or "Chat about it" records "fix"; "s" records "skip"; "x"
        (abort) resolves the whole run immediately regardless of per-row progress. Resets
        every row's decision to undecided at the start, since a fix-round can re-park the
        same `FindingBox`. Awaits `_await_list_view()` rather than the plain
        `_list_view()` so the initial `.focus()` lands even if this coroutine starts
        before `FindingBox` finishes composing; restores plain display in a `finally`
        regardless of how the park resolved.
        """

        self._parked = True
        for row in self._rows:
            row.clear_decision()
        list_view = await self._await_list_view()
        self._set_footer_hint(True)
        item = self._highlighted_finding()
        if item is not None:
            item.reset_decision()
            item.set_decision()
            item.restore_chat_preview()
            self._last_highlighted = item
        if list_view is not None:
            list_view.focus()
        self._pending = asyncio.get_running_loop().create_future()
        try:
            return await self._pending
        finally:
            self._parked = False
            self._pending = None
            if self._last_highlighted is not None:
                self._last_highlighted.set_plain()
            self._set_footer_hint(False)
