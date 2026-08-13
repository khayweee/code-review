# Streaming Agent Observability

Live observation of an LLM call's individual tool calls, surfaced through the same
activity-pane mechanism the rest of the pipeline already uses — no separate display or
event stream.

## Architecture

### 1. StreamEvent & StreamEventType

`src/code_review/agent/streaming.py` — backend-agnostic, no dependency on `pipeline/` or
`tui/`.

- **StreamEventType**: `TOOL_USE`, `TOOL_RESULT`, `ASSISTANT_TEXT`, `THINKING`, `ERROR`.
- **StreamEvent**: frozen dataclass — `type`, `payload: dict` (type-specific), `timestamp`,
  `session_id: str | None`.

### 2. RunOpts.on_stream_event

`src/code_review/agent/base.py`:

```python
@dataclass
class RunOpts(Generic[OutputT]):
    ...
    on_stream_event: Callable[[StreamEvent], Awaitable[None]] | None = None
```

`None` (the default) keeps `ClaudeCLI` on the legacy `--output-format json` path. Setting
it switches to `--verbose --output-format stream-json` and invokes the callback for each
parsed line.

### 3. ClaudeCLI streaming path

`src/code_review/agent/claude_cli.py`:

- `_build_args` switches to `stream-json`/`--verbose` when `opts.on_stream_event` is set.
- `_run_streaming` reads NDJSON line-by-line, calling `_parse_stream_line` for each line
  and emitting events as they arrive — no buffering. It also captures the one
  `"result"`-type line's parsed object directly, so the final structured
  output/usage are read from that object rather than re-derived from the concatenated
  NDJSON text (a naive brace-match over the raw text would misfire on `{`/`}` characters
  inside a tool's own output, e.g. a `Read` of a JSON file).
- `_parse_stream_line` converts one stream-json line into a `StreamEvent`, or `None` for
  lines with nothing observable. A `tool_result` correlates to its call via the block's
  own `tool_use_id`, not the enclosing message's `parent_tool_use_id` (that field names a
  *subagent's* enclosing tool call, if any — unrelated).

Known limitation: the streaming path takes priority over the stdin-relay path in `run()`,
so streaming and permission-gated calls (`tools_allowlist`/a pinned `permission_mode`)
aren't supported together. Not a current gap in practice — `ReviewStep`, the only caller
that streams, never sets either.

### 4. Wiring into a step: ReviewStep

There is no `StepContext.on_stream_event` field and no separate stream display — a step
that wants to stream builds a relay closure over its own `ctx.activity_reporter` and
passes it straight to `RunOpts`. `src/code_review/steps/review.py`:

- `_tool_activity_label(tool_name, tool_input)` renders one tool call as e.g.
  `Tool: Read(/path/to/file)`.
- `_tool_stream_relay(reporter)` returns an `on_stream_event` callback that opens a nested
  `reporter.activity(label)` span on `TOOL_USE` and closes it on the matching
  `TOOL_RESULT`, keyed by `tool_id` (a `StreamEvent` is a point-in-time callback, not an
  `async with` block, so the span's `AsyncExitStack` is held open in a dict between the
  two calls).
- `ReviewStep.run` builds this relay only when `ctx.activity_reporter is not None`
  (passing `None` otherwise, not a no-op relay, so tests/calls with no reporter stay on
  the legacy JSON path) and passes it to `RunOpts` from inside the existing
  `ctx.report_activity("Agent: reviewing diff via claude")` span, so each tool's activity
  nests under it automatically via `ActivityRelay`'s contextvar-based parent tracking —
  see `src/code_review/tui/activity.py`'s module docstring.

No TUI-layer change was needed: `tui/state.py`'s `backfill_activities` already renders
every activity reported under a step as its own row regardless of nesting depth, so tool
rows just appear alongside the outer "Agent: reviewing..." row.

## Data flow

```
Claude CLI (stream-json)
  ↓ (NDJSON lines)
_run_streaming
  ↓ (raw JSON)
_parse_stream_line
  ↓ (StreamEvent)
_tool_stream_relay (steps/review.py)
  ↓ (nested activity() span)
ActivityRelay
  ↓ (ActivityEvent)
tui/state.py's backfill_activities
  ↓ (renders)
Activity pane
```

## Testing

- `tests/agent/test_streaming.py` — `_parse_stream_line` against real stream-json line
  shapes, and `ClaudeCLI.run`/`_run_streaming` end to end against a fake CLI that emits a
  full NDJSON transcript (`tests/agent/fakes/streaming_tool_call.py`).
- `tests/steps/test_review.py` — `ReviewStep` produces the expected nested `ActivityEvent`s
  for a real streamed tool call (`tests/pipeline/fakes/review_streams_a_tool_call.py`).

Run:
```bash
uv run pytest tests/agent/test_streaming.py tests/steps/test_review.py -v
```

## Adding streaming to another step

Only worth doing for a step that itself calls `ctx.agent.run` with tool use enabled.
Build a relay closure over `ctx.activity_reporter` the same way `_tool_stream_relay` does,
and pass it as `RunOpts.on_stream_event` from inside that step's own
`ctx.report_activity(...)` span so nesting falls out automatically. No new `StepContext`
field, no new TUI widget.

## Future: multi-provider support

To add streaming for a different LLM provider (e.g. Bedrock, Vertex AI):

1. Implement a new `Agent` adapter.
2. Parse that provider's streaming format into the same `StreamEvent` types.
3. Call `opts.on_stream_event(event)` as events arrive.
4. No changes needed anywhere else — pipeline, steps, and TUI are all backend-agnostic.
