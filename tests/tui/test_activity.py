"""Pure unit tests for `ActivityRelay`/`ActivityEvent`, independent of Textual (see
`activity.py`'s module docstring).

No `App`, no `Pilot`, no subprocess -- plain `asyncio`, matching `test_input_relay.py`'s
own style and independence from a running `App`.
"""

from __future__ import annotations

import asyncio

from code_review.tui.activity import ActivityEvent, ActivityRelay


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
