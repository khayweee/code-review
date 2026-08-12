# Steps

This package contains the domain operations that make up a code-review run. Each step is a
concrete subclass of [`pipeline.step.Step`](../pipeline/README.md) -- see
[docs/GLOSSARY.md](../../../docs/GLOSSARY.md) for the one-paragraph definition of a step, and
`pipeline/AGENTS.md` for the milestone history behind the contract described here.

## What a Step is

A `Step` is a fixed-interface unit of pipeline work: one class, one method, one input type,
one output type.

```python
class Step(ABC):
    supports_fix_round: ClassVar[bool] = False

    @abstractmethod
    async def run(self, ctx: StepContext) -> StepOutcome: ...

    def get_name(self) -> str: ...  # defaults to type(self).__name__
```

- **Input**: exactly one `StepContext` -- the immutable, per-run bag of dependencies
  (`cwd`, `agent`, `diff`, `intent`, plus the human-in-the-loop callbacks and `fix_round`;
  see `pipeline/step.py`'s docstring for the full field list). A step reads from `ctx`
  directly; it never receives another step's `StepOutcome` as input. This is deliberate --
  see "Steps do not read each other's outcomes" below.
- **Output**: exactly one `StepOutcome` -- `needs_approval: bool`, `auto_fixable: bool`, and
  `findings: object` (a step's own schema, narrowed back by whoever consumes it). This is a
  *report*, not a command: the step never decides whether the run pauses or retries. The
  executor (`pipeline/executor.py`) owns that.
- Concrete steps are typically `@dataclass(frozen=True, slots=True)`, matching the rest of
  this codebase's immutable-value-object convention. An Agent-backed step usually carries an
  `executable: str | Path = "claude"` field as a subprocess test seam.

Steps report facts and requested actions; the executor owns ordering and control flow.

## Steps do not read each other's outcomes

A step's only inputs are `StepContext` fields, never a prior step's `StepOutcome.findings`.
Concretely: `ReviewStep`, `TestSufficiencyStep`, and (once built) `pr.py` each call their own
`wrap_intent(ctx.intent.summary, ctx.intent.source)` off the *shared* `ctx.intent`, rather
than consuming wrapped text some earlier step attached to its outcome. If a later step needs
data from an earlier one, that data must become an explicit new field on `StepContext`
(added by `cli.py` when it builds the context, or by the executor via
`StepContext.with_fix_round(instructions)` for a fix round) -- never threaded through
`StepOutcome`. Getting this backwards means a step downstream of a hypothetical future
outcome-mutating step would silently see stale data (see `steps/AGENTS.md`'s `intent.py`
section for the incident this rule is pinned against).

**The one apparent exception is not cross-step**: after a review-family step reports
findings, a human choosing which findings to act on does not hand its selection to a
*different, later* step. It re-runs the **same** step with `StepContext.fix_round: FixRound`
set (see "Fix rounds" below) -- the step that produced the findings is also the one that
edits the working tree to address them. There is currently no general mechanism for a step
to consume a prior step's structured output; building one is tracked as `pipeline/AGENTS.md`
Open item `#78` (suggestion-selection/`EditStep`), not implemented today.

## Fix rounds (opt-in)

A step can opt into the executor's bounded auto-fix-then-park loop by setting
`supports_fix_round: ClassVar[bool] = True`. When it does:

- If `StepOutcome.auto_fixable` is `True` after a run, the executor re-runs the *same* step
  with `ctx.fix_round = FixRound(instructions=describe_auto_fix_findings(outcome.findings))`
  -- up to `pipeline/executor.py`'s `_MAX_AUTO_FIX_ROUNDS` (currently 2) automatic rounds.
- Once that cap is hit (or the outcome needed approval outright), the run parks for a human;
  a `"fix"` response re-runs the step again with the human's own typed instructions,
  uncapped.
- The step's `run()` only ever branches on `ctx.fix_round is not None` to choose which
  prompt-assembly function to call (e.g. `build_review_fix_prompt` vs `build_review_prompt`)
  -- everything else about the round is identical. The fix-mode prompt tells the agent to
  edit the live working tree and then re-report a fresh `StepOutcome` from scratch, since
  `ctx.diff` was captured once before the run and a prior round's own edits make it stale.

Only opt in if the step actually knows how to build a fix-mode prompt; `supports_fix_round`
defaults to `False` precisely so a step's `auto_fixable=True` is inert unless it does.

## Step modules

| Module | Purpose | Main input | Main output | Status |
|---|---|---|---|---|
| `intent.py` | Represent user intent (the `Intent` dataclass). `IntentStep` confirms the shared intent without calling an Agent. | `ctx.intent` | `Intent` in `StepOutcome.findings` | Implemented |
| `rebase.py` | Update the branch onto the latest default branch before review; conflicts and unpushed local-default commits block for a human. | Checkout and branch state | Updated checkout or a blocking finding | Implemented |
| `review.py` | Check correctness and conformance with intent, returning findings and a required risk verdict in one schema. Supports fix rounds. | Diff plus safely wrapped intent | `ReviewOutput` (findings, risk level, risk rationale) | Implemented |
| `test_sufficiency.py` | Decide whether tests would catch a regression, following the ladder: existing test, focused new test, manual verification, or a finding. Supports fix rounds. | Diff, intent, and the current working tree | `TestSufficiencyOutput` (findings, tested behaviors, artifacts) | Implemented |
| `pr.py` | Assemble PR evidence deterministically and optionally ask an Agent to draft the title and "What Changed" section. | Intent, risk, pipeline evidence, and diff summary | PR title/body and creation result | Not yet built (Milestone 8) |
| `gitutils.py` | Shared `git`-subprocess plumbing (no `Step` subclass; a leaf helper module). | - | - | Implemented |
| `registry.py` | Not a step itself -- the single source of truth for which steps exist, in what order (see "Adding a new Step"). | - | - | Implemented |

Redacting credential-shaped text, defanging prompt delimiters, and building prompts safely
live in [`prompt/`](../prompt/AGENTS.md), not here.

## Shared contracts

| Name | Meaning inside this package |
|---|---|
| `Step` | One operation exposing `async run(ctx) -> StepOutcome`; defined by `pipeline.step`. |
| `StepContext` | The immutable per-run dependencies available to every step: `cwd`, `agent`, `diff`, `intent`, plus `activity_reporter`/`on_approval_needed`/`on_input_needed`/`fix_round`. Steps read shared intent from here rather than from another step's outcome. |
| `StepOutcome` | The step's findings plus flags (`needs_approval`, `auto_fixable`) indicating whether approval or an automatic fix may be appropriate. It is a report, not a command to the executor. |
| `FixRound` | `StepContext.fix_round`'s payload: one `instructions: str`, from either the auto-fix path or a human's typed "fix" response. See "Fix rounds" above. |
| `Intent` | The requested behavior (`summary`) and its provenance (`source`), with confidence and optional session metadata reserved for future inference. |

## Adding a new Step

Every new step touches all of these; skipping one either breaks a test in
`tests/steps/test_registry.py` or leaves the step invisible to the TUI/CLI.

1. **Module**: create `steps/<name>.py`. Define the step's pydantic output schema (a
   `BaseModel`, not this repo's usual frozen dataclass -- it must validate an Agent's
   structured answer) and a `@dataclass(frozen=True, slots=True)` `Step` subclass
   implementing `async def run(self, ctx: StepContext) -> StepOutcome`. Keep the module to
   schema + orchestration only.
2. **Prompt** (Agent-backed steps only): put prompt assembly in `prompt/<name>.py`, never
   inline in the step -- see [`prompt/AGENTS.md`](../prompt/AGENTS.md). Build the wrapped
   intent yourself off `ctx.intent`; don't consume it from another step's outcome. If the
   step will support fix rounds, add a sibling `build_<name>_fix_prompt(ctx)` that asserts
   `ctx.fix_round is not None` and tells the agent to edit the working tree and re-report
   from scratch (mirror `prompt/review.py`'s `build_review_fix_prompt`).
3. **Deterministic fallback**: every Agent-dependent step needs one for when the agent call
   itself fails outright (root `AGENTS.md`'s core invariants).
4. **Register in `registry.py`**, all three in the same commit:
   - Add the class name to `STEP_REGISTRY` at its correct pipeline position (fixed order,
     never reorderable by input).
   - Add the class itself to `IMPLEMENTED_STEPS` -- it must be a same-order prefix of
     `STEP_REGISTRY` (`step_cls().get_name()` must match the corresponding
     `STEP_REGISTRY` entry).
   - Add a friendly name to `STEP_DISPLAY_NAMES`, keyed by the same class name.
   - `tests/steps/test_registry.py` pins all three invariants; run it before anything else.
5. **Fix-round opt-in** (optional): set `supports_fix_round: ClassVar[bool] = True` only if
   step 2 built a fix-mode prompt. See "Fix rounds" above.
6. **Tests**: `tests/steps/test_<name>.py`, following Milestone 13's convention -- a real
   `Step` instance, a real temporary git checkout with a real `git diff`, and the real
   `ClaudeCLI` backend pointed at a fake CLI script. Never mock `Step` or `Agent` themselves.
7. **Docs**: add a row to this README's step-module table, and add the module's own section
   to [`steps/AGENTS.md`](./AGENTS.md) recording any non-obvious design decision (this
   README stays the current-state contract; `AGENTS.md` is the milestone-by-milestone why).

`cli.py` needs no per-step changes for a step that only needs what `StepContext` already
carries -- it builds its step list generically from `IMPLEMENTED_STEPS`
(`steps = [cls() for cls in IMPLEMENTED_STEPS]`).

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
                  (Review, Test sufficiency: fix rounds re-run
                   the same step against a live working tree)
```

The first step is intentionally different: the CLI constructs explicit `Intent` before
execution, and `IntentStep` only reports that same object. Later prompt-producing steps
must call `wrap_intent(ctx.intent.summary, ctx.intent.source)` themselves, imported from
the sibling [`prompt`](../prompt/AGENTS.md) package, so they always use current shared
context and apply identical sanitization regardless of provenance.

Intent, Rebase, Review, and Test sufficiency are implemented and wired into `cli.py` today;
PR is the pipeline's last remaining step (Milestone 8, see
[docs/ROADMAP.md](../../../docs/ROADMAP.md)).
