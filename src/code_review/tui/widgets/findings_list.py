"""The Findings box: the most recently completed step's findings, one row per finding.

- Shows a `ReviewOutput`, `TestSufficiencyOutput`, or bare `list[Finding]` (see
  `state.py`'s `latest_findings`), one `Finding` row per finding plus a severity-count
  summary line.
- While a step is parked, turns the highlighted row's `FindingsSuggestion` into a live
  inline decision selector: each finding's own `suggestions` plus a single "Chat about
  it" entry, always recording "fix" for whichever row is highlighted when confirmed.
- Skip ("s") records "skip" for the highlighted row the same per-row way; the park itself
  only resolves once every row has a decision, aggregating them into one
  `ApprovalResponse`.
- Abort ("x") is the one binding that stays a separate, global, step-scoped control -- it
  stops the whole run outright regardless of how many rows are already decided.
- Takes the data it displays as plain data, never reads a `StepEvent` stream or a
  registry/agent output itself.
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
from code_review.pipeline.step import ApprovalDecision, ApprovalResponse
from code_review.steps.review import ReviewOutput
from code_review.steps.test_sufficiency import TestSufficiencyOutput
from code_review.tui.widgets.finding import Finding
from code_review.tui.widgets.findings_list_view import _FindingsListView
from code_review.tui.widgets.findings_suggestion import _CUSTOM_ENTRY


def _findings_of(
    output: ReviewOutput | TestSufficiencyOutput | list[FindingData],
) -> list[FindingData]:
    """Extract the plain `list[Finding]` from whichever of `ReviewOutput`/
    `TestSufficiencyOutput`/bare `list[Finding]` `state.py`'s `latest_findings` picked --
    the one place `FindingsList`'s helpers need to branch on shape, so nothing downstream
    does."""

    return output if isinstance(output, list) else output.findings


def _findings_summary(output: ReviewOutput | TestSufficiencyOutput | list[FindingData]) -> str:
    """Render `output`'s severity-count summary, e.g. `1 error, 2 warning, 0 info`."""

    counts = {severity: 0 for severity in ("error", "warning", "info")}
    for finding in _findings_of(output):
        counts[finding.severity] += 1
    return f"{counts['error']} error, {counts['warning']} warning, {counts['info']} info"


# `FindingsList._set_footer_hint` appends a live "N/M decided" progress count after this
# fixed copy while parked, recomputed on every recorded decision, not just at park start/end.
_FOOTER_HINT = (
    "Enter to confirm this finding  |  left/right or 1-9 browse options  |  f to chat"
    "  |  s to skip this finding  |  x to abort the run"
)


