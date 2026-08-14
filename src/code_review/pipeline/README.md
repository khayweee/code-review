# Pipeline

This package is the orchestration core. It defines the contract every pipeline step
follows, the shared inputs for one run, and the executor that invokes steps in order. It
does not contain review policy or GitHub-specific behavior; those belong in
[`steps`](../steps/README.md) and [`scm`](../scm/README.md).

## Subunits

| Subunit       | Purpose                                                                                                                                                        | Input                                                  | Output                                                                                        |
| ------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------ | --------------------------------------------------------------------------------------------- |
| `Step`        | Abstract base class subclassed by one unit of pipeline work. Its only abstract operation is asynchronous `run(ctx)`; it also provides a concrete `get_name()`. | `StepContext`                                          | `StepOutcome`                                                                                 |
| `StepContext` | Immutable per-pipeline-run data shared by every step (see `docs/GLOSSARY.md`'s "run" entry).                                                                   | `cwd`, `agent`, `diff`, and `intent`                   | Read by steps; it is not itself transformed                                                   |
| `StepOutcome` | A step's report to the executor.                                                                                                                               | Values chosen by the step                              | `needs_approval`, `auto_fixable`, and step-specific `findings`                                |
| `StepEvent`   | One progress unit `run_steps` yields: a step entering `"running"`, or its `"completed"` report.                                                                | Produced by the executor, not by steps themselves      | `step_name`, `status`, `outcome` (set only when completed), `started_at`/`duration`           |
| `run_steps`   | Executes the supplied steps sequentially, yielding a running/completed event pair per round, and pausing for a human at an approval park.                      | Ordered `list[Step]` and one `StepContext`             | `AsyncIterator[StepEvent]`, two events per round in execution order                           |
| `findings.py` | Shared finding schema (`Finding`), the fail-safe action default, the blocking-findings gate, and fix-loop rendering helpers.                                   | `ReviewOutput`/`TestSufficiencyOutput`-shaped findings | `Finding`, `has_blocking_finding`, `describe_auto_fix_findings`, `describe_finding_decisions` |

`StepOutcome.findings` is currently typed as `object` because each step owns its output
schema. Callers narrow it to the expected type. `run_steps` acts on `needs_approval` and
`auto_fixable` today: a step opted into `supports_fix_round` gets bounded automatic fix
rounds before falling through to a park; `needs_approval` parks outright. See
`pipeline/step.py`'s `FixRound`/`StepContext.fix_round` and `pipeline/AGENTS.md`'s
"Milestone 7" entries for the full loop.

`StepEvent` is what a caller actually iterates today, not `StepOutcome` directly -- pulling
a step's `StepOutcome` out means reading it off that step's `"completed"` event. `step_name`
comes from `step.get_name()`, a concrete method `Step` provides (defaulting to
`type(self).__name__`) that a step can override if it needs a name distinct from its class.
`started_at`/`duration` use `time.monotonic()`, so they measure elapsed time within the run,
not wall-clock time.

## Place in the complete pipeline

```text
CLI
  | builds one StepContext
  v
run_steps([Intent, Rebase, Review, Test sufficiency, PR], ctx)
  |          fixed caller-supplied order, async generator
  +--> yield StepEvent(running)   --> step.run(ctx) --> yield StepEvent(completed, outcome)
  +--> yield StepEvent(running)   --> step.run(ctx) --> yield StepEvent(completed, outcome)
  +--> ...
  v
a live stream of events, two per step, in execution order
```

The intended application supplies the canonical hard-coded step list. `run_steps`
preserves that order; it does not discover, sort, skip, or reorder steps. A step can run
more than once per slot when a fix round fires, but the outer step order itself never
changes. Head continuity remains a later executor milestone (`pipeline/AGENTS.md`'s
"Open" section). A caller that only
wants the final outcomes (e.g. a non-interactive script) drains the stream itself:
`outcomes = [e.outcome async for e in run_steps(steps, ctx) if e.status == "completed"]`.
A caller that wants live progress -- the Milestone 13 TUI (`tui/`, issue #40) -- consumes
each event as it arrives instead of waiting for the whole run, which is the reason
`run_steps` is a generator rather than a coroutine returning a list.

An Agent-dependent step uses the shared dependency like this:

```python
result = await ctx.agent.run(RunOpts(prompt=prompt, cwd=ctx.cwd, output_schema=MyFindings))
return StepOutcome(
    needs_approval=False,
    auto_fixable=False,
    findings=result.output,
)
```

The package therefore has one pipeline-level input and output boundary:

- Input: an ordered set of operations plus the checkout, Agent, diff, and intent for the
  run.
- Output: typed-by-convention reports in the same order. Outcomes are process-local; no
  persistence or resume mechanism exists.

## Execution flow (today)

This section is the shortest end-to-end mental model of object creation and control flow.

1. `cli.py` builds the shared run objects.
   - `Intent(...)` from `--intent`.
   - `ClaudeCLI()` as the `Agent` implementation.
   - `InputRelay()`, and (for an interactive run) an `ActivityRelay` and `ApprovalRelay` so
     backend prompts, sub-step activity, and approval parks can all reach the TUI.
   - `StepContext(cwd, agent, diff, intent, on_input_needed=relay.request_input,
activity_reporter=activity_relay, on_approval_needed=approval_relay.request_approval)`.
   - `steps = [cls() for cls in IMPLEMENTED_STEPS]`.

2. `cli.py` starts orchestration by creating `run_steps(steps, ctx)`.
   - This returns an async event stream (`AsyncIterator[StepEvent]`).
   - The TUI consumes this stream live.

3. The executor runs each step in fixed order, and each step's slot can loop over more
   than one round.
   - For each round: emit `StepEvent(status="running")`, call `await step.run(round_ctx)`,
     emit `StepEvent(status="completed", outcome=StepOutcome(...))`.
   - If the step opted into `supports_fix_round` and the outcome is `auto_fixable`, the
     executor builds a `FixRound` and re-runs the same step (bounded by
     `_MAX_AUTO_FIX_ROUNDS`) before moving to the next step.
   - Step order across slots is still fixed and unconditional; only the round count within
     a slot varies.

4. Inside each round, control is step-local.
   - Agent-dependent step: constructs its own `RunOpts(...)` (including `output_schema`),
     branching only on whether `round_ctx.fix_round is not None` to pick a fix-mode prompt,
     and calls `await round_ctx.agent.run(opts)`.
   - Non-agent step: executes local logic directly (for example intent passthrough or git
     orchestration).

5. Each round returns one `StepOutcome`.
   - `findings`: step-specific output payload.
   - `needs_approval` and `auto_fixable`: consulted by the executor to decide the next round
     (see step 3) and whether to park.

6. Findings drive real executor branching today.
   - A step maps a blocking finding (any resolved `ask-user` action) to
     `StepOutcome.needs_approval = True`, and an `auto-fix`-actioned finding to
     `auto_fixable = True`.
   - The executor parks on `needs_approval`, or on a still-`auto_fixable` outcome once the
     automatic fix-round cap is exhausted, awaiting `ctx.on_approval_needed(step_name,
outcome)` for an "approve"/"skip"/"abort"/"fix" decision. See `pipeline/AGENTS.md`'s
     "Milestone 7" entries for the full loop and `executor.py`'s module docstring for the
     exact state machine.

7. Permission prompts are backend-level I/O, not finding-level control flow.
   - If a call runs with skip-permissions, no interactive prompt path is used.
   - If a call opts into permission handling and the backend needs input, `on_input_needed`
     relays the prompt through `InputRelay` to the TUI, and the step waits for the answer.

## Reporting sub-step activity

A step's own row in the TUI is one `StepEvent`. Anything finer-grained -- a `git` call, an
agent-call span, one streamed tool call -- is a separate, second event stream: an
"activity," reported through `ActivityReporter` (the structural `Protocol` this package
defines) and rendered as nested lines under the owning step's row. `pipeline/`/`steps/`
never import `tui/` directly; `tui.activity.ActivityRelay` satisfies `ActivityReporter`
purely structurally, wired in by `cli.py`.

There are two independent choices, never more: **shape** (does this have a duration worth
timing, or is it one already-finished moment?) and **access** (do you have a `StepContext`
in hand, or only a reporter read implicitly from a shared slot -- see "No `ctx`" below).
That gives four named entry points, all funnelling through the same two null-safe
primitives in `step.py`:

|                | Block (has a start and an end)                     | One-shot (a point in time)            |
| -------------- | -------------------------------------------------- | ------------------------------------- |
| **Have `ctx`** | `async with ctx.report_activity(label): ...`       | `await ctx.log(label)`                |
| **No `ctx`**   | `async with report_activity(reporter, label): ...` | `await log_activity(reporter, label)` |

`ctx.report_activity`/`ctx.log` are thin `self.activity_reporter`-bound wrappers around the
module-level `report_activity`/`log_activity` -- same implementation, just bound to `ctx`
instead of taking the reporter as an argument. Both free functions are null-safe: pass a
`None` reporter and they're a no-op, so a caller never has to branch on whether one is
attached.

How to choose:

1. **Shape, first.** A block naturally pairs a "started" and a "finished" event and reports
   a duration -- use it for anything with real elapsed time (a subprocess call, an
   agent-call span). A one-shot has no duration to track -- use it for something that has
   already happened by the time you find out about it (e.g. one `TOOL_USE` callback from a
   streamed agent run).
2. **Access, second -- default to `ctx`.** Inside `Step.run(ctx)`, or any function that
   already receives `ctx`, always use `ctx.report_activity`/`ctx.log`. Only reach for the
   free functions (`report_activity(current_activity_reporter.get(), label)` /
   `log_activity(...)`) in shared helper code with no `StepContext` parameter at all.
   `current_activity_reporter` is a `contextvars.ContextVar` that `executor.run_steps` sets
   right before calling `step.run(ctx)` and clears right after -- so any code running during
   that window can call `.get()` and read the current step's reporter, even several function
   calls deep, without `ctx` or the reporter ever being passed to it as an argument. Today's
   two users are `steps/gitutils.py`'s `run_git` and `scm/github.py`'s `_run_gh`, both called
   from deep inside step logic, where adding `ctx` as a parameter to every helper along the
   way just to reach them would be unnecessary threading. This is a documented, narrow
   exception, not a second style to pick freely -- a further case like it is a signal
   `StepContext` itself should grow instead (see `pipeline/AGENTS.md`'s Milestone 14 entry).

Never construct `ActivityEvent` or reach for an `ActivityRelay` instance directly outside
`tui/activity.py` -- always go through one of the four entry points above.

```python
# Block, ctx-bound -- the common case inside a step.
async with ctx.report_activity("Agent: reviewing diff via claude"):
    result = await ctx.agent.run(opts)

# One-shot, ctx-bound -- a single already-finished event.
await ctx.log("Tool: Read(/path/to/file)")

# Block, no ctx -- shared helper code with no StepContext to read from.
async with report_activity(current_activity_reporter.get(), label) as activity:
    process = await asyncio.create_subprocess_exec("git", *args, ...)
    if process.returncode != 0:
        activity.fail(f"exit {process.returncode}")
```

**Pass/fail signal**: `activity()`/`report_activity(...)` yield an `ActivityHandle`
(`error: str | None = None`, `.fail(detail)`), so a block can mark its own "finished" event
as failed without raising -- `run_git`/`_run_gh` do this on a nonzero subprocess exit, since
neither ever raises on an ordinary command failure (see their own docstrings). `.fail(...)`
is always safe to call: the no-reporter `nullcontext` branch yields a real (just
unread-by-anything) `ActivityHandle` too, so no call site has to branch on whether a
reporter is attached first. The TUI renders a failed activity's own line with the `"failed"`
status icon plus that error text as its `detail` (`tui/state.py`'s `backfill_activities`,
`tui/widgets/pipeline_box.py`'s `format_activity_row`) -- see `pipeline/AGENTS.md`'s
Milestone 14 entry for `ActivityRow`'s own history.
