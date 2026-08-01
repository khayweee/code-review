# AGENTS.md — src/code_review/agent/

Scope: this package only. See the [root AGENTS.md](../../../AGENTS.md) for repo-wide
conventions.

- `RunOpts.executable` is the subprocess test seam. Tests use real fake-CLI processes;
  don't replace this with mocks of `asyncio.create_subprocess_exec`.
- Send prompts through stdin, never argv. Full-diff prompts can exceed the platform's
  per-argument size limit before the subprocess starts.
- Every subprocess starts a new session so its PID is also its process-group ID.
  https://github.com/khayweee/code-review/issues/6 (cancellation must leave no surviving
  processes) will add group-wide cleanup on every exit path; do not regress the spawn
  invariant while that cleanup is pending.
- The Claude CLI's `--output-format json` response is an envelope. Only
  `structured_output` is validated as the caller's schema; usage remains backend metadata.
- Structured-answer extraction and validation stay in `schema.py`. Backend adapters may
  unwrap their own transport envelope, but must not grow a private response parser.
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