class FindingsList(Vertical):
    """A bordered box showing the most recently completed step's findings.

    - Hosts a child `_FindingsListView`, which hosts one `Finding` per finding, plus a
      trailing severity-count summary line and a bound-key footer hint.
    - Only the finding currently under the cursor shows anything in its
      `FindingsSuggestion` column; arrow keys move the cursor.
    - While parked, `await_decision` turns the highlighted row's `FindingsSuggestion` into
      a live decision selector; outside a park, the box is read-only.
    - A `Vertical`, not a `_BorderedBox` (`Static`) subclass -- it needs to host three
      children, which a `Static` can't.
    """

    DEFAULT_CSS = Path(__file__).with_suffix(".tcss").read_text()

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
        # Set only for the duration of `await_decision` -- see that method's docstring.
        self._parked = False
        self._pending: asyncio.Future[ApprovalResponse] | None = None
        # The `Finding` row `on_list_view_highlighted` most recently hid -- tracked so the
        # handler can un-highlight it without re-deriving it from `_FindingsListView`'s own
        # (already-advanced) `index`. `None` before anything has ever been highlighted.
        self._last_highlighted: Finding | None = None
        # This box's own authoritative list of `Finding` rows, in order -- `update_findings`
        # reconciles against *this*, never against `_FindingsListView.children` fresh each
        # call. See that method's docstring for why.
        self._rows = [Finding(finding) for finding in _findings_of(output)]

    def compose(self) -> ComposeResult:
        yield _FindingsListView(*self._rows, owner=self)
        yield Static(_findings_summary(self._output), id="findings-summary")
        yield Static("", id="findings-footer", classes="footer-hint")

    def on_mount(self) -> None:
        # Safety net: `_FindingsListView`'s own initial `index=0` may or may not have
        # already posted `Highlighted` by the time this runs -- explicitly prime row 0
        # rather than depend on message-delivery ordering between two widgets.
        self._prime_highlighted()

    def update_findings(
        self, output: ReviewOutput | TestSufficiencyOutput | list[FindingData], step_name: str
    ) -> None:
        """Replace the displayed findings with `output`'s, and update `border_title`.

        Called on every periodic render tick, whether or not `output` actually changed. The
        common case (finding count unchanged) updates every existing row in place, touching
        no `_FindingsListView`-level DOM structure -- so its cursor index and every row's
        own mode survive untouched. Only a growing/shrinking finding count mounts or
        removes rows, and only the ones beyond the overlap with the old list.

        Reconciles against `self._rows` (this box's own authoritative row list), never a
        fresh `_FindingsListView.children` read -- Textual mounts/removes children
        asynchronously, so a live query mid-tick could under/over-count rows still settling
        into the DOM. No-ops the child rebuild (but still updates `border_title`) if
        `_FindingsListView` hasn't composed yet.
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
            # If the row `on_list_view_highlighted` most recently hid is one of the rows
            # just removed, drop the reference now rather than leaving it to that handler
            # to call `.set_hidden()` on an already-unmounted `Finding`.
            if self._last_highlighted in removed:
                self._last_highlighted = None
            if list_view.index is not None and list_view.index >= len(new_findings):
                list_view.index = len(new_findings) - 1 if new_findings else None

        if new_findings and list_view.index is None:
            list_view.index = 0

        summary.update(_findings_summary(output))

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        """Hide the previously-highlighted row's suggestions and show the newly
        highlighted one's -- `plain` outside a park, or `decision` (cursor reset to 0
        first) while parked, since moving to a different finding always starts its own
        decision cycle fresh."""

        if self._last_highlighted is not None:
            self._last_highlighted.set_hidden()
        item = event.item
        self._last_highlighted = item if isinstance(item, Finding) else None
        if self._last_highlighted is None:
            return
        if self._parked:
            self._last_highlighted.reset_decision()
            self._last_highlighted.set_decision()
        else:
            self._last_highlighted.set_plain()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        """Confirm whatever the cursor currently points at for the selected row's finding
        -- a no-op outside a park. A suggestion entry records itself as the fix immediately,
        verbatim, with no edit step -- "Chat about it" is the one entry with no text of its
        own to record, so confirming it opens the inline chat instead."""

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
        item.set_decision() if self._parked else item.set_plain()
        self._last_highlighted = item

    def _list_view(self) -> _FindingsListView | None:
        """This box's `_FindingsListView`, or `None` when it hasn't composed yet. Every
        caller below treats `None` as "nothing to do yet, will settle on its own"."""

        try:
            return self.query_one(_FindingsListView)
        except NoMatches:
            return None

    async def _await_list_view(self) -> _FindingsListView | None:
        """Give `_FindingsListView` a few event-loop turns to exist if it doesn't yet,
        rather than the fire-and-forget guard every synchronous caller uses -- so
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
        # `_FindingsListView.__init__` asserts every mounted child is a `Finding`, and this
        # module never mounts anything else into it.
        return cast("Finding | None", item)

    def _cycle_decision(self, delta: int) -> None:
        """Move the highlighted row's decision cursor by `delta`, then open the inline
        chat the moment it lands on "Chat about it" -- so browsing onto that entry already
        puts the human straight into typing."""

        if not self._parked:
            return
        item = self._highlighted_finding()
        if item is None:
            return
        item.cycle_decision(delta)
        if item.confirmed_entry() == _CUSTOM_ENTRY:
            self._open_chat()

    def _jump_decision(self, index: int) -> None:
        """Digit-key counterpart to `_cycle_decision` -- same "open the chat the instant
        the cursor lands on 'Chat about it'" behavior, checked after the call regardless
        of whether `Finding.jump_decision` itself no-op'd."""

        if not self._parked:
            return
        item = self._highlighted_finding()
        if item is None:
            return
        item.jump_decision(index)
        if item.confirmed_entry() == _CUSTOM_ENTRY:
            self._open_chat()

    def _quick_decision(self, decision: ApprovalDecision) -> None:
        """ "s"/"x"'s shared entry point. "abort" resolves `self._pending` directly and
        immediately, regardless of how many rows are already decided -- a whole-run action
        with no coherent per-finding meaning. Every other value reaching this method is
        "skip", recorded against the highlighted row only."""

        if not self._parked or self._pending is None:
            return
        if decision == "abort":
            self._pending.set_result(ApprovalResponse(decision="abort", instructions=None))
            return
        self._record_decision(decision, None)

    def _chat_prefill(self, item: Finding) -> str:
        """The text to seed a freshly (re)opened chat with: the highlighted row's own
        previously recorded "fix" instructions, if it has any, so browsing away from a
        decided row and back -- then reopening its chat via Enter/"f"/cycling back onto
        "Chat about it" -- shows what was already confirmed instead of an empty box.
        Without this, a stray Enter on a revisited row would resubmit an empty `Input` and
        silently overwrite the real instructions with "" in the aggregated fix prompt
        (`pipeline.findings.describe_finding_decisions`). Empty for an undecided row, or
        one decided "skip" -- there is no fix text to restore."""

        response = item.row_decision
        if response is not None and response.decision == "fix":
            return response.instructions or ""
        return ""

    def _open_chat(self, prefill: str | None = None) -> None:
        """Open the highlighted row's chat, seeded with `prefill`, in place inside that
        row's own `FindingsSuggestion`. Idempotent, so calling this twice in a row -- via a
        cycle/jump auto-open and again via a redundant Enter/"f" -- never stacks or resets
        anything. Focuses the returned `Input`.

        `prefill` defaults to `_chat_prefill`'s own row-decision-aware guess rather than
        always `""`, so every real key-binding call site (Enter/"f"/cycle/jump) gets that
        behavior for free. Tests pass an explicit string (including `""`) to force a
        specific seed regardless of the row's recorded decision."""

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
        """Escape counterpart to `_open_chat`: cancel the highlighted row's live chat
        without resolving `self._pending` -- the park stays open exactly as it was before
        the chat opened. Refocuses `_FindingsListView` explicitly."""

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
        """Handle the highlighted row's live chat `Input` being submitted -- this
        `Message` bubbles up to `FindingsList` regardless of where it lives in the row
        tree. Delegating to `_resolve_chat` is enough on its own; no explicit cleanup
        needed here."""

        self._resolve_chat(event.value)

    def _resolve_chat(self, instructions: str) -> None:
        """The highlighted row's chat `Input` was submitted -- records "fix" for that row
        only, via `_record_decision`."""

        self._record_decision("fix", instructions)

    def _record_decision(self, decision: ApprovalDecision, instructions: str | None) -> None:
        """Record `decision`/`instructions` against the currently highlighted row only.

        - Once every row in `self._rows` has its own decision, aggregates them into the
          one final `ApprovalResponse` and resolves the pending park (`_resolve_park`).
        - Otherwise leaves the park open and moves the highlighted cursor on to the next
          undecided row (`_advance_to_next_undecided`).
        - The footer's decided/total progress count is recomputed either way.
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
        """After a decision is recorded but the park is not yet fully decided: move the
        highlighted cursor to the next undecided row, searching forward from the current
        index and wrapping past the end. Reuses `on_list_view_highlighted`'s existing
        reset-cursor/`set_decision()` plumbing for free by only moving
        `_FindingsListView.index`. Explicitly refocuses `_FindingsListView` afterward."""

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
        """Aggregate every row's now-final decision into the one `ApprovalResponse` that
        resolves `self._pending` -- called once every row in `self._rows` has a decision;
        never called directly by a key binding.

        A single-row park resolves with that row's own `ApprovalResponse`, unwrapped.
        Otherwise every "fix"-decided row's instructions are combined via
        `describe_finding_decisions` into one `decision="fix"` response; if every row chose
        "skip" instead, resolves `decision="skip", instructions=None`.
        """

        assert self._pending is not None
        decided: list[tuple[FindingData, ApprovalResponse]] = []
        for row in self._rows:
            response = row.row_decision
            if response is not None:
                decided.append((row.finding, response))

        if len(self._rows) == 1:
            resolution = decided[0][1]
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
        live "N/M decided" progress count. Called both at park start/end and after every
        single recorded decision, since the count must move the instant a decision is
        recorded."""

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
        until every row in `self._rows` has its own decision and the park resolves.

        Confirming a suggestion or "Chat about it" records "fix" for the highlighted row;
        "s" records "skip"; "x" (abort) resolves the whole run immediately regardless of
        per-row progress. Each recorded decision either resolves the park (once every row
        is decided) or moves the highlighted cursor to the next undecided row via
        `_record_decision`, leaving the park open for the rest.

        Resets every row's decision to undecided at the start, since a fix-round can
        re-park the same `FindingsList` on a fresh round. Awaits `_await_list_view()` (not
        the plain `_list_view()`) so the initial `.focus()` call lands even if this
        coroutine starts before `FindingsList` finishes composing; restores plain display
        in a `finally` regardless of how the park resolved.
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
