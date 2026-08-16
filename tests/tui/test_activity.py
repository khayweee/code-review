"""Pure unit tests for `ActivityRelay`/`ActivityEvent`, independent of Textual (see
`activity.py`'s module docstring).

No `App`, no `Pilot`, no subprocess -- plain `asyncio`, matching `test_input_relay.py`'s
own style and independence from a running `App`.
"""

from __future__ import annotations

import asyncio

from code_review.pipeline.step import ActivityHandle
from code_review.tui.activity import ActivityRelay
from code_review.tui.schemas import ActivityEvent


def test_activity_queues_a_started_event_on_entry() -> None:
    async def scenario() -> ActivityEvent:
        relay = ActivityRelay()
        async with relay.activity("fetch"):
            return await relay.next_event()

    event = asyncio.run(scenario())

    assert isinstance(event, ActivityEvent)
    assert event.label == "fetch"
    assert event.status == "started"
    assert event.parent_id is None


def test_activity_queues_a_finished_event_on_clean_exit() -> None:
    async def scenario() -> list[ActivityEvent]:
        relay = ActivityRelay()
        async with relay.activity("fetch"):
            pass
        started = await relay.next_event()
        finished = await relay.next_event()
        return [started, finished]

    started, finished = asyncio.run(scenario())

    assert started.status == "started"
    assert finished.status == "finished"
    assert finished.label == "fetch"
    # Same activity, not a fresh id -- the finished event closes the same span the
    # started event opened.
    assert finished.activity_id == started.activity_id


def test_activity_queues_a_finished_event_even_when_the_body_raises() -> None:
    """`activity()`'s "finished" event must still be queued on an exception -- the
    underlying generator's `finally` runs on every exit path, matching `RebaseStep`'s own
    "never leave the repo mid-rebase" non-negotiable-cleanup shape."""

    async def scenario() -> list[ActivityEvent]:
        relay = ActivityRelay()
        try:
            async with relay.activity("fetch"):
                raise RuntimeError("boom")
        except RuntimeError:
            pass
        started = await relay.next_event()
        finished = await relay.next_event()
        return [started, finished]

    started, finished = asyncio.run(scenario())

    assert started.status == "started"
    assert finished.status == "finished"


def test_activity_yields_a_fresh_handle_with_no_error_by_default() -> None:
    async def scenario() -> ActivityHandle | None:
        relay = ActivityRelay()
        async with relay.activity("fetch") as handle:
            return handle

    handle = asyncio.run(scenario())

    assert isinstance(handle, ActivityHandle)
    assert handle.error is None


def test_activity_reports_the_finished_event_as_failed_when_the_block_calls_fail() -> None:
    """`ActivityHandle.fail(detail)`, called from inside the block, carries `detail`
    through to the "finished" `ActivityEvent`'s own `error` -- the "started" event never
    carries one, since the block hasn't run yet when it's queued."""

    async def scenario() -> list[ActivityEvent]:
        relay = ActivityRelay()
        async with relay.activity("git fetch origin") as handle:
            handle.fail("exit 1")
        started = await relay.next_event()
        finished = await relay.next_event()
        return [started, finished]

    started, finished = asyncio.run(scenario())

    assert started.error is None
    assert finished.error == "exit 1"


def test_activity_reports_the_finished_event_with_no_error_when_fail_is_never_called() -> None:
    async def scenario() -> ActivityEvent:
        relay = ActivityRelay()
        async with relay.activity("fetch"):
            pass
        await relay.next_event()  # started
        return await relay.next_event()  # finished

    finished = asyncio.run(scenario())

    assert finished.error is None


def test_activity_relay_invokes_on_event_synchronously_for_started_and_finished() -> None:
    """`on_event` fires right where each event would be queued, for both the "started" and
    "finished" pushes -- proven by asserting it ran before `next_event()` ever needed to be
    awaited (a synchronous list append reflects that ordering directly)."""

    seen: list[ActivityEvent] = []

    async def scenario() -> None:
        relay = ActivityRelay(on_event=seen.append)
        async with relay.activity("fetch"):
            assert len(seen) == 1
            assert seen[0].status == "started"
        assert len(seen) == 2
        assert seen[1].status == "finished"

    asyncio.run(scenario())


