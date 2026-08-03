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
box does not exist at all until `self._done`, matching `FindingsBox`'s own "no box, not an
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

## The Findings box (issue #42, widened for #61)

`FindingsBox` (`widgets.py`) is a second `_BorderedBox` widget (the shared base that also
backs `PipelineBox` and `StatusBox` — see its own docstring for why the border/padding CSS
lives there once, not copy-pasted per box), mirroring `PipelineBox`'s shape (a bordered
box, an `update_*` method) but with a different mount lifecycle:
`PipelineBox` is always composed (empty registry entries render as pending placeholders),
while `FindingsBox` is mounted/removed dynamically by `ReviewApp._render_findings` because
"no findings" must show no box at all, not an empty one. `state.py`'s `latest_findings`
picks the most recently *completed* step whose `outcome.findings` is a non-empty
`ReviewOutput` (imported from `steps.review`) or `TestSufficiencyOutput` (imported from
`steps.test_sufficiency`) -- both are data-schema imports, not a `ReviewStep`/
`TestSufficiencyStep`/agent-call dependency, and neither creates an import cycle since
`steps/` never imports `tui/`; `IntentStep`'s outcome (`findings` is an `Intent`) is exactly
what the `isinstance` check there guards against. `render_findings`/`FindingsBox` themselves
never branch on which of the two schemas they were handed -- both share an identical
`findings: list[Finding]` shape, so only `state.py`'s `isinstance` check and the type
annotations threaded through `widgets.py` needed to widen; the rendering logic itself did
not change. One box, most-recent-completion-wins -- not an accumulated history across
steps, matching `PipelineBox`'s own "one box, updated in place" pattern. Display only: no
key or action here lets a user approve, fix, skip, or abort a finding.

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

## The `ApprovalRelay` seam (issue #80)

`approval_relay.py`'s `ApprovalRelay` is the same shape as `InputRelay`/`ActivityRelay`
above -- Textual-import-free, unit-tested in isolation in `tests/tui/test_approval_relay.py`
-- for a third purpose: relaying a parked step's approve/skip/abort decision. A distinct
class from `InputRelay`, not a reuse of it, since the answer here is a three-way `Decision`
(`Literal["approve", "skip", "abort"]`), not free text. Breaks the same construction-order
cycle the other two do: `cli.py` builds one `ApprovalRelay`, hands `relay.request_approval`
to `StepContext.on_approval_needed` on one side and `relay` itself to `ReviewApp(...,
approval_relay=relay)` on the other. The caller that actually invokes `on_approval_needed`
is `pipeline.executor.run_steps` itself (not a step -- see that module's "The approval
park" section), the moment a step's `StepOutcome.needs_approval` is True.

`ReviewApp.on_mount` starts a fourth worker (`_relay_approval`) in its own worker group
("approval-relay") when `approval_relay` is not `None`. Each iteration awaits
`approval_relay.next_request()` (the parked step's name and its `StepOutcome`), sets
`self._parked_step` and re-renders (so the Pipeline box shows that row as "parked" right
away), pushes `screens.ApprovalPromptScreen` via `await self.push_screen_wait(...)` to
collect a `Decision`, records a "skip" into `self._skipped_steps` (permanently, the same
"stays visible for the rest of the run" rule reported activities and `_failed_step` already
follow), clears `self._parked_step` back to `None`, re-renders again, and only then resolves
`future.set_result(decision)` -- so the app's own rendered state already reflects the
decision by the time the parked `run_steps` call resumes (or, on "abort", raises `pipeline.
executor.RunAbortedError`, unwinding the run through the same generic `ReviewApp.error` path
every other step failure already uses -- `cli.py` needs no dedicated `except` clause for it).

**"Parked"/"skipped" are overrides of "completed", not a third `StepEvent` status** -- see
`state.py`'s module docstring's `parked_step`/`skipped_steps` section for why (the same
design nuance driving `_relay_approval`'s ordering above): `run_steps` already yields a
step's "completed" event before it even checks `needs_approval`, so by the time a park
happens the event stream already calls that step "completed". `ReviewApp` is the only thing
that knows a step is currently parked or was skipped, exactly the same "derived by the
caller, not reported by the executor" rule `failed_step` already established.

`ApprovalPromptScreen` (`screens.py`) mirrors `InputPromptScreen`'s split-out-of-`app.py`
shape, but offers both a mouse path (`Button`s) and a keyboard path (single-key `BINDINGS`:
"a"/"s"/"x") so a script-driven real-pty test (no mouse available) can answer it the same
way a `Pilot`-driven test can. Every `Static` on this screen is constructed with
`markup=False` -- `_format_outcome`'s text ultimately embeds agent-produced `Finding.
description` text (untrusted), and rendering a real `ReviewOutput`/`TestSufficiencyOutput`
outcome via `str(...)` (an earlier version of this function) reproduced a real `MarkupError`
crash from Rich trying to parse that repr's own `[...]`-shaped list syntax as a style tag --
`tests/tui/test_app.py`'s `test_review_app_parks_with_a_review_output_outcome_without_
crashing_on_markup` pins this regression. `_format_outcome` itself now renders a
`ReviewOutput`/`TestSufficiencyOutput` via `widgets.render_findings` (the same function
`FindingsBox` uses) and a bare `list[Finding]` (the shape `steps/rebase.py`'s two
`needs_approval=True` returns actually carry) via `widgets.format_finding`, falling back to
`str(...)` only for a schema this module has no business assuming -- `markup=False` is a
second, independent safety net for that fallback and for `Finding.description` content in
general, not a replacement for rendering known schemas properly.

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
own stdin-reading task) -- see that helper's own docstring.

## Non-goals landed in later issues, not here

- The bounded auto-fix-before-park round (issue #81, blocked by #80) and its mirror for
  `TestSufficiencyStep` (issue #82, blocked by #81) are not built here -- #80 only makes an
  already-`needs_approval=True` outcome stop the run; nothing here runs a fix attempt before
  parking, or resumes the *same* step after a "fix" response (there is no "fix" response
  yet, only approve/skip/abort).
