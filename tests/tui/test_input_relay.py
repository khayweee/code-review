"""Pure unit tests for `InputRelay`, independent of Textual (see its module docstring).

No `App`, no `Pilot`, no subprocess -- plain `asyncio`, matching `test_state.py`'s style.
"""

from __future__ import annotations

import asyncio

from code_review.tui.input_relay import InputRelay


def test_request_input_blocks_until_next_request_resolves_its_future() -> None:
    async def scenario() -> str:
        relay = InputRelay()
        request_task = asyncio.ensure_future(relay.request_input("proceed?"))

        # `request_input` should not have resolved yet -- nothing has answered it.
        await asyncio.sleep(0)
        assert not request_task.done()

        prompt, future = await relay.next_request()
        assert prompt == "proceed?"
        assert not future.done()

        future.set_result("yes")
        return await request_task

    answer = asyncio.run(scenario())

    assert answer == "yes"


def test_multiple_queued_requests_are_delivered_in_order() -> None:
    async def scenario() -> list[str]:
        relay = InputRelay()
        first_task = asyncio.ensure_future(relay.request_input("first?"))
        second_task = asyncio.ensure_future(relay.request_input("second?"))

        first_prompt, first_future = await relay.next_request()
        assert first_prompt == "first?"
        second_prompt, second_future = await relay.next_request()
        assert second_prompt == "second?"

        first_future.set_result("first answer")
        second_future.set_result("second answer")

        return [await first_task, await second_task]

    answers = asyncio.run(scenario())

    assert answers == ["first answer", "second answer"]


def test_next_request_blocks_until_a_request_is_queued() -> None:
    async def scenario() -> str:
        relay = InputRelay()
        next_request_task = asyncio.ensure_future(relay.next_request())

        await asyncio.sleep(0)
        assert not next_request_task.done()

        request_task = asyncio.ensure_future(relay.request_input("proceed?"))
        prompt, future = await next_request_task
        assert prompt == "proceed?"

        future.set_result("ok")
        return await request_task

    answer = asyncio.run(scenario())

    assert answer == "ok"