def test_activity_relay_on_event_also_sees_the_error_on_a_failed_finish() -> None:
    seen: list[ActivityEvent] = []

    async def scenario() -> None:
        relay = ActivityRelay(on_event=seen.append)
        async with relay.activity("fetch") as handle:
            handle.fail("exit 1")

    asyncio.run(scenario())

    assert seen[1].error == "exit 1"


def test_activity_ids_are_distinct_and_increasing_across_calls() -> None:
    async def scenario() -> list[int]:
        relay = ActivityRelay()
        async with relay.activity("first"):
            pass
        async with relay.activity("second"):
            pass
        ids = []
        for _ in range(4):
            ids.append((await relay.next_event()).activity_id)
        return ids

    started_first, finished_first, started_second, finished_second = asyncio.run(scenario())

    assert started_first == finished_first
    assert started_second == finished_second
    assert started_second != started_first


def test_nested_activity_automatically_records_the_enclosing_one_as_its_parent() -> None:
    """Issue #66's own acceptance criterion: nesting is automatic, not caller-managed --
    no call site ever passes a parent id explicitly."""

    async def scenario() -> list[ActivityEvent]:
        relay = ActivityRelay()
        async with relay.activity("outer"):
            async with relay.activity("inner"):
                pass
        events = []
        for _ in range(4):
            events.append(await relay.next_event())
        return events

    outer_started, inner_started, inner_finished, outer_finished = asyncio.run(scenario())

    assert outer_started.label == "outer"
    assert outer_started.parent_id is None

    assert inner_started.label == "inner"
    assert inner_started.parent_id == outer_started.activity_id

    assert inner_finished.parent_id == outer_started.activity_id
    assert outer_finished.parent_id is None
    assert outer_finished.activity_id == outer_started.activity_id


def test_sibling_activities_do_not_see_each_other_as_parents() -> None:
    """Two activities opened one after another (not nested) both have no parent -- being
    reported through the same relay is not enough to link them; only a genuine `async
    with` nesting should."""

    async def scenario() -> list[ActivityEvent]:
        relay = ActivityRelay()
        async with relay.activity("first"):
            pass
        async with relay.activity("second"):
            pass
        events = []
        for _ in range(4):
            events.append(await relay.next_event())
        return events

    first_started, _first_finished, second_started, _second_finished = asyncio.run(scenario())

    assert first_started.parent_id is None
    assert second_started.parent_id is None


def test_log_queues_a_started_then_finished_event_pair() -> None:
    """`log` is semantically equivalent to `async with relay.activity(label): pass` -- same
    "started"/"finished" pair, same shared `activity_id`, consumable by `next_event()`/
    backfill with no separate code path."""

    async def scenario() -> list[ActivityEvent]:
        relay = ActivityRelay()
        await relay.log("tool call")
        started = await relay.next_event()
        finished = await relay.next_event()
        return [started, finished]

    started, finished = asyncio.run(scenario())

    assert started.status == "started"
    assert started.label == "tool call"
    assert finished.status == "finished"
    assert finished.label == "tool call"
    assert finished.activity_id == started.activity_id
    assert finished.timestamp >= started.timestamp


def test_log_nested_inside_an_activity_records_that_activity_as_its_parent() -> None:
    """A one-shot `log()` call made while another `activity()` block is open (e.g. a tool
    call streamed during an agent-call span) nests under it automatically, exactly like a
    nested `activity()` block would."""

    async def scenario() -> list[ActivityEvent]:
        relay = ActivityRelay()
        async with relay.activity("outer"):
            await relay.log("inner")
        events = []
        for _ in range(4):
            events.append(await relay.next_event())
        return events

    outer_started, inner_started, inner_finished, outer_finished = asyncio.run(scenario())

    assert outer_started.label == "outer"
    assert inner_started.label == "inner"
    assert inner_started.parent_id == outer_started.activity_id
    assert inner_finished.parent_id == outer_started.activity_id
    assert outer_finished.parent_id is None


