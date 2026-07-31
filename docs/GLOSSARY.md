# Glossary

The vocabulary this project uses in code, issues, and docs. Only terms you need in order
to follow how the library works are here. If a word is obvious from ordinary English or
from reading one function, it does not belong in this file.

**One owner per fact**: this file owns what words mean, [`ROADMAP.md`](ROADMAP.md) owns why
the design is the way it is, and GitHub issues own status and sequencing. Define a term
here once and link to it rather than re-explaining it.

## The shape of the system

One command runs a fixed sequence of steps over a diff. Each step composes a prompt, hands
it to an agent, and gets a typed answer back that code can branch on.

```
code-review review <branch> --intent "..."
        |
        v
   Executor  ---- runs Steps in a fixed, hard-coded order ---->  Intent
        |                                                        Review
        | each Step needs an answer                              Test sufficiency
        v                                                        PR
      Agent          <- the abstraction: one call in, one result out
        |
        v
     Backend         <- a concrete implementation, e.g. the `claude` CLI adapter
        |
        v
  a subprocess, whose stdout is parsed into a schema-validated object
```

The two directions that matter: steps only ever talk *down* to the `Agent` abstraction and
never to a backend, and the executor decides ordering and human approval, never a step.

## The agent layer

**Agent**
: The abstraction every step is written against. Its contract is deliberately tiny: **one
call in, one result out** (`run`, plus a `close` for teardown). No streaming, no
conversation state, no multi-turn memory. A step that needs three answers makes three
calls. Keeping the contract this small is what makes the layer swappable.

**Backend**
: A concrete implementation of `Agent`. Where `Agent` says *what* you can ask for, a
backend is the thing that actually does it: takes the prompt, produces the answer, and
knows the mechanics involved. The first (and currently only) backend shells out to the
`claude` CLI as a subprocess per call and reads structured output from its stdout. A
future backend could call an HTTP API, drive a different CLI, or replay fixtures in tests.
Nothing above this layer knows or cares which one is in use. The module implementing a
backend is also called its **adapter**.

**Structured output**
: The answer as a typed object rather than prose. A step must be able to branch on
`finding.action` or `risk.level`; it cannot branch on a paragraph of English. Everything
this library does depends on answers being parseable.

**Schema**
: The declared shape of an answer, as a pydantic model, passed in with the prompt. It does
double duty: it tells the agent what to produce, and it lets a required field be *enforced*
after the fact. That distinction is load-bearing. "Please always include a risk level" is a
request an agent can miss; a required schema field is a validation error it cannot.

**Extraction** and **validation**
: The two separate stages of turning a response into an object, with separate failures.
*Extraction* finds the JSON in a reply that may be bare JSON, a fenced block, or an object
buried in prose. *Validation* checks the extracted JSON against the schema. "I found no
JSON anywhere" and "I found JSON of the wrong shape" call for different responses, so they
are never collapsed into one error.

**Process group** and **orphan**
: A spawned agent starts its own children: test runners, build watchers, git. Terminating
the direct child leaves those grandchildren running as **orphans**, still holding file
handles and the working directory. The fix is to spawn each call into its own **process
group** and terminate the whole group, so cleanup reaches the entire tree rather than just
the top of it.

## The pipeline layer

**Step**
: One unit of pipeline work with a fixed interface: it receives a context describing the
run, does its job (usually one agent call), and returns an outcome. The four steps are
intent, review, test sufficiency, and PR.

**Executor**
: The loop that runs the steps and owns everything *between* them: ordering, whether to
stop, whether to attempt a fix, and whether to hand control to a human. Steps report; the
executor decides.

**Step order**
: The sequence of steps, hard-coded in the executor. Not configurable, not data-driven, not
reorderable by any input. This is a safety property, not a simplification: "nothing reaches
a PR without being reviewed first" only holds if nothing can rearrange the order.

**Round**
: One pass of the fix loop over a step's findings. Automatic fix rounds are **bounded** (a
capped number of attempts, then stop), while rounds gated on a human are **unbounded** (a
person can iterate as long as they like). The asymmetry is intentional: a loop with no
human in it needs a hard stop, a loop with a human in it already has one.

