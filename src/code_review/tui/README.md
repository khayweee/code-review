# TUI

This package is the live, full-screen terminal view of a review run. It renders the fixed
step registry as a "Pipeline" box that updates as [`pipeline`](../pipeline/README.md)'s
executor streams progress, including not-yet-implemented steps as pending placeholders. It
does not decide what steps run or in what order; that stays the executor's job.

## Subunits

| Subunit | Purpose | Input | Output |
|---|---|---|---|
| `StepRow` | One display row: a step's name, current status, and duration. | Derived by `backfill` | Consumed by `PipelineBox` |
| `backfill` | Pure, Textual-independent function turning the `StepEvent`s seen so far into one `StepRow` per registry entry. | `registry`, `events`, `now`, optional `failed_step` | `list[StepRow]` |
| `PipelineBox` | Bordered Textual widget rendering one line per `StepRow`, with a status icon and formatted duration. | `list[StepRow]` via `update_rows` | Rendered terminal content |
| `render_rows`/`format_row`/`format_duration` | Formatting helpers behind `PipelineBox`, unit-testable on their own. | `StepRow`(s) | Plain display strings |
| `ReviewApp` | The Textual `App`: consumes an injected `events` stream in a worker, re-renders `PipelineBox` on every event and on a timer tick, and exits itself when the stream ends or raises. | `registry: Sequence[str]`, `events: AsyncIterator[StepEvent]` | Terminal UI; `self.error` after `run()` returns |

## Place in the complete pipeline

```text
cli.py review
  | builds StepContext, IMPLEMENTED_STEPS
  v
run_steps(steps, ctx)  ------------------------>  AsyncIterator[StepEvent]
                                                          |
                                                          v
                                              ReviewApp(STEP_REGISTRY, events)
                                                          |
                                       worker: for each event -> backfill -> PipelineBox
                                                          |
                                          stream ends or raises -> self.exit()
                                                          |
                                                          v
                                         cli.py checks app.error, sets the CLI exit code
```

`STEP_REGISTRY` (`steps/registry.py`) is the single source of truth for step display
names, ordered; `IMPLEMENTED_STEPS` is the ordered prefix of classes `cli.py` actually
runs. A registry entry with no matching class yet still renders in `PipelineBox` as a
pending placeholder for the whole run — that is how a step lands in the live view without
any `tui/` code change once its class exists.

`ReviewApp` receives `registry` and `events` as constructor arguments rather than
importing them itself. That seam is what lets `tests/tui/` drive `ReviewApp` with
hand-built fake `StepEvent`s through Textual's `Pilot`/`run_test()`, independent of a real
agent subprocess. See [`tui/AGENTS.md`](./AGENTS.md) for the non-obvious decisions behind
that seam, how "failed" is derived rather than reported by `pipeline.step.StepEvent`
itself, and why `state.py` stays free of any Textual import.

The package boundary is:

- Input: an ordered step-name registry and a live `StepEvent` stream.
- Output: a full-screen terminal render that a human reads while a review runs, plus
  `ReviewApp.error` for the caller to turn a mid-run failure into a real nonzero exit code.

`code-review review` refuses to start this view at all when stdin or stdout isn't a real
TTY (see `cli.py`), rather than attempting a broken or partial render.