def test_start_queues_a_started_event_with_the_ambient_activity_as_parent() -> None:
    async def scenario() -> list[ActivityEvent]:
        relay = ActivityRelay()
        async with relay.activity("outer"):
            activity_id = await relay.start("tool call")
            await relay.finish(activity_id, "tool call")
        events = []
        for _ in range(4):
            events.append(await relay.next_event())
        return events

    outer_started, tool_started, tool_finished, _outer_finished = asyncio.run(scenario())

    assert tool_started.status == "started"
    assert tool_started.label == "tool call"
    assert tool_started.parent_id == outer_started.activity_id
    assert tool_finished.activity_id == tool_started.activity_id


def test_start_and_finish_do_not_affect_ambient_nesting_for_later_activities() -> None:
    """`start`/`finish` must not touch the ambient `_current_activity_id` `activity()` uses
    for auto-nesting: a span opened via `start` has no children of its own in this design,
    and a later, unrelated `activity()`/`log()` call must still nest under whatever was
    ambient before `start` ran, not under the still-open (or since-closed) `start` span."""

    async def scenario() -> list[ActivityEvent]:
        relay = ActivityRelay()
        async with relay.activity("outer"):
            activity_id = await relay.start("tool call")
            await relay.log("narration")
            await relay.finish(activity_id, "tool call")
        events = []
        for _ in range(6):
            events.append(await relay.next_event())
        return events

    (
        outer_started,
        _tool_started,
        narration_started,
        _narration_finished,
        _tool_finished,
        _outer_finished,
    ) = asyncio.run(scenario())

    assert narration_started.label == "narration"
    assert narration_started.parent_id == outer_started.activity_id


def test_two_interleaved_start_finish_pairs_share_the_same_enclosing_parent() -> None:
    """Two tool calls opened before either closes (e.g. Claude calling two tools in one
    turn: TOOL_USE, TOOL_USE, TOOL_RESULT, TOOL_RESULT) must both nest under the same
    enclosing activity, never under each other."""

    async def scenario() -> list[ActivityEvent]:
        relay = ActivityRelay()
        async with relay.activity("outer"):
            first_id = await relay.start("first tool")
            second_id = await relay.start("second tool")
            await relay.finish(first_id, "first tool")
            await relay.finish(second_id, "second tool")
        events = []
        for _ in range(6):
            events.append(await relay.next_event())
        return events

    (
        outer_started,
        first_started,
        second_started,
        first_finished,
        second_finished,
        _outer_finished,
    ) = asyncio.run(scenario())

    assert first_started.parent_id == outer_started.activity_id
    assert second_started.parent_id == outer_started.activity_id
    assert first_finished.parent_id == outer_started.activity_id
    assert second_finished.parent_id == outer_started.activity_id
    assert first_started.activity_id != second_started.activity_id


def test_finish_carries_a_real_elapsed_duration() -> None:
    async def scenario() -> list[ActivityEvent]:
        relay = ActivityRelay()
        activity_id = await relay.start("slow tool")
        await asyncio.sleep(0.05)
        await relay.finish(activity_id, "slow tool")
        started = await relay.next_event()
        finished = await relay.next_event()
        return [started, finished]

    started, finished = asyncio.run(scenario())

    assert finished.timestamp - started.timestamp >= 0.05


def test_finish_error_lands_on_the_finished_events_error_field() -> None:
    """Mirrors `ActivityHandle.fail(detail)`'s existing "started" event never carries an
    error, only "finished" does -- convention for the `activity()`/block-shaped path."""

    async def scenario() -> list[ActivityEvent]:
        relay = ActivityRelay()
        activity_id = await relay.start("Tool: Read(/fake/missing.txt)")
        await relay.finish(activity_id, "Tool: Read(/fake/missing.txt)", error="file not found")
        started = await relay.next_event()
        finished = await relay.next_event()
        return [started, finished]

    started, finished = asyncio.run(scenario())

    assert started.error is None
    assert finished.error == "file not found"


def test_next_event_blocks_until_an_activity_is_reported() -> None:
    async def scenario() -> ActivityEvent:
        relay = ActivityRelay()
        next_event_task = asyncio.ensure_future(relay.next_event())

        await asyncio.sleep(0)
        assert not next_event_task.done()

        async with relay.activity("fetch"):
            pass

        return await next_event_task

    event = asyncio.run(scenario())

    assert event.status == "started"
    assert event.label == "fetch"
