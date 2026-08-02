# AGENTS.md — src/code_review/agent/

Scope: this package only. See the [root AGENTS.md](../../../AGENTS.md) for repo-wide
conventions.

- `RunOpts.executable` is the subprocess test seam. Tests use real fake-CLI processes;
  don't replace this with mocks of `asyncio.create_subprocess_exec`.
- Send prompts through stdin, never argv. Full-diff prompts can exceed the platform's
  per-argument size limit before the subprocess starts.
- Every subprocess starts a new session so its PID is also its process-group ID. Do not
  regress this invariant - it's what lets `process_group.terminate_process_group` signal
  the whole group, not just the direct child.
- Process-group teardown lives in `process_group.py`, not the adapter, for the same
  reason structured-output extraction lives in `schema.py`: it's backend-agnostic, so a
  second backend inherits the same cancellation-safety guarantee instead of growing its
  own kill logic. https://github.com/khayweee/code-review/issues/6 (cancellation must
  leave no surviving processes) is implemented there: `terminate_process_group` sends
  SIGTERM to the process group, polls a bounded deadline, escalates to SIGKILL if the
  group is still alive, then reaps the direct child. A backend adapter wraps its
  subprocess-communication call in `try`/`finally` and awaits
  `terminate_process_group(process)` in the `finally` - on every exit path (success,
  non-zero exit, a parse/validation failure, or cancellation), not only the
  cancellation branch.
- The Claude CLI's `--output-format json` response is an envelope. Only
  `structured_output` is validated as the caller's schema; usage remains backend metadata.
- Structured-answer extraction and validation stay in `schema.py`. Backend adapters may
  unwrap their own transport envelope, but must not grow a private response parser.
- `extract_json` tries a fixed, documented order and stops at the first strategy that
  parses: the whole response as JSON, then a fenced code block, then the last balanced
  `{...}` span in the text. Don't reorder or add a strategy without updating both the
  order here and the docstring in `schema.py`.
- Failures are four distinct types in `errors.py`, not one generic exception, because a
  step author's remedy differs by which stage broke: `ProcessStartError` (the subprocess
  never started), `ProcessExitError` (it started but exited non-zero, carries the exit
  status and captured stderr), `NoStructuredOutputError` (no JSON answer was found
  anywhere, including a Claude envelope missing `structured_output`), and
  `OutputValidationError` (a structured answer was found but failed the caller's schema).
  A backend adapter's envelope-unwrap failures should still raise the shared
  `NoStructuredOutputError` rather than inventing a backend-local error.
- Retry (same backend, transient failure) and backend fallback (different backend,
  unavailability) remain separate mechanisms and are both out of scope for this package.
- `RunOpts.permission_mode` defaults to `None`, meaning the caller has not pinned a
  mode. With no `tools_allowlist` and no pinned mode, the backend appends
  `--dangerously-skip-permissions`, mirroring no-mistakes' `claudeAgent.buildArgs`
  opt-out pattern: skip permission checks by default, back off only when the caller
  asked for something specific. Setting `permission_mode` explicitly opts out of that
  default. A non-empty `tools_allowlist` instead sends `--allowedTools` plus
  `--permission-mode` (`permission_mode` or `"auto"`), scoping the call to that list
  instead of skipping permissions entirely.
- Prefer `RunOpts.append_system_prompt` for step instructions (e.g. "classify this
  diff's risk"). It layers on top of Claude's default system prompt. `system_prompt`
  discards that default entirely and should only be used when a step needs full control
  over the model's behavior, not for ordinary task instructions.
- `RunOpts.on_input_needed` (issue #41) is the interactive-input relay seam: once a caller
  sets `permission_mode` (or a `tools_allowlist`), `_build_args` no longer appends
  `--dangerously-skip-permissions`, and `claude_cli.py` routes that call through a second,
  hand-rolled read/write loop (`_run_with_stdin_relay`) instead of the default
  `process.communicate()` fast path -- see that function's docstring for the exact
  read/write loop and why stdin is written but never closed until the subprocess itself
  signals it's done (EOF on stdout). Every call that leaves `permission_mode` at its
  default `None` stays on the untouched `communicate()` path, byte-for-byte identical to
  before this seam existed. A stall with no `on_input_needed` supplied raises
  `StdinBlockedError` after `_STDIN_IDLE_TIMEOUT_SECONDS` (30s, module-level, shrink via
  monkeypatch in tests) rather than hanging or fabricating an answer. **Known limitation**:
  this path is only exercised against the fake CLI test double
  (`tests/agent/fakes/blocks_on_stdin.py`); its prompt-detection framing (an idle-timeout
  heuristic on stdout) has not been validated against the real `claude` CLI's actual
  stdin-blocking behavior.
- Test fakes that read the whole prompt from stdin (`_shared.py`'s `read_prompt`) must
  branch on whether `--dangerously-skip-permissions` is in `sys.argv`: present means the
  parent used `communicate()` and sent EOF, so a blocking `sys.stdin.read()` is correct;
  absent means the parent is on the stdin-relay path and will never send EOF, so the fake
  must drain whatever's already arrived instead (`drain_available_stdin`) or it will hang
  forever waiting for an EOF that never comes.
