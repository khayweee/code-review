# TUI

This package is the live, full-screen terminal view of a review run. It renders the fixed
step registry as a "Pipeline" box that updates as [`pipeline`](../pipeline/README.md)'s
executor streams progress, including not-yet-implemented steps as pending placeholders. It
does not decide what steps run or in what order; that stays the executor's job.

## Subunits

| Subunit | Purpose | Input | Output |
| --- | --- | --- | --- |
| `StepRow` | One display row: a step's name, current status, and duration. | Derived by `backfill` | Consumed by `PipelineBox` |
| `backfill` | Pure, Textual-independent function turning the `StepEvent`s seen so far into one `StepRow` per registry entry. | `registry`, `events`, `now`, optional `failed_step` | `list[StepRow]` |
| `PipelineBox` | Bordered Textual widget rendering one line per `StepRow`, with a status icon and formatted duration. | `list[StepRow]` via `update_rows` | Rendered terminal content |
| `render_rows`/`format_row`/`format_duration` | Formatting helpers behind `PipelineBox`, unit-testable on their own. | `StepRow`(s) | Plain display strings |
| `ReviewApp` | The Textual `App`: consumes an injected `events` stream in a worker, re-renders `PipelineBox` on every event and on a timer tick, exits itself when the stream ends or raises, and (if given an `input_relay`) runs a second worker relaying queued prompts through a modal. | `registry: Sequence[str]`, `events: AsyncIterator[StepEvent]`, optional `input_relay: InputRelay` | Terminal UI; `self.error` after `run()` returns |
| `InputRelay` | Textual-import-free queue pairing a blocked backend's prompt with a `pending_answer` future for the human's answer (issue #41). | `request_input(prompt)` from a backend call; `next_request()` from `ReviewApp` | An awaited answer string; an `InputRequest(prompt, pending_answer)` |
| `InputPromptScreen` | Modal screen (`screens.py`) showing one prompt and collecting one line of input. | `prompt: str` | Dismisses with the submitted line |

`STEP_REGISTRY` (`steps/registry.py`) is the single source of truth for step display
names, ordered; `IMPLEMENTED_STEPS` is the ordered prefix of classes `cli.py` actually
runs. A registry entry with no matching class yet still renders in `PipelineBox` as a
pending placeholder for the whole run — that is how a step lands in the live view without
any `tui/` code change once its class exists.

`ReviewApp` receives `registry` and `events` as constructor arguments rather than
importing them itself. That seam is what lets `tests/tui/` drive `ReviewApp` with
hand-built fake `StepEvent`s through Textual's `Pilot`/`run_test()`, independent of a real
agent subprocess. See [`tui/AGENTS.md`](./AGENTS.md) for the non-obvious decisions behind
that seam, how "failed" is derived rather than reported by `pipeline.schemas.StepEvent`
itself, and why `state.py` stays free of any Textual import.

## The approval-park pattern (`ApprovalResponse`)

When a step reports a finding that needs a human ("blocking"), answering it is not one
function calling another. It is **two independent coroutines, running at the same time on
the same event loop, that never call each other's functions directly** — `pipeline/` is
not permitted to import `tui/` at all. Call them:

- the **pipeline flow** — `executor.run_steps` (`pipeline/executor.py`), driving the step
  list, with no idea Textual exists.
- the **TUI flow** — `ReviewApp` (`tui/app.py`), rendering the terminal and reading
  keypresses, with no idea what a `ReviewOutput` or `Finding` means beyond what it's handed.

The only thing that connects them is one object both flows hold a reference to at the same
time.

### 1. The shared object

It's a single `asyncio.Future` — an empty placeholder with no value yet, that both flows
hold a reference to. `ApprovalRequest` (`tui/schemas.py`) calls its field `pending_response`
(not the bare `future`), because that's what it actually represents from a reader's point
of view: the human's `ApprovalResponse`, pending. Any coroutine holding a reference to it
can `await pending_response` and suspend there until *some other* coroutine, holding that
same reference, calls `pending_response.set_result(value)`. No locking, no polling: the
event loop itself wakes the waiting coroutine back up the instant the result lands. That's
the entire mechanism.

A "park" is one call to `ctx.on_approval_needed(step_name, outcome)` — one per
`StepOutcome` that needs a human, *not* one per finding inside it. `outcome.payload` can
carry several findings at once (e.g. `ReviewOutput.findings` with 3 entries), and all of
them ride through the same single park: 3 blocking findings means 1 park, 1 shared
`pending_response`, and 3 rows inside `FindingBox` for the human to decide one at a time
(see §3) — never 3 separate parks or 3 separate `Future`s.

Exactly one such `Future` is created per approval park, inside `ApprovalRelay.
request_approval` (`approval_relay.py:34-38`):

```python
pending_response: asyncio.Future[ApprovalResponse] = asyncio.get_running_loop().create_future()
await self._queue.put(ApprovalRequest(step_name, outcome, pending_response))
return await pending_response
```

The pipeline flow gets its reference by creating it. The TUI flow gets a reference to that
*same* object by pulling it back out of the `asyncio.Queue` it was queued on, in
`ReviewApp._relay_approval` (`app.py:278`):

```python
request = await self._approval_relay.next_request()
```

`request` is an `ApprovalRequest` -- `request.step_name`, `request.outcome`, and
`request.pending_response` are attribute access from here on, not tuple unpacking. Both
flows are now looking at the same Python object in memory. One is asleep on it (`await
pending_response`); the other, once it has an answer, will wake it up
(`pending_response.set_result(...)`). `ApprovalRelay`'s `Queue` is a supporting actor here,
not the star — it only exists to physically carry the *reference* to that `Future` (bundled
into the `ApprovalRequest` alongside `step_name`/`outcome`) from the pipeline side to the
TUI side once.

(A second, unrelated `Future` also exists purely inside the TUI flow —
`FindingBox._pending`, which `await_decision()` awaits internally so `_resolve_park` has
something to resolve once every row is decided. It is never seen by the pipeline flow;
`_relay_approval` is the code that bridges its result into the one *shared*
`pending_response` above. Don't confuse the two — only `ApprovalRelay`'s `pending_response`
crosses the `pipeline`/`tui` boundary.)

### 2. The trigger on the pipeline side

The pipeline flow blocks and starts waiting for a human the moment a step's `StepOutcome`
says it must. `executor.run_steps` checks this right after a step (or fix round) finishes
(`executor.py:162-172`):

```python
needs_park = outcome.needs_approval or (
    step.supports_fix_round and outcome.auto_fixable and auto_fix_cap_exhausted
)
if not needs_park:
    break
...
response = await round_ctx.on_approval_needed(step_name, outcome)
```

Two things can set `needs_park = True`:
- `outcome.needs_approval` — a step decided a finding is blocking on its own (e.g.
  `ReviewStep` sets this whenever `has_blocking_finding(...)` is true — any finding whose
  `action` resolves to `"ask-user"`).
- for a step that opts into fix rounds (`ReviewStep`), a still-`auto_fixable` outcome whose
  automatic retry budget (`_MAX_AUTO_FIX_ROUNDS = 2`) is exhausted — an auto-fixable
  finding that the agent couldn't resolve on its own within the cap falls through to a
  human instead of looping forever.

`round_ctx.on_approval_needed` is `ApprovalRelay.request_approval` — but `executor.py`
itself never knows that. `StepContext.on_approval_needed` is typed only as
`Callable[[str, StepOutcome], Awaitable[ApprovalResponse]] | None`; `pipeline/` doesn't
import `code_review.tui` anywhere and has no idea a concrete `ApprovalRelay` exists. It's
`cli.py` — the composition root, importing both `pipeline/` and `tui/` — that does the
actual wiring, at construction time, by injecting a bound method into that slot:

```python
approval_relay = ApprovalRelay()  # defined in tui/, built once
ctx = StepContext(
    ...,
    on_approval_needed=approval_relay.request_approval,  # pipeline side: an opaque callable
)
tui_app = ReviewApp(..., approval_relay=approval_relay)  # TUI side: the object itself
```

Neither package reaches into the other directly. `cli.py` hands the *same* `ApprovalRelay`
instance to both sides, in two different forms, which is what lets a call inside
`pipeline/` end up talking to something that physically lives in `tui/` with no import
between them. Calling `round_ctx.on_approval_needed(step_name, outcome)` is what creates
the shared `pending_response` from step 1 and suspends the pipeline flow on it — this line
is the entire "block."

### 3. The mechanism on the TUI side that lets the human answer

The TUI flow has been running this whole time, in a background worker started when
`ReviewApp` mounts: `self.run_worker(self._relay_approval(), group="approval-relay")`
(`app.py:141`). `_relay_approval` loops forever, and once it dequeues a request
(`app.py:266-288`):

```python
findings_box = self.query_one(FindingBox)
response = await findings_box.await_decision()
...
request.pending_response.set_result(response)
```

`FindingBox.await_decision()` is the actual human-input mechanism:

- It flips `self._parked = True`, resets every row's decision, focuses the list, and
  turns the highlighted row's `FindingsSuggestion` column into a live, keyboard-driven
  menu (`Finding.suggestions` plus a trailing "Chat about it" entry).
- Keypresses drive it: confirming a suggestion or submitting chat text calls
  `_record_decision("fix", <text>)`; "s" calls `_record_decision("skip", None)`; "x"
  resolves the whole park immediately as `"abort"`. Each of these builds an
  `ApprovalResponse` for that row alone, held on the row's own `Finding` widget as
  `row_decision`.
- Once every row has a `row_decision`, `_resolve_park` pairs each row into a
  `FindingDecision(finding, response)` and — for more than one row — folds them through
  `describe_finding_decisions` into one combined `ApprovalResponse(decision="fix",
  instructions=<combined text>)` (or `"skip"` if every row skipped). A single-row park
  just reuses that row's own response, unwrapped.
- `_resolve_park` calls `self._pending.set_result(resolution)` — the *internal* `Future`
  from step 1's aside — which is what makes `await findings_box.await_decision()` return.

Back in `_relay_approval`, that returned `response` is what gets handed to
`request.pending_response.set_result(response)` on the *shared* `Future` — waking the
pipeline flow back up at `response = await round_ctx.on_approval_needed(...)`. From there, a
`"fix"` decision becomes a `FixRound` (`round_ctx.with_fix_round(response.instructions)`),
and the step
re-runs with that text folded into its next prompt — the pipeline never receives anything
more structured than the string the TUI flow produced.

See [`AGENTS.md`](./AGENTS.md)'s `FindingBox`/`Finding`/`FindingsSuggestion` sections for
the non-obvious per-widget decisions behind this (why decisions are recorded per row
instead of once per park, cursor preservation on revisit, chat prefill), and
[`pipeline/AGENTS.md`](../pipeline/AGENTS.md) for the executor's side of the
fix-round/auto-fix-round loop this pattern feeds into.

The package boundary is:

- Input: an ordered step-name registry and a live `StepEvent` stream.
- Output: a full-screen terminal render that a human reads while a review runs, plus
  `ReviewApp.error` for the caller to turn a mid-run failure into a real nonzero exit code.

`code-review review` refuses to start this view at all when stdin or stdout isn't a real
TTY (see `cli.py`), rather than attempting a broken or partial render.
