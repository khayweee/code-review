# Pipeline

This package is the orchestration core. It defines the contract every pipeline step
follows, the shared inputs for one run, and the executor that invokes steps in order. It
does not contain review policy or GitHub-specific behavior; those belong in
[`steps`](../steps/README.md) and [`scm`](../scm/README.md).

## Subunits

| Subunit | Purpose | Input | Output |
|---|---|---|---|
| `Step` | Protocol implemented by one unit of pipeline work. Its only operation is asynchronous `run(ctx)`. | `StepContext` | `StepOutcome` |
| `StepContext` | Immutable per-run data shared by every step. | `cwd`, `agent`, `diff`, and `intent` | Read by steps; it is not itself transformed |
| `StepOutcome` | A step's report to the executor. | Values chosen by the step | `needs_approval`, `auto_fixable`, and step-specific `findings` |
| `run_steps` | Executes the supplied steps sequentially and retains their outcomes in memory. | Ordered `list[Step]` and one `StepContext` | `list[StepOutcome]` in execution order |
| `findings.py` | Reserved home for the shared finding schema and fix-loop helpers. | Not implemented yet | Not implemented yet |

`StepOutcome.findings` is currently typed as `object` because each step owns its output
schema. Callers narrow it to the expected type. The approval and auto-fix flags are
reported now, but the executor does not act on them yet.

## Place in the complete pipeline

```text
CLI
  | builds one StepContext
  v
run_steps([Intent, Rebase, Review, Test sufficiency, PR], ctx)
  |          fixed caller-supplied order
  +--> step.run(ctx) --> StepOutcome
  +--> step.run(ctx) --> StepOutcome
  +--> ...
  v
ordered list of outcomes
```

The intended application supplies the canonical hard-coded step list. `run_steps`
preserves that order; it does not discover, sort, skip, or reorder steps. Today it runs
every supplied step unconditionally. Branching for blocking findings, automatic fixes,
human approval, and head continuity are later executor milestones.

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
