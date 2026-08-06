# AGENTS.md — src/code_review/tui/

Scope: this package only. See the [root AGENTS.md](../../../AGENTS.md) for repo-wide
conventions.

Milestone 13's #40 landed this package: a live pipeline-progress view built on
[Textual](https://textual.textualize.io/), wired into `cli.py review`.

## The `events: AsyncIterator[StepEvent]` injection seam

`ReviewApp.__init__(registry, events)` takes the event stream as a constructor argument
rather than importing `pipeline.executor.run_steps`/`steps.registry` and building it
itself. `cli.py` passes `run_steps(steps, ctx)` as `events`; every test in `tests/tui/`
passes a hand-written async generator that yields `StepEvent`s directly, with no
`StepContext`, `Agent`, or subprocess involved. This is what makes `ReviewApp` testable
with Textual's `Pilot`/`run_test()` "independent of a real agent subprocess" (the issue's
own acceptance criterion) — don't collapse this seam by having `ReviewApp` build its own
event stream, even once more real steps land.

## `state.py` must stay Textual-import-free

`state.py`'s `backfill` turns `StepEvent`s into `StepRow`s with zero Textual dependency, so
`tests/tui/test_state.py` can pin its semantics as plain unit tests, independent of an
`App`/`Pilot`. If a future change to `backfill` needs anything from Textual, that is a sign
the function has grown beyond "pure data transform" and the new behavior belongs in
`app.py` or `widgets.py` instead.

## "Failed" is derived, never reported

