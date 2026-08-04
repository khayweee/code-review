# Agent

This package provides the backend-independent Agent boundary: one prompt in, one
schema-validated result out. Pipeline steps depend on the `Agent` protocol, not on a
specific model provider or CLI. The current backend starts a fresh `claude` subprocess
for every call.

## Subunits

| Subunit | Purpose | Input | Output |
| --- | --- | --- | --- |
| `Agent` | Protocol used by steps: asynchronous `run` plus `close` for teardown. | `RunOpts[T]` | `Result[T]` |
| `RunOpts[T]` | Complete description of one isolated call: prompt, checkout, Pydantic output schema, model, prompt overrides, tool permissions, interactive-input relay, and executable test seam. | Values supplied by a step | Backend call configuration |
| `Result[T]` | Preserve both the validated answer and backend evidence. | Backend response | Typed `output`, original `text`, and optional `Usage` |
| `ClaudeCLI` | Translate `RunOpts` into a non-interactive Claude CLI invocation and unwrap its JSON envelope; routes calls that pinned `permission_mode`/`tools_allowlist` through a stdin-relay loop instead of the default fast path. | `RunOpts[T]`; prompt is sent on stdin | `Result[T]` or an `AgentError` subtype |
| `schema.py` | Extract JSON from bare, fenced, or chatty output, then validate it against the requested schema. | Response text and `type[T]` | JSON value and then `T` |
| `process_group.py` | Terminate the entire subprocess tree on success, failure, or cancellation. | Spawned process/session | Completed bounded cleanup |
| `errors.py` | Distinguish process-start, process-exit, missing-output, schema-validation, and stdin-blocked-with-no-relay failures. | Failure details | Actionable `AgentError` subtype |
| `Usage` | Record backend-reported tokens and cost without inventing missing values. | Optional backend metadata | Values or `None` when unknown |

On any exit path, the backend tears down the process group so child tools do not remain
as orphans. Failures stay separate because callers may respond differently: retrying a
process failure, using a deterministic fallback when no answer is available, or asking a
human when output is invalid are not interchangeable decisions.

The package boundary is:

- Input: `RunOpts[T]`, where `T` is a Pydantic model that makes the expected answer
  explicit and enforceable.
- Output: `Result[T]` with validated structured output, raw transport evidence, and
  optional usage; otherwise a specific `AgentError`.

Retry and switching to another backend are intentionally outside the current adapter and
must remain separate mechanisms. See the project
[glossary](../../../docs/GLOSSARY.md) for Agent, backend, schema, extraction, validation,
and fallback terminology.
