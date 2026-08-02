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
passes that name as `backfill`'s `failed_step` only in the `except` branch of
`_consume_events`, for the one final render before `self.exit()`. Do not add a "failed"
status to `StepEvent` itself to "simplify" this — that would touch `pipeline/step.py`'s
already-shipped (#39) contract for a purely presentational concern.

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

## The Findings box (issue #42)

`FindingsBox` (`widgets.py`) is a second `Static` widget, mirroring `PipelineBox`'s shape
(`DEFAULT_CSS`, a bordered box, `update_findings`) but with a different mount lifecycle:
`PipelineBox` is always composed (empty registry entries render as pending placeholders),
while `FindingsBox` is mounted/removed dynamically by `ReviewApp._render_findings` because
"no findings" must show no box at all, not an empty one. `state.py`'s `latest_findings`
picks the most recently *completed* step whose `outcome.findings` is a non-empty
`ReviewOutput` (imported from `steps.review` -- a data-schema import, not a `ReviewStep`/
agent-call dependency, and does not create an import cycle since `steps/` never imports
`tui/`); `IntentStep`'s outcome (`findings` is an `Intent`) is exactly what the `isinstance`
check there guards against. One box, most-recent-completion-wins -- not an accumulated
history across steps, matching `PipelineBox`'s own "one box, updated in place" pattern.
Display only: no key or action here lets a user approve, fix, skip, or abort a finding.

## Non-goals landed in later issues, not here

- The interactive approve/fix/skip/abort layer waits on Milestone 7's approval loop, which
  isn't specced yet.
