"""Pure unit tests for `ApprovalRelay`, independent of Textual (see its module docstring).

No `App`, no `Pilot`, no subprocess -- plain `asyncio`, matching `test_input_relay.py`'s
style. Updated for issue #81's `ApprovalResponse` (`pipeline/step.py`), which replaced this
module's own, now-removed `Decision` Literal -- every future here resolves with a full
`ApprovalResponse`, not a bare string.
"""

from __future__ import annotations

import asyncio

from code_review.pipeline.step import ApprovalResponse, StepOutcome
from code_review.tui.approval_relay import ApprovalRelay

_OUTCOME = StepOutcome(needs_approval=True, auto_fixable=False, payload=["a finding"])


def test_request_approval_blocks_until_next_request_resolves_its_future() -> None:
    async def scenario() -> ApprovalResponse:
        relay = ApprovalRelay()
        request_task = asyncio.ensure_future(relay.request_approval("RebaseStep", _OUTCOME))

        # `request_approval` should not have resolved yet -- nothing has answered it.
        await asyncio.sleep(0)
        assert not request_task.done()

        step_name, outcome, future = await relay.next_request()
        assert step_name == "RebaseStep"
        assert outcome is _OUTCOME
        assert not future.done()

        future.set_result(ApprovalResponse(decision="approve"))
        return await request_task

    response = asyncio.run(scenario())

    assert response == ApprovalResponse(decision="approve")


def test_multiple_queued_requests_are_delivered_in_order() -> None:
    async def scenario() -> list[ApprovalResponse]:
        relay = ApprovalRelay()
        first_task = asyncio.ensure_future(relay.request_approval("RebaseStep", _OUTCOME))
        second_task = asyncio.ensure_future(relay.request_approval("ReviewStep", _OUTCOME))

        first_name, _first_outcome, first_future = await relay.next_request()
        assert first_name == "RebaseStep"
        second_name, _second_outcome, second_future = await relay.next_request()
        assert second_name == "ReviewStep"

        first_future.set_result(ApprovalResponse(decision="skip"))
        second_future.set_result(ApprovalResponse(decision="abort"))

        return [await first_task, await second_task]

    responses = asyncio.run(scenario())

    assert responses == [ApprovalResponse(decision="skip"), ApprovalResponse(decision="abort")]


def test_next_request_blocks_until_a_request_is_queued() -> None:
    async def scenario() -> ApprovalResponse:
        relay = ApprovalRelay()
        next_request_task = asyncio.ensure_future(relay.next_request())

        await asyncio.sleep(0)
        assert not next_request_task.done()

        request_task = asyncio.ensure_future(relay.request_approval("RebaseStep", _OUTCOME))
        step_name, outcome, future = await next_request_task
        assert step_name == "RebaseStep"
        assert outcome is _OUTCOME

        future.set_result(ApprovalResponse(decision="approve"))
        return await request_task

    response = asyncio.run(scenario())

    assert response == ApprovalResponse(decision="approve")


def test_fix_response_carries_the_humans_typed_instructions() -> None:
    """Issue #81: a "fix" `ApprovalResponse` carries free-text instructions through the
    same relay round-trip, unlike "approve"/"skip"/"abort" (which never set them)."""

    async def scenario() -> ApprovalResponse:
        relay = ApprovalRelay()
        request_task = asyncio.ensure_future(relay.request_approval("ReviewStep", _OUTCOME))

        _step_name, _outcome, future = await relay.next_request()
        future.set_result(ApprovalResponse(decision="fix", instructions="rename the helper"))

        return await request_task

    response = asyncio.run(scenario())

    assert response == ApprovalResponse(decision="fix", instructions="rename the helper")