`pipeline.step.StepEvent.status` only distinguishes `"running"`/`"completed"` — a step that
raises never gets a "completed" event, so nothing in `pipeline/` ever says "failed" (see
`pipeline/step.py`'s own docstring). `ReviewApp` derives it: it tracks `_running_step`, the
name of whichever step was last seen `"running"` with no matching `"completed"` yet, and
copies that into `self._failed_step` in the `except` branch of `_consume_events`. Every
`_rows()` call from then on (the timer tick, and the final render in `finally`) reads
`self._failed_step`, not a per-call parameter — since the app now stays alive and keeps
rendering after the run ends (see below), the failed step must stay marked failed for the
rest of the app's lifetime, not just in one render right before an exit that no longer
happens automatically. Do not add a "failed" status to `StepEvent` itself to "simplify"
this — that would touch `pipeline/step.py`'s already-shipped (#39) contract for a purely
presentational concern.

## The app doesn't exit itself once the run ends — the Status box and "e"

`ReviewApp` used to call `self.exit()` unconditionally in `_consume_events`'s `finally`
the instant `events` was exhausted or raised. That was wrong in practice: today's pipeline
is a single near-instant `IntentStep` (Rebase/Review/Test sufficiency/PR aren't wired into
`IMPLEMENTED_STEPS` yet), so a real run flashed onto the terminal's alternate screen buffer
and vanished in well under a second — indistinguishable, to a human, from nothing having
happened at all (see `docs/ROADMAP.md`/this issue's own "stall that looks alive" framing,
just the opposite failure mode: a *flash* that looks like nothing).

Instead: the run's end (`self._done = True`) stops the tick timer
(`self._tick_timer.stop()`) and mounts a Status box (`widgets.StatusBox`, driven by
`state.final_status_message(self.error)`) naming the outcome — `"Pipeline ran
successfully."` or `"Pipeline failed: <error>."` — plus a reminder that pressing "e" now
closes the app. `action_exit_when_done` (bound to `BINDINGS = [("e", "exit_when_done",
...)]`) is a no-op until `self._done`, so a stray "e" during a real run can't cut it short
— only Textual's own default `ctrl+q`/`ctrl+c` bindings can abort mid-run. This applies
identically on failure, not just success: seeing the broken step and the error message is
at least as important as a clean exit, so the failure path was not left auto-exiting while
only success waits for a keypress.

`_render_status` mirrors `_render_findings`'s exact mount/update/remove pattern: the Status
box does not exist at all until `self._done`, matching `FindingsList`'s own "no box, not an
empty one" rule for the same shape of reason.

## Don't shadow `App`'s own attributes

`textual.app.App` (via `DOMNode`) already owns a private `_registry` attribute (its
mounted-widget registry). `ReviewApp` deliberately stores the caller-supplied step registry
as `self._step_registry`, not `self._registry` — the collision is silent at assignment time
and only surfaces later, as a confusing `AttributeError` deep inside Textual's own shutdown
path (`'str' object has no attribute '_close_messages'` when the registry tuple of step
names replaces Textual's own node registry). If a future `App` subclass attribute picks a
name that collides with a Textual internal, expect the same class of confusing failure —
check `dir(App)`/`dir(DOMNode)` before naming a new attribute here.

## The `InputRelay` seam (issue #41)

`input_relay.py`'s `InputRelay` is, like `state.py`, deliberately Textual-import-free —
its queueing contract (`request_input`/`next_request`) is unit-tested in isolation in
`tests/tui/test_input_relay.py`, independent of a running `App`/`Pilot`. It exists to
break a construction-order cycle: `cli.py` needs `ctx.on_input_needed` (see
`pipeline/step.py`'s `StepContext.on_input_needed`) bound before `StepContext`/
`run_steps(...)` can be built, but `ReviewApp` is constructed *from* that same `events`
generator — neither side can hold a live reference to the other yet. `cli.py` builds the
`InputRelay` first and hands `relay.request_input` to `StepContext` and `relay` itself to
`ReviewApp(..., input_relay=relay)` independently.

`ReviewApp.on_mount` starts a second worker (`_relay_input`) alongside `_consume_events`
when `input_relay` is not `None`, in its own worker `group` — `run_worker(...,
exclusive=True)` only cancels other workers in the *same* group, so the events worker
(default group, exclusive) and this one don't race to cancel each other. Each iteration
awaits `input_relay.next_request()`, pushes `screens.InputPromptScreen` via
`await self.push_screen_wait(...)` to collect one line of human input, and resolves the
matching `request_input` call's future with it.

**Known limitation**: the modal round-trip is proven end-to-end against a hand-built
`InputRelay` request in `tests/tui/test_app.py`, and the detection/relay logic itself is
proven against a fake CLI subprocess in `tests/agent/test_claude_cli.py` — but the two have
not been exercised together against a real `claude` CLI process that actually blocks on
stdin waiting for a permission answer. See `agent/AGENTS.md`'s matching note.

## The Findings box (issue #42, widened for #61 and #87, rebuilt as a widget tree by #91)

`FindingsList` (`widgets.py`) replaced the original `FindingsBox` (a single `OptionList` of
Rich-rendered options) with a true Textual widget-composition tree: `FindingsList` (a
`Vertical`, not a `_BorderedBox`/`Static` subclass like `PipelineBox`/`StatusBox` — it needs
to host a child `_FindingsListView`, a summary `Static`, a footer-hint `Static`, and, while
parked, an inline chat widget too, none of which a plain `Static` can host) hosts a private
`_FindingsListView(ListView)`, which hosts one `Finding(ListItem)` widget per finding, each
composing `FindingsDescription(Static)` (severity dot, description, location, in a left
column with a `border-right` vertical-rule divider) and `FindingsSuggestion(Static)` (that
finding's suggestions or, while parked and highlighted, its live decision cycle, in a right
column) in a horizontal split. `ListView(can_focus=True, can_focus_children=False)`: the
`ListView` itself holds keyboard focus, its `ListItem` children can never be individually
focused, and it assumes every mounted child is a `ListItem` (`action_cursor_up/down`,
`watch_index`, `highlighted_child` all index/assert against `self._nodes` unfiltered) — so
the summary/footer `Static`s are siblings of `_FindingsListView`, not its children (mounting
one into it would silently corrupt that indexing), and `_FindingsListView.__init__` asserts
every item it's given is a `Finding` for the same reason. `Finding` shadows
`pipeline.findings.Finding`; `widgets.py` imports the pydantic model as `FindingData` to
free up the name, since this widget's identity in this module *is* "one finding, rendered."

Mirrors `PipelineBox`'s shape (a bordered box, an `update_*` method) but with a different
mount lifecycle: `PipelineBox` is always composed (empty registry entries render as pending
placeholders), while `FindingsList` is mounted/removed dynamically by
`ReviewApp._render_findings` because "no findings" must show no box at all, not an empty
one. `state.py`'s `latest_findings` picks the most recently *completed* step whose
`outcome.findings` is a non-empty `ReviewOutput` (imported from `steps.review`),
`TestSufficiencyOutput` (imported from `steps.test_sufficiency`), or bare `list[Finding]`
(`steps/rebase.py`'s two `needs_approval=True` returns, widened for issue #87 — without this
a `RebaseStep` park would have no `FindingsList` content at all, once issue #87 removed
`ApprovalPromptScreen`'s own fallback rendering) -- the two schema imports are data-schema
imports, not a `ReviewStep`/`TestSufficiencyStep`/agent-call dependency, and neither creates
an import cycle since `steps/` never imports `tui/`; `IntentStep`'s outcome (`findings` is
an `Intent`) is exactly what the `isinstance` checks there guard against. `widgets.py`'s
module-level render functions (`render_description`, `render_suggestions_plain`,
`render_decision_cycle`, all pure and unit-tested without Textual, per this module's own
pure/impure split) never branch on which of the three outcome shapes they were handed beyond
one point of entry, `_findings_of`, which normalizes all three to a plain `list[Finding]` --
everything downstream of it is unchanged regardless of shape. One box, most-recent-
completion-wins -- not an accumulated history across steps, matching `PipelineBox`'s own
"one box, updated in place" pattern.

**Only the highlighted finding shows its suggestions (issue #88, deliberately preserved by
#91)**: every `Finding` row owns its own display mode (`_mode: "hidden" | "plain" |
"decision"`) and, while parked, its own `_decision_cursor` -- a purely per-row browsing aid,
not itself a decision (see below). `FindingsList.on_list_view_highlighted` (handling
`ListView`'s own built-in `Highlighted` message -- using it is not a violation of this
package's "no custom Message subclasses, use owner delegation" convention, which is about
this codebase inventing its own messages, not about Textual's) hides the previously
highlighted row and shows the newly highlighted one, tracked via `self._last_highlighted`
rather than re-derived from `_FindingsListView.index` each time. Choosing between the
alternative of always showing every row's suggestions was explicitly rejected (issue #88),
to avoid clutter when a step has many findings -- #91's rebuild kept this rule unchanged,
just reimplemented against the new tree.

**No longer display-only (issue #87, its menu simplified since)**: while a step is parked,
`FindingsList.await_decision` (awaited by `app.py`'s `_relay_approval`, see below) turns the
highlighted row's `FindingsSuggestion` into a live decision selector cycling through
`[*finding.suggestions, "Chat about it"]` (`_decision_entries`, `_CUSTOM_ENTRY`) — left/right
arrow keys move that row's `_decision_cursor` through that list (reset to 0 whenever the
highlighted finding changes), Enter confirms whatever it's on, and digit keys "1".."9"
(`_FindingsListView`'s `jump_decision(n)` bindings, delegating to
`FindingsList._jump_decision` → the highlighted `Finding.jump_decision`) jump
`_decision_cursor` straight to that entry — a no-op if the digit is past the highlighted
finding's own entry count, since a finding with fewer suggestions than another has a shorter
list. `render_decision_cycle` renders this list 1-based ("1. rename it", "2. Chat about it",
…), labeling entry 0 " (Recommended)" when it came from the finding's own `suggestions`
(styled after the Claude Code CLI's own interactive picker), and the one fixed entry
(`_CUSTOM_ENTRY`) gets a short indented detail line of static UI copy (`_ENTRY_DETAILS`) a
suggestion's own text doesn't get, since it has no further data to split one from.

Every entry in this list is discussion-only (it does not auto-apply anything — issue #78's
`EditStep`/apply machinery is still out of scope): confirming a suggestion or "Chat about it"
mounts `_InlineApprovalChat`, a small `Vertical` with a prompt `Static` and an `Input` seeded
with that suggestion's text (or empty, for "Chat about it"), submitting which resolves the
park with `ApprovalResponse(decision="fix", instructions=<what was typed>)` — `_open_chat` is
a no-op if one is already mounted, so re-entering this path never stacks a second prompt.
`_FindingsListView`'s "f" binding (`action_open_chat`) jumps straight to it regardless of
cursor position, mirroring the removed `ApprovalPromptScreen`'s own "f"; unlike before, the
decision cursor itself also opens it automatically the moment left/right or a digit key moves
it *onto* "Chat about it" (`FindingsList._cycle_decision`/`_jump_decision`, not
`Finding.cycle_decision`/`jump_decision` themselves, which stay pure cursor moves with no
Textual side effect) — so browsing onto that entry already starts typing, no extra Enter/"f"
needed. This auto-open is deliberately *not* wired into the plain per-row highlight reset
path (`on_list_view_highlighted`/`_prime_highlighted`, which calls `reset_decision()`/
`set_decision()` directly, no cursor-move call at all): a finding with zero suggestions has
"Chat about it" at cursor 0, and merely arrow-key-browsing between finding rows must never
yank focus into a chat box — only a deliberate intra-row cursor move does that.

Approve/skip/abort used to also live in `_decision_entries` as three more fixed cycle-through
entries (`_DECISION_ENTRIES`), each resolving the park directly with no chat step, with
matching single-key "a"/"s"/"x"/"f" shortcuts on `_FindingsListView` mirroring
`ApprovalPromptScreen`'s own. That menu was later simplified once the product call landed
that a human can just describe what they want in the chat instead — every decision that
reaches this box now resolves via that same chat mechanism (`decision="fix"`), with no
separate intent-parsing needed for the common case. Approve is no longer reachable from this
UI at all, in any form — it was removed for good, with no replacement. Skip and abort each
survive, kept as separate global controls rather than listed per-finding options:
`_FindingsListView`'s "s"/"x" bindings (`action_quick_skip`/`action_quick_abort` →
`FindingsList._quick_decision("skip"/"abort")`) still resolve the park directly, unchanged,
since both are step-level decisions with no discussion step of their own. Skip's binding was
briefly removed alongside approve/skip's per-finding menu entries, then restored as a bare
escape hatch once it became clear the chat mechanism cannot resolve every park: a step whose
own `run` ignores `ctx.fix_round` entirely (`steps/rebase.py`'s issue #24 guard is exactly
this — see `pipeline/executor.py`'s "The approval park" section for why `decision="fix"`
unconditionally re-runs the *same* step rather than advancing) re-parks on the identical
finding no matter what a human types, so without skip the only way past that specific park
would be abort. Approve never had an equivalent need — a step already completes and advances
on its own without a human approving it, so there was nothing left for that key to do once
its listed menu entry was removed. The decision itself stays step-scoped, not per-finding
either way: confirming the chat, skipping, or aborting from *any* finding's row resolves the
one pending park, regardless of which row the cursor was browsing — `_decision_cursor` is
purely a per-row display aid. A `#findings-footer` `Static` beneath the summary line shows
bound-key copy ("Enter to confirm, left/right or 1-9 browse options, f to chat, s to skip, x
to abort") only while parked, matching this package's "no box, not an empty box" instinct
applied at the sub-widget level -- no "Esc to cancel" clause, since no such binding exists.
Outside a park, the box behaves exactly as #88 already shipped: read-only, only the
highlighted finding's suggestions shown, no key does anything.

`FindingsList.update_findings` is called on every one of `app.py`'s periodic render ticks
(`_render` → `_render_findings`), not just when the displayed output actually changed.
`FindingsList` keeps its own authoritative `self._rows: list[Finding]`, populated once at
construction time and mutated by `update_findings` itself -- never re-derived fresh from
`_FindingsListView.children` on each call, because that live query is unsound during a real
timing window this rebuild introduced: a widget's constructor-supplied children (here, these
same `Finding` instances, passed to `_FindingsListView` in `compose()`) are only flushed
into Textual's own `_nodes` once that widget's own `Compose` message is later processed by
the message pump, and `update_findings` (a synchronous method, called from `app.py`'s
non-async `_render()`) can run before that happens; re-deriving from `.children` at that
moment undercounts `self._rows` and mounts duplicate `Finding` widgets for rows that already
exist, just not yet flushed (confirmed empirically against this Textual version, not just
reasoned about). Given `self._rows`, the common case (finding count unchanged) updates every
existing row in place via `Finding.update_finding` -- touching no DOM structure at all, so
`_FindingsListView.index`, every row's own `_decision_cursor`/`_mode`, and any mounted
`_InlineApprovalChat` (a sibling of `_FindingsListView`, untouched by this method regardless)
all survive completely untouched; a same-length redundant tick, the common case given
`app.py`'s 0.25s timer, now touches nothing at all. Only the finding count actually growing
or shrinking mounts or removes rows, and only the ones beyond the overlap with the old list
-- every retained row, including the highlighted one, is still updated in place first. This
reconciles the overlap/tail directly rather than `_FindingsListView.clear()` followed by
`.extend(...)` (the more obvious-looking "rebuild" shape) for a second, independent reason:
Textual removes a `ListView`'s old children asynchronously too (`ListItem.remove()` posts a
`Prune` message, actually dropped from `self._nodes` only once that message is later
dispatched), so `clear()` then `extend()` back-to-back leaves `_nodes` briefly containing
*both* the old (still-pruning) and new rows, and setting `.index` right after would highlight
a stale, about-to-be-removed row instead of the intended new one (also confirmed
empirically). Two more corollaries of the same "mounting/composing a widget tree is async,
this code is sync" gap, guarded the same "skip now, the eventual real render reflects the
already-updated data anyway" way: `Finding.update_finding`/`set_hidden`/`set_plain`/
`set_decision` no-op (via a `NoMatches` guard) when this row's own `compose()` (yielding
`FindingsDescription`/`FindingsSuggestion`) hasn't run yet, and `Finding.compose()` itself
calls `_apply_mode` once it does run, priming `FindingsSuggestion` from whatever `_mode`/
`_decision_cursor` a call landed while it wasn't ready yet; `FindingsList.await_decision`
awaits a short bounded `_await_list_view()` retry before focusing `_FindingsListView` (the
one caller where silently skipping would leave every keypress for the rest of that park
going nowhere, unlike a merely stale render elsewhere), rather than the plain, synchronous,
"return `None` if not ready yet" `_list_view()` every other caller uses.

**`FindingsDescription`/`FindingsSuggestion` are matched `width: 1fr` columns (issue #92)**,
superseding #91's `width: auto` description column against `FindingsSuggestion`'s `1fr`.
That earlier shape sized each row's description to its own content, squeezing
`FindingsSuggestion` by a different amount on every row depending on that row's own
description length -- since only one row is ever highlighted at a time (every other row's
`FindingsSuggestion` is empty), the split ratio a human actually saw while browsing findings
changed from row to row instead of reading as one consistent grid, exactly the "grid is
inconsistent between screenshots" report that prompted #92. Matched `1fr` shares fix that:
every row shares its `_FindingsListView`'s width, so two equal `fr` columns land at the
identical 50/50 boundary regardless of which row is highlighted or how long its own
description happens to be. A long description now wraps within its half of the row instead
of squeezing its sibling (`render_description` no longer sets `no_wrap` -- Textual's own
default word-wrap on a bounded-width `Static` handles it), closing out #91's own "a real fix
is a genuinely separate design question" note on this exact tradeoff.

`FindingsSuggestion` is `display: none`, not just empty content, while hidden -- an
always-reserved `1fr` column would squeeze `FindingsDescription` into half the row even on
every non-highlighted row, which has nothing to show on the right at all. `display: none`
drops it out of `Finding`'s horizontal layout entirely, so `FindingsDescription` (the only
sized child left) takes the whole row -- the same "no box, not an empty box" instinct
`FindingsList`'s own mount lifecycle already follows, applied one widget level down. The
`-visible` class (added in `show_plain`/`show_decision`, removed in `clear`) restores
`display: block` and draws a full `border` around the column (issue #92's "box around
`FindingsSuggestion` so it reads as its own widget") -- replacing #91's `border-right` on
`FindingsDescription`, which drew a single divider line on every row regardless of whether
there was anything on the right to divide from. `FindingsSuggestion.border_title =
"Suggestion"` is set directly in `__init__`, the same mechanism `PipelineBox`/`FindingsList`/
`StatusBox` already use -- it only actually renders once `-visible` has added the border, so
it costs nothing while hidden.

## The `ActivityRelay` seam (issue #66)

`activity.py`'s `ActivityRelay` is the same shape as `InputRelay` above, for a different
purpose: a second, independent progress stream for nested sub-step activity (one `git
fetch`, one agent call), not a new `StepEvent` status. Textual-import-free like
`input_relay.py`/`state.py`, unit-tested in isolation in `tests/tui/test_activity.py`.
Breaks the same construction-order cycle `InputRelay` does: `cli.py` builds one
`ActivityRelay`, hands it to `StepContext.activity_reporter` (via `pipeline/step.py`'s
`ActivityReporter` Protocol) on one side and to `ReviewApp(..., activity_relay=relay)` on
the other.

`ReviewApp.on_mount` starts a third worker (`_consume_activities`) in its own worker
`group` ("activity-relay", distinct from both the default events group and "input-relay")
when `activity_relay` is not `None`. Each iteration awaits `activity_relay.next_event()`
and appends the raw `ActivityEvent` to `self._activity_events` — owner correlation does
NOT happen here, at receipt time (see below); `ActivityRelay` itself never needs to know
steps exist.

**Owner tagging is NOT "`self._running_step` at receipt time" (#66's original design,
found unsound once real producers existed — issues #64 and #65)**: steps themselves do run
strictly sequentially and never in parallel, but `_consume_events` (the `StepEvent` worker)
and `_consume_activities` are two independently scheduled `asyncio.Task`s draining two
separate queues with no ordering guarantee between them. A step's activity can have its
"finished" event still queued at the moment the *next* step has already started — this is
not a rare edge case but `ReviewStep`'s (#65) ordinary call shape, since its activity closes
right at the tail of `run` with no further `await` before the next step starts — so naively
re-reading `self._running_step` for that "finished" event tags it with the wrong (next)
step, splitting one activity's two events across two different owners. `backfill_activities`
cannot handle that (it assumes both halves share one owner; a mismatch produces either a
phantom permanently-"running" row or a `KeyError`). `#66`'s own synthetic test never caught
this because it keeps one step "running forever," so no step transition ever races an
in-flight activity.

The fix, `app.py`'s module-level `_tag_activity_events`/`_owning_step` (issue #65): compute
ownership fresh, at render time, purely from each step's own running window (its "running"
`StepEvent`'s `started_at` through its "completed" one's implied end time, or open-ended
while still running) — never from live, potentially-stale worker state. Since
`ctx.report_activity` can only be called from inside a step's own `run` coroutine, and
steps never run in parallel, every activity's timestamp is guaranteed to fall inside its
owning step's window; both halves of one span are tagged with the SAME owner (computed once
from the "started" event and reused for "finished"), so scheduling order between the two
workers can never split a span across owners. An earlier fix attempt (recording the owner
once, at receipt of the "started" event, in a `dict[int, str | None]`) still implicitly
depended on the "started" event itself being dequeued before `self._running_step` could
change, which is not a documented `asyncio` guarantee even if it holds in today's call
shapes — `_tag_activity_events` has no dependency on scheduling order at all, so it was
adopted as the sound, permanent design. Tagged `(step_name, ActivityEvent)` pairs are
recomputed by `_rows()` on every render and feed `state.backfill`'s `activity_events`
parameter.

`state.py`'s `backfill_activities` groups those tagged pairs into one `ActivityRow` per
activity under a given step, using the identical "elapsed-while-running, final-once-
finished" duration rule `backfill` uses for `StepRow` itself; `backfill` attaches each
step's own `ActivityRow`s to that `StepRow.activities` field. `widgets.py`'s
`render_rows`/`render_rows_live` render each row's activities as directory-tree-style lines
(`├─`/`└─` connectors, the last activity in a step's list getting the closing `└─` --
`format_activity_row`'s `is_last` argument, computed by the caller since neither rendering
function knows a `ActivityRow`'s position among its siblings on its own) immediately
beneath it, reusing `_STATUS_ICONS`/`format_duration` rather than a parallel set — deliberately no live `Spinner` for a running activity (see `_render_activity_row`'s
docstring): "live" comes from the duration number itself ticking on `PipelineBox`'s
existing 60fps refresh via `ReviewApp`'s own re-render, the same way a `StepRow`'s duration
does. Activity lines stay attached to their step permanently once reported, regardless of
that step's own current status — the same way a completed `StepRow` itself stays visible
for the rest of the run, rather than disappearing once the step moves on.

First proven end to end with a hand-built, synthetic `relay.activity(...)` call feeding a
real `ReviewApp` in `tests/tui/test_app.py`, exactly mirroring how #41 proved `InputRelay`'s
own queueing contract before any real backend existed. `gitutils.run_git` (issue #64,
`steps/AGENTS.md`) and `ReviewStep`'s one agent call (issue #65, `steps/AGENTS.md`) are now
the two real producers — see `tests/steps/test_rebase.py`'s "Activity reporting" section
and `tests/steps/test_review.py`'s activity-span tests for real end-to-end runs proving the
full sequence, which between them is what caught the owner-tagging race documented above.
Both issues are closed.

## The `ApprovalRelay` seam (issue #80, extended by #81 and #87)

`approval_relay.py`'s `ApprovalRelay` is the same shape as `InputRelay`/`ActivityRelay`
above -- Textual-import-free, unit-tested in isolation in `tests/tui/test_approval_relay.py`
-- for a third purpose: relaying a parked step's approve/skip/fix/abort decision. Breaks the
same construction-order cycle the other two do: `cli.py` builds one `ApprovalRelay`, hands
`relay.request_approval` to `StepContext.on_approval_needed` on one side and `relay` itself
to `ReviewApp(..., approval_relay=relay)` on the other. The caller that actually invokes
`on_approval_needed` is `pipeline.executor.run_steps` itself (not a step -- see that
module's "The approval park" section), the moment a step's `StepOutcome.needs_approval` is
True.

`ReviewApp.on_mount` starts a fourth worker (`_relay_approval`) in its own worker group
("approval-relay") when `approval_relay` is not `None`. Each iteration awaits
`approval_relay.next_request()` (the parked step's name and its `StepOutcome`), sets
`self._parked_step` and re-renders (so the Pipeline box shows that row as "parked" and the
Findings box already shows this step's own findings -- `_render_findings` mounted it the
moment this step's "completed" `StepEvent` arrived, before the park was ever noticed, per
`run_steps`'s own "yield completed, then check needs_approval" ordering), then `await`s
`self.query_one(FindingsList).await_decision()` directly -- **no modal, as of issue #87**;
see the Findings box section above for what that call does. Once it resolves, "skip" is
recorded into `self._skipped_steps` (permanently, the same "stays visible for the rest of
the run" rule reported activities and `_failed_step` already follow), `self._parked_step`
clears back to `None`, the app re-renders again, and only then does
`future.set_result(response)` resolve -- so the app's own rendered state already reflects
the decision by the time the parked `run_steps` call resumes (or, on "abort", raises
`pipeline.executor.RunAbortedError`, unwinding the run through the same generic
`ReviewApp.error` path every other step failure already uses -- `cli.py` needs no dedicated
`except` clause for it).

**"Parked"/"skipped" are overrides of "completed", not a third `StepEvent` status** -- see
`state.py`'s module docstring's `parked_step`/`skipped_steps` section for why (the same
design nuance driving `_relay_approval`'s ordering above): `run_steps` already yields a
step's "completed" event before it even checks `needs_approval`, so by the time a park
happens the event stream already calls that step "completed". `ReviewApp` is the only thing
that knows a step is currently parked or was skipped, exactly the same "derived by the
caller, not reported by the executor" rule `failed_step` already established.

**`ApprovalPromptScreen` (issue #87, removed)**: this seam used to push a dedicated modal
(`screens.ApprovalPromptScreen`, offering both `Button`s and single-key "a"/"s"/"f"/"x"
`BINDINGS`) collecting a bare `ApprovalDecision`, duplicating the same findings content
`FindingsList` already showed above it (via its own `_format_outcome` helper). Issue #87
removed both the screen and that helper entirely, in favor of the inline decision selector
described in the Findings box section above -- one of #87's five resolved design questions
was explicitly "remove the modal entirely," so every park now resolves inline, with no
screen ever pushed. `screens.py` now holds only `InputPromptScreen`, still used by the
unrelated `InputRelay` seam above.

First proven end to end with a hand-built, synthetic parked `StepOutcome` in
`tests/tui/test_app.py` (a generator that calls `relay.request_approval` itself, exactly
mirroring how #41/#66 each proved `InputRelay`/`ActivityRelay` before any real producer
existed), then against `steps/rebase.py`'s already-shipped issue #24 guard end to end
(`tests/test_cli_review.py`'s `repo_with_unpushed_local_default_commits` fixture, driven
over a real pty). That real-pty helper (`_run_review_with_keypresses`) had to drain the
child's `stdout`/`stderr` continuously on background threads, not just once at the end the
way `_run_review_and_press_e_to_exit` does -- an earlier version that only read output via
`communicate()` reproduced an intermittent deadlock (a keypress sent during an undrained
multi-second wait could be silently dropped, since `PipelineBox`'s own repaint ticks can
fill the pty's output buffer and block the child's single-threaded event loop, including its
own stdin-reading task) -- see that helper's own docstring. The real-pty test's "a"/"s"/"x"
keypresses kept working unchanged across #87's rewrite (and again across #91's widget-tree
rebuild) -- `_FindingsListView` binds the same letters, just against `FindingsList` instead
of a modal. Once the per-finding menu was later simplified to drop approve/skip as *listed*
entries (only "Chat about it" survives there), that same test's "a" keypress stopped
resolving anything at all -- approve has no replacement, so any scenario proving "approve
continues the run" was retired outright, not rewritten. "s" (skip) kept working unchanged:
it was briefly removed alongside the listed menu entries, then restored as a bare global
escape hatch (matching "x"/abort's own treatment) the moment it became clear the chat
mechanism cannot resolve every park -- `steps/rebase.py`'s issue #24 guard is the concrete
case that proved it: `RebaseStep.run` never reads `ctx.fix_round`, so a human's "fix"
response there just re-parks on the identical finding forever (see the Findings box section
above for why), leaving skip as the only way past that specific park short of aborting.
`tests/test_cli_review.py` reflects this exactly: the rebase-park test still answers with
"s", the two-park blocking-finding test was rewritten to answer both parks with "s" instead
of the "a" it used before (that fake `claude` also ignores what it's asked to fix, so chat
cannot resolve it either), and "x" (abort) needed no change throughout.

## The "fix" response and the inline chat widget (issue #81, reworked by #87)

The fourth park response, "fix" (`pipeline.step.ApprovalDecision`/`ApprovalResponse`,
defined in `pipeline/step.py`), originally collected free-text instructions via a second
modal (`InputPromptScreen`) pushed right after `ApprovalPromptScreen` dismissed with "fix".
Issue #87 replaced that two-modal round-trip with `widgets._InlineApprovalChat`: a small
`Vertical` (prompt `Static` + `Input`) `FindingsList` mounts on demand -- when a human
confirms a suggestion string (seeded with that suggestion's own text) or "Type something."
(seeded empty) from the decision cycle described above. `_open_chat` is a no-op if one is
already mounted, so re-entering this path never stacks a second prompt. Submitting it
resolves the pending park with `ApprovalResponse(decision="fix", instructions=<what was
typed>)`, then the widget removes itself; every other decision resolves with
`instructions=None`. `InputPromptScreen` itself is untouched and still exists, just no
longer used by this seam -- it remains the unrelated `InputRelay` seam's own modal
(issue #41).

Because each fix-round re-run gets its own fresh "running"/"completed" `StepEvent` pair
(`pipeline/executor.py`'s own design, see that module's AGENTS.md), the Findings box shows
the fresh round's findings for free -- no new `tui/` rendering logic was needed for that
half of the acceptance criteria, unchanged from #81.

First proven with a hand-built, synthetic parked outcome and relay in
`tests/tui/test_widgets.py`/`tests/tui/test_app.py`, mirroring how #80's own approve/skip/
abort flow and #81's original modal round-trip were each first proven.

## Non-goals landed in later issues, not here

- `TestSufficiencyStep`'s own fix-mode prompt (issue #82, blocked by #81) is not built here
  or anywhere in this ticket -- `steps/test_sufficiency.py`/`prompt/test_sufficiency.py`
  are untouched; that step still only ever reaches the plain approve/skip/fix/abort park
  (never the automatic round) since it leaves `Step.supports_fix_round` at its `False`
  default (see `pipeline/AGENTS.md`'s "The fix-round loop" section for why that gate
  exists).
- Suggestion-selection/`EditStep`/yolo-mode (issue #78) and head continuity (Milestone 9)
  remain out of scope, unaffected by this ticket.
