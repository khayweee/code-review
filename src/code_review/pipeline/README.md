# Pipeline

This package is the orchestration core. It defines the contract every pipeline step
follows, the shared inputs for one run, and the executor that invokes steps in order. It
does not contain review policy or GitHub-specific behavior; those belong in
[`steps`](../steps/README.md) and [`scm`](../scm/README.md).

## Subunits

| Subunit | Purpose | Input | Output |
|---|---|---|---|
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

See the project [glossary](../../../docs/GLOSSARY.md) for the precise meanings of Step,
finding, park, and approval gate.
