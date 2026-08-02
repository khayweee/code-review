# Steps

This package contains the domain operations that make up a code-review run. Each step
implements the [`Step`](../pipeline/README.md) protocol: it reads the shared
`StepContext`, performs one focused job, and reports a `StepOutcome`. Steps report facts
and requested actions; the pipeline executor owns ordering and control flow.

## Step modules

| Module | Purpose | Main input | Main output | Status |
|---|---|---|---|---|
| `intent.py` | Represent user intent, redact credential-shaped text, defang prompt delimiters, and frame intent safely at each prompt site. `IntentStep` confirms the shared intent without calling an Agent. | `ctx.intent`; or text and provenance for `wrap_intent` | `Intent` in `StepOutcome.findings`; or sanitized prompt text | Implemented |
| `rebase.py` | Update the branch onto the latest default branch before review; conflicts and unpushed local-default commits must block for a human. | Checkout and branch state | Updated checkout or a blocking finding | Planned; module not present yet |
| `review.py` | Check correctness and conformance with intent, returning findings and a required risk verdict in one schema. | Diff plus safely wrapped intent | Findings, risk level, and risk rationale | Planned; design stub only |
| `test_sufficiency.py` | Decide whether tests would catch a regression, following the ladder: existing test, focused new test, manual verification, or honest warning. | Diff, intent, and Review result as needed | Test evidence or findings | Planned; design stub only |
| `pr.py` | Assemble PR evidence deterministically and optionally ask an Agent to draft the title and “What Changed” section. | Intent, risk, pipeline evidence, and diff summary | PR title/body and creation result | Planned; design stub only |

## Shared contracts

| Name | Meaning inside this package |
|---|---|
| `Step` | One operation exposing `async run(ctx) -> StepOutcome`; defined by `pipeline.step`. |
| `StepContext` | The immutable `cwd`, `agent`, `diff`, and `intent` available to every step. Steps read shared intent from here rather than from another step's outcome. |
| `StepOutcome` | The step's findings plus flags indicating whether approval or an automatic fix may be appropriate. It is a report, not a command to the executor. |
| `Intent` | The requested behavior (`summary`) and its provenance (`source`), with confidence and optional session metadata reserved for future inference. |

## Place in the complete pipeline

```text
--intent + branch
       |
       v
    Intent ----> Rebase ----> Review ----> Test sufficiency ----> PR
       |            |            |                |                |
       |            |            +-- risk verdict |                +-- scm/GitHub
       |            +-- current diff              |
       +-- shared via StepContext ----------------+
                         |
                  Agent calls where needed
```

The first step is intentionally different: the CLI constructs explicit `Intent` before
execution, and `IntentStep` only reports that same object. Later prompt-producing steps
must call `wrap_intent(ctx.intent.summary, ctx.intent.source)` themselves so they always
use current shared context and apply identical sanitization regardless of provenance.

Inputs and outputs are deliberately structured:

- Inputs come from `StepContext` and, where needed, earlier pipeline evidence selected by
  the executor. Raw intent is data and must be wrapped before inclusion in a prompt.
- Outputs are `StepOutcome` objects. Agent-backed answers should be schema-validated
  findings, while each Agent-dependent step must also provide a deterministic fallback
  for an outright Agent failure.

The conceptual order above is the target described in the
[roadmap](../../../docs/ROADMAP.md). At present, only the Intent step is executable.
