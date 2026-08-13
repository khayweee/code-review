# Streaming Agent Observability

This document describes the streaming infrastructure that enables live observation of LLM execution within the pipeline and TUI.

## Architecture

### 1. StreamEvent & StreamEventType

Located in `src/code_review/agent/streaming.py`.

- **StreamEventType**: Enum of observable moments:
  - `TOOL_USE`: Agent called a tool (tool_name, tool_id, input)
  - `TOOL_RESULT`: Tool returned a result (tool_id, output, is_error)
  - `ASSISTANT_TEXT`: Final assistant text response (content)
  - `THINKING`: Extended thinking blocks (content)
  - `ERROR`: Tool execution error (tool_id, message)

- **StreamEvent**: Immutable dataclass carrying:
  - `type: StreamEventType`
  - `payload: dict` – type-specific data
  - `timestamp: float` – when event occurred
  - `session_id: str | None` – backend session ID

### 2. Streaming in RunOpts

`src/code_review/agent/base.py`:

```python
@dataclass
class RunOpts(Generic[OutputT]):
    # ... existing fields ...
    on_stream_event: Callable[[StreamEvent], Awaitable[None]] | None = None
```

- When `None`: silent mode (backward compatible, legacy behavior)
- When set: agent call uses `--verbose --output-format stream-json` and invokes the callback for each event

### 3. ClaudeCLI Streaming Implementation

`src/code_review/agent/claude_cli.py`:

- `_build_args()`: Switches to `stream-json` mode when `opts.on_stream_event` is set
- `_run_streaming()`: Reads NDJSON line-by-line, calls `_parse_stream_line()` for each
- `_parse_stream_line()`: Converts raw stream-json into typed `StreamEvent` instances
- Emits events as soon as they're available (no buffering)

### 4. Pipeline Integration

`src/code_review/pipeline/step.py`:

```python
@dataclass(frozen=True, slots=True)
class StepContext:
    # ... existing fields ...
    on_stream_event: Callable[[StreamEvent], Awaitable[None]] | None = None
```

`src/code_review/agent/streaming_helpers.py`:

```python
async def run_with_streaming(ctx: StepContext, opts: RunOpts[OutputT]) -> Result[OutputT]:
    """Wire streaming from StepContext into agent.run()."""
    opts_with_streaming = RunOpts(..., on_stream_event=ctx.on_stream_event)
    return await ctx.agent.run(opts_with_streaming)
```

**Usage in steps:**

```python
# Instead of:
result = await ctx.agent.run(RunOpts(...))

# Use:
from code_review.agent.streaming_helpers import run_with_streaming
result = await run_with_streaming(ctx, RunOpts(...))
```

### 5. TUI Display

`src/code_review/tui/streaming.py`:

- **StreamRelay**: Adapts `Callable[[StreamEvent], Awaitable[None]]` to post messages to a Textual app
- **StreamViewer**: Textual widget that displays last 20 events with icons
- **StreamEventMessage**: Textual message carrying a StreamEvent

**Integration in app:**

```python
relay = StreamRelay()
relay.attach(app)

step_context = StepContext(
    ...,
    on_stream_event=relay,
)
```

## Data Flow

```
Claude CLI (stream-json)
  ↓ (NDJSON lines)
_run_streaming()
  ↓ (raw JSON)
_parse_stream_line()
  ↓ (StreamEvent)
on_stream_event callback
  ↓ (awaits)
StreamRelay
  ↓ (posts message)
Textual App.post_message()
  ↓ (queued)
StreamEventMessage handler
  ↓ (updates)
StreamViewer widget
  ↓ (renders)
Live TUI display
```

## Example: Adding Streaming to a Step

1. Import the helper:
   ```python
   from code_review.agent.streaming_helpers import run_with_streaming
   ```

2. Replace `ctx.agent.run(opts)` with:
   ```python
   result = await run_with_streaming(ctx, opts)
   ```

3. The streaming callback flows automatically from the pipeline context (if wired).

## Testing

`tests/agent/test_streaming.py`:

- `test_streaming_events_emitted_on_tool_use`: Verifies events are captured
- `test_streaming_backward_compatible_without_callback`: Ensures silent mode still works
- `test_stream_event_has_required_fields`: Validates event structure

Run:
```bash
uv run pytest tests/agent/test_streaming.py -v
```

## Future: Multi-Provider Support

To add streaming for a different LLM provider (e.g., Bedrock, Vertex AI):

1. Implement a new adapter (e.g., `src/code_review/agent/bedrock_adapter.py`)
2. Parse provider's streaming format into same `StreamEvent` types
3. Call `opts.on_stream_event(event)` as events arrive
4. No changes needed to TUI, pipeline, or steps

## Known Limitations

- Streaming requires `--verbose` in Claude CLI; overhead is minimal
- TUI displays last 20 events; older events are rotated out
- `stream-json` mode incompatible with `--permission-mode manual` (stdin relay). Workaround: use auto or skip permissions for streaming runs