**Park**
: To suspend the run and wait for a person, rather than failing or proceeding. The state a
finding lands in when it needs judgement the pipeline will not exercise on its own.

**Approval gate**
: The point where a parked run waits for a human decision. Compare **the gate** under
[overloaded words](#words-this-repo-overloads) below, which is a different thing entirely.

## The review vocabulary

**Intent**
: What the change was *meant* to achieve, as opposed to what it does. Supplied explicitly
via `--intent` for now; inferring it from a session transcript is a later milestone. It is
the reference point the review measures against, which is what makes "correct, but not what
you were trying to do" an expressible verdict.

**Finding**
: One structured observation from a review: what is wrong, where, how serious, and what
should happen about it. Findings are the review's output. Prose is not.

**Action**
: The field on a finding saying what to do about it, principally *fix it automatically* or
*ask a human*. Its default when unclassified is always to ask a human. See **fail-safe
default**.

**Blocking finding**
: A finding severe enough to stop the run from proceeding to later steps. The check for
these is shared rather than reimplemented per step, so "what counts as blocking" has one
answer.

**Risk verdict**
: A required level plus a written rationale, returned as fields on the *review's own*
schema rather than produced by a separate step. Putting them on the same schema means the
review structurally cannot come back without a risk assessment.

**Test sufficiency**
: The question "would the existing tests catch it if this change broke?", which is distinct
from "do tests pass" and from "is coverage high". Answered via a **decision ladder**: an
existing test already covers it, or write one, or describe manual verification, or state
honestly that the change is unverified. The last rung exists so the step never has to
choose between lying and failing.

## Safety rules that are also vocabulary

**Fail-safe default**
: When something is unclassified, unknown, or unset, the default points toward a human,
never toward proceeding automatically. Getting this backwards is a fail-open hole: the
pipeline would silently take action precisely in the cases it understood least.

**Deterministic fallback**
: A non-agent code path that produces a usable result when an agent call fails outright,
for example building a PR body from `git diff --name-status` when drafting fails. Every
step that depends on an agent needs one, so that an agent outage degrades the output rather
than failing the run.

**Provenance**
: Where a piece of input came from, such as intent typed by the user versus intent inferred
from a transcript. Provenance changes how much **trust weight** the input carries, and
never whether sanitisation applies. Both kinds get identically redacted and wrapped before
going into a prompt; only the framing of authority differs.

**Trusted vs descriptive config**
: The split that config is partitioned along, by one question: can this field cause code to
execute? Trusted fields (hooks, commands) must come from a source pinned to an exact commit
fetched fresh. Descriptive fields (wording, thresholds) can come from the pushed branch.
Deferred until the tool is exposed to contributors other than the repo owner.

## Words this repo overloads

Four words mean more than one thing here. These are the ones that actually cause confusion.

**agent**
: (1) The `Agent` abstraction in this library. (2) A coding-agent CLI such as `claude`,
which a backend drives. (3) An AI assistant working in this repo, which is who
[`AGENTS.md`](../AGENTS.md) addresses. Capitalised `Agent` always means sense 1.

**fallback**
: (1) *Backend fallback*: trying a different backend when one is unavailable. Not built,
and deliberately kept separate from **retry**, which re-attempts the *same* backend after a
transient error. Same-looking loop, different causes, different correct behaviour.
(2) *Deterministic fallback*: the non-agent code path described above. Unqualified
"fallback" in this repo usually means sense 2.

**review**
: (1) What the whole tool does. (2) The specific Review step, which covers correctness and
risk. Prefer "the Review step" when you mean sense 2.

**gate**
: (1) An *approval gate*: the in-pipeline pause for a human. Being built. (2) *The gate*:
the bare-repo-plus-git-hooks trigger design from the Go tool this project learns from,
where pushes are intercepted locally and forwarded to GitHub only after the pipeline
passes. **Deliberately not built here**, because this project triggers from a plain
foreground CLI command instead, which removes about six subsystems' worth of work. See
[`GATE-MODEL.md`](GATE-MODEL.md).
