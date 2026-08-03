# Pipeline

This package is the orchestration core. It defines the contract every pipeline step
follows, the shared inputs for one run, and the executor that invokes steps in order. It
does not contain review policy or GitHub-specific behavior; those belong in
[`steps`](../steps/README.md) and [`scm`](../scm/README.md).

## Subunits

| Subunit | Purpose | Input | Output |
| --- | --- | --- | --- |
| `Step` | Abstract base class subclassed by one unit of pipeline work. Its only abstract operation is asynchronous `run(ctx)`; it also provides a concrete `get_name()`. | `StepContext` | `StepOutcome` |
| `StepContext` | Immutable per-run data shared by every step. | `cwd`, `agent`, `diff`, and `intent` | Read by steps; it is not itself transformed |
| `StepOutcome` | A step's report to the executor. | Values chosen by the step | `needs_approval`, `auto_fixable`, and step-specific `findings` |
| `StepEvent` | One progress unit `run_steps` yields: a step entering `"running"`, or its `"completed"` report. | Produced by the executor, not by steps themselves | `step_name`, `status`, `outcome` (set only when completed), `started_at`/`duration` |
| `run_steps` | Executes the supplied steps sequentially, yielding a running/completed event pair per step as it goes. | Ordered `list[Step]` and one `StepContext` | `AsyncIterator[StepEvent]`, two events per step in execution order |
| `findings.py` | Reserved home for the shared finding schema and fix-loop helpers. | Not implemented yet | Not implemented yet |

`StepOutcome.findings` is currently typed as `object` because each step owns its output
schema. Callers narrow it to the expected type. The approval and auto-fix flags are
reported now, but the executor does not act on them yet.

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
preserves that order; it does not discover, sort, skip, or reorder steps. Today it runs
every supplied step unconditionally. Branching for blocking findings, automatic fixes,
human approval, and head continuity are later executor milestones. A caller that only
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
   - `InputRelay()` so backend prompts can be relayed to the TUI when needed.
   - `StepContext(cwd, agent, diff, intent, on_input_needed=relay.request_input)`.
   - `steps = [cls() for cls in IMPLEMENTED_STEPS]`.

2. `cli.py` starts orchestration by creating `run_steps(steps, ctx)`.
   - This returns an async event stream (`AsyncIterator[StepEvent]`).
   - The TUI consumes this stream live.

3. The executor runs each step in fixed order.
   - For each step: emit `StepEvent(status="running")`.
   - Call `await step.run(ctx)`.
   - Emit `StepEvent(status="completed", outcome=StepOutcome(...))`.
   - Current behavior: every supplied step runs unconditionally; no branching yet.

4. Inside each step, control is step-local.
   - Agent-dependent step: constructs its own `RunOpts(...)` (including `output_schema`) and
     calls `await ctx.agent.run(opts)`.
   - Non-agent step: executes local logic directly (for example intent passthrough or git
     orchestration).

5. Each step returns one `StepOutcome`.
   - `findings`: step-specific output payload.
   - `needs_approval` and `auto_fixable`: flags reported now for later milestones.

6. Findings influence step flags, not executor branching (yet).
   - Example: a step can map a finding action like `ask-user` to
     `StepOutcome.needs_approval = True`.
   - Current executor does not pause/park on this flag yet; that logic is a later milestone.

7. Permission prompts are backend-level I/O, not finding-level control flow.
   - If a call runs with skip-permissions, no interactive prompt path is used.
   - If a call opts into permission handling and the backend needs input, `on_input_needed`
     relays the prompt through `InputRelay` to the TUI, and the step waits for the answer.
