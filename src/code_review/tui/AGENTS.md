# AGENTS.md — src/code_review/tui/

Scope: this package only. See the [root AGENTS.md](../../../AGENTS.md) for repo-wide
conventions.

A full-screen [Textual](https://textual.textualize.io/) view of a live review run, wired
into `cli.py review`. It renders `pipeline.executor.run_steps`' `StepEvent` stream as a
Pipeline box, shows the most recently completed step's findings in a Findings box, and lets
a human resolve a parked step's approve/skip/fix/abort decision inline. It never decides
what runs or in what order — that stays `pipeline`'s job; this package only renders and
relays.

**Textual-import-free modules** — `state.py`, `input_relay.py`, `activity.py`,
`approval_relay.py` — carry no Textual dependency, so their logic is unit-tested as plain
Python, independent of a running `App`/`Pilot`. Keep it that way: if a change to one of them
needs anything from Textual, that behavior belongs in `app.py` or `widgets/` instead.

## `ReviewApp` (`app.py`)

The `App` subclass. Takes `registry` (step names) and `events` (an
`AsyncIterator[StepEvent]`) as constructor arguments rather than building them itself —
`cli.py` passes `run_steps(steps, ctx)`; tests pass a hand-written async generator. This
injection seam is what makes `ReviewApp` testable via Textual's `Pilot`/`run_test()`
without a real agent subprocess.

- Runs up to four workers from `on_mount`, each in its own group so `exclusive=True` on the
  events worker can't cancel the others: `_consume_events` (always), and — only when the
  corresponding relay is passed in — `_relay_input`, `_consume_activities`,
  `_relay_approval`.
- `_consume_events` drains `events`, re-renders on every `StepEvent` and on a 0.25s timer
  tick (so a running step's elapsed duration visibly ticks), and marks the run `_done` in a
  `finally` once `events` is exhausted or raises. On an exception it infers the failing step
  as whichever step was last seen `"running"` with no matching `"completed"` (`StepEvent`
  itself has no "failed" status — see `state.py`) and stores it in `self._failed_step`,
  read by every later render for the rest of the app's lifetime.
- The app does not exit itself when the run ends: it stops the tick timer and mounts a
  Status box naming the outcome. Pressing "e" (`action_exit_when_done`) is a no-op until
  `self._done` — a stray "e" mid-run can't cut it short; only Textual's own `ctrl+q`/
  `ctrl+c` can.
- `_relay_approval` learns a step parked, sets `self._parked_step`, re-renders (the
  Findings box already shows that step's findings — it mounted on the "completed" event
  before the park was noticed), then awaits `FindingsList.await_decision()` directly — no
  modal. "skip" is recorded into `self._skipped_steps` (kept for the app's lifetime, same as
  `_failed_step`); "abort" unwinds via `pipeline.executor.RunAbortedError` through the
  normal `ReviewApp.error` path.
- `_tag_activity_events`/`_owning_step`: attribute each `ActivityEvent` to its owning step
  purely from `StepEvent` timestamps, computed fresh at render time — not from
  `self._running_step` at receipt time. `_consume_activities` and `_consume_events` are two
  independently scheduled workers with no ordering guarantee between them, so tagging at
  receipt time can split one activity's started/finished pair across two different owners;
  timestamp-window attribution can't.
- Stores caller-supplied state as `self._step_registry`, not `self._registry` —
  `textual.app.App`/`DOMNode` already owns a private `_registry` (its widget registry);
  shadowing it corrupts app teardown with a confusing error far from the assignment. Check
  `dir(App)`/`dir(DOMNode)` before naming a new attribute here.

## `state.py`

Pure backfill of `StepEvent`s (plus the app's own park/skip/activity bookkeeping) into
render-ready rows. No Textual import.

- `backfill(registry, events, ..., failed_step, parked_step, skipped_steps,
  activity_events)` → `list[StepRow]`, one per registry entry: pending (no event),
  running/failed, completed, or parked/skipped (both are caller-supplied overrides of a
  "completed" event — `run_steps` already yields "completed" before checking
  `needs_approval`, so park/skip are never a third `StepEvent` status).
- `backfill_activities` groups tagged `(step_name, ActivityEvent)` pairs into
  `ActivityRow`s nested under `StepRow.activities`, using the same elapsed/final duration
  rule as `StepRow` itself.
- `latest_findings(events)` scans for the most recently completed step whose outcome
  carries a non-empty `ReviewOutput`, `TestSufficiencyOutput`, or bare `list[Finding]`
  (imported from `steps.review`/`steps.test_sufficiency` as data-schema-only imports —
  `steps/` never imports `tui/`, so no cycle). One box, most-recent-completion-wins, not an
  accumulated history.
- `final_status_message(error)` renders the Status box's one-line outcome plus the "press
  'e'" reminder.

## `InputRelay` (`input_relay.py`), `ActivityRelay` (`activity.py`), `ApprovalRelay` (`approval_relay.py`)

Three independent relays of the same shape: an `asyncio.Queue`-backed class with a
producer-side method (called from backend/pipeline code via `StepContext`) and a
consumer-side `next_*()` awaited by one of `ReviewApp`'s workers. Each exists to break a
construction-order cycle — `cli.py` needs the callback bound into `StepContext` before
`run_steps(...)` can be built, but `ReviewApp` is constructed *from* that same `events`
generator, so neither side can hold a live reference to the other. `cli.py` builds the relay
first and hands one side to `StepContext`, the other to `ReviewApp`.

- `InputRelay.request_input(prompt) -> str` / `next_request()`: one line of free-text input
  for a blocked backend subprocess (`StepContext.on_input_needed`), collected via
  `InputPromptScreen`.
- `ActivityRelay.activity(label)` (async context manager) / `next_event()`: nested sub-step
  progress (`StepContext.report_activity`, e.g. `gitutils.run_git`, `ReviewStep`'s agent
  call). Nesting/`parent_id` is automatic via a `contextvars.ContextVar`. `ActivityRelay`
  itself never knows which step an activity belongs to — see `_tag_activity_events` in
  `app.py` for that correlation.
- `ApprovalRelay.request_approval(step_name, outcome) -> ApprovalResponse` / `next_request()`:
  a parked step's approve/skip/fix/abort decision, called from
  `pipeline.executor.run_steps` itself (not from a step — see `pipeline/AGENTS.md`'s "The
  approval park"). `ApprovalDecision`/`ApprovalResponse` are imported from `pipeline.step`,
  not redefined here.

## `screens.py` — `InputPromptScreen`

The one remaining modal: a `ModalScreen[str]` with a prompt `Static` and an `Input`,
dismissing with the submitted line. Used only by the `InputRelay` seam. (Approval used to
have its own modal pair here; that was replaced by `FindingsList`'s inline decision selector
— see below.)

## Widgets (`widgets/`)

One Textual widget class per module. `widgets/__init__.py` is a barrel re-exporting the
full public (and test-imported private) surface, so `from code_review.tui.widgets import X`
keeps resolving regardless of which submodule defines `X`. Dependency direction:
`styles`/`base` have no internal deps → `pipeline_box`/`findings_description`/`status_box`
depend on those → `findings_suggestion` is standalone → `finding` depends on
`findings_description` + `findings_suggestion` → `findings_list_view` depends on `finding`
→ `findings_list` depends on `finding` + `findings_list_view`.

Every widget takes plain data (`StepRow`, `Finding`, ...) and never reads a `StepEvent`
stream or a registry/agent output itself — `app.py`/`state.py` own that translation.

### `styles.py`

Plain-data icon/color constants only, no widget logic: `_STATUS_ICONS`/
`_STATUS_DOT_STYLES`/`_DOT_ICON`/`_ACTIVITY_STYLE` (pipeline box), `_SEVERITY_DOT_STYLES`/
`_DECISION_MARKER_ICONS`/`_DECISION_MARKER_STYLES` (findings description, the marker set
derived from the status set).

### `base.py` — `_BorderedBox`

Shared `DEFAULT_CSS` (border/padding, from `base.tcss`) for `PipelineBox`/`StatusBox`, both
`Static` subclasses. `FindingsList` needs a `Vertical` instead, so it doesn't extend this.

### `PipelineBox` (`pipeline_box.py`)

One line per registry step: status icon, name, elapsed/final duration, nested activity
lines. Always composed (unlike the Findings/Status boxes) — a step with no event yet still
renders as a pending placeholder.

- `format_row`/`render_rows`/`format_activity_row`/`gradient_text` are pure and
  unit-tested without Textual; `render_rows_live` renders Rich renderables so the running
  row's name shimmers (`gradient_text`, phased by `time.monotonic()`) and its icon spins
  (`Spinner`, cached per step name in `PipelineBox._spinners` so the animation clock
  doesn't reset every render).
- Self-driven 60fps `_animate_shimmer` timer re-renders the running row's colors between
  `StepEvent`s; `update_rows` replaces the full row set on every `app.py` render tick.

### `StatusBox` (`status_box.py`)

One-line run outcome, mounted only once the run is done (`app.py`'s `_render_status`) —
a still-running pipeline shows no Status box at all, not an empty one.

### `FindingsList` (`findings_list.py`)

The Findings box: the most recently completed step's findings, one `Finding` row each, plus
a severity-count summary and a bound-key footer hint. A `Vertical`, not `_BorderedBox` — it
hosts three children (`_FindingsListView`, summary `Static`, footer `Static`), which a
`Static` can't. Mounted/removed dynamically by `app.py`'s `_render_findings`, mirroring the
Status box's "no box, not an empty box" rule.

- Only the highlighted row shows anything in its `FindingsSuggestion` column
  (`on_list_view_highlighted`); every other row stays hidden.
- **Per-finding decisions**: each `Finding` row owns its own decision state. Confirming a
  row's chat, or pressing "s" while it's highlighted, records that row's decision alone
  (`_record_decision`) and either advances the highlighted cursor to the next undecided row
  (`_advance_to_next_undecided`) or — once every row in `self._rows` is decided —
  aggregates them via `pipeline.findings.describe_finding_decisions` into one final
  `ApprovalResponse` (`_resolve_park`). A single-row park resolves immediately with that
  row's own response, unwrapped. "x" (abort) is the one exception: always resolves the
  whole park immediately regardless of per-row progress — abort has no per-finding meaning.
- `await_decision()` drives the whole park: resets every row to undecided (a fix-round's
  re-park must not carry over the previous round's decisions), focuses
  `_FindingsListView`, and awaits `self._pending`.
- `update_findings` is called on every render tick whether or not the output changed. It
  reconciles against `self._rows` (this box's own authoritative list), never against a
  fresh `_FindingsListView.children` read — Textual mounts/removes children asynchronously,
  so a live query mid-tick can under/over-count rows still settling into the DOM. The
  common case (row count unchanged) updates every row in place, touching no
  `_FindingsListView`-level DOM structure, so cursor/mode state survives untouched.

### `_FindingsListView` (`findings_list_view.py`)

The focusable `ListView` hosting one `Finding` per finding —
`can_focus=True, can_focus_children=False`, so all keyboard bindings live here (never on
`Finding`, since `ListView`'s own action methods index against `self._nodes` unfiltered) and
delegate to the owning `FindingsList` (a no-op while not parked). Bindings: left/right cycle
the highlighted row's decision entries, digits 1-9 jump to that entry, "f" opens the chat,
"s"/"x" skip/abort, "escape" cancels an open chat without resolving the park.

### `Finding` (`finding.py`)

One row: composes `FindingsDescription` + `FindingsSuggestion` in a horizontal split.
Shadows `pipeline.findings.Finding` deliberately (imported there as `FindingData`) — this
widget's identity *is* "one finding, rendered." Owns three pieces of per-row state: display
mode (`hidden`/`plain`/`decision`), a decision-cycle browsing cursor, and the row's own
recorded `ApprovalResponse` (`None` until decided). `ListItem.can_focus=False` — carries no
key bindings of its own.

### `FindingsDescription` (`findings_description.py`)

The left column: severity dot + description + location, `width: 1fr` matched to
`FindingsSuggestion`'s own `1fr` so every row shares an identical 50/50 split regardless of
highlight state. Renders on every row (unlike `FindingsSuggestion`). While parked, also
prefixes a decided-marker icon (reusing the "completed"/"skipped" glyphs) so a human
browsing away from a just-decided row still sees it recorded. `format_finding`/
`render_description` are pure and unit-tested without Textual.

### `FindingsSuggestion` (`findings_suggestion.py`)

The right column: that finding's `suggestions`, or — while parked and highlighted — a live
decision cycle (`suggestions` + a trailing "Chat about it" entry). Standalone module, no
dependency on any other widget here. `display: none` while hidden so `FindingsDescription`
takes the full row; the `-visible` class restores `display: block` and draws a full border
so this column only reads as its own widget once it has content.

- Confirming "Chat about it" (or cycling/jumping onto it) swaps that trailing line for a
  live `Input` in place (`ensure_input`), seeded with whatever text is being confirmed —
  idempotent, so re-opening never stacks or resets an in-progress chat. `show_decision` is
  safe to call on every redundant render tick: with no `Input` open it re-renders the plain
  entry list; with one open, it leaves the `Input` (and whatever's typed into it) untouched.
- The mounted `Input` is styled `border: none !important; height: 1 !important;` so it
  reads as one inline field, not a box nested inside this column's own border.
