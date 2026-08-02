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

## Non-goals landed in later issues, not here

- Relaying an agent subprocess's stdin prompts through the TUI is issue #41
  (`RunOpts.on_input_needed`) — no such seam exists yet.
- A findings-display box is issue #42 — `PipelineBox` is the only widget so far.
- The interactive approve/fix/skip/abort layer waits on Milestone 7's approval loop, which
  isn't specced yet.
