"""Pure unit tests for `ApprovalRelay`, independent of Textual (see its module docstring).

No `App`, no `Pilot`, no subprocess -- plain `asyncio`, matching `test_input_relay.py`'s
style. Updated for issue #81's `ApprovalResponse` (`pipeline/schemas.py`), which replaced
this module's own, now-removed `Decision` Literal -- every `pending_response` here resolves
with a full `ApprovalResponse`, not a bare string.
"""

from __future__ import annotations

import asyncio

from code_review.pipeline.schemas import ApprovalResponse
from code_review.pipeline.step import StepOutcome
from code_review.tui.approval_relay import ApprovalRelay

_OUTCOME = StepOutcome(needs_approval=True, auto_fixable=False, payload=["a finding"])


def test_request_approval_blocks_until_next_request_resolves_its_pending_response() -> None:
    async def scenario() -> ApprovalResponse:
        relay = ApprovalRelay()
        request_task = asyncio.ensure_future(relay.request_approval("RebaseStep", _OUTCOME))

        # `request_approval` should not have resolved yet -- nothing has answered it.
        await asyncio.sleep(0)
        assert not request_task.done()

        request = await relay.next_request()
        assert request.step_name == "RebaseStep"
        assert request.outcome is _OUTCOME
        assert not request.pending_response.done()

        request.pending_response.set_result(ApprovalResponse(decision="approve"))
        return await request_task

    response = asyncio.run(scenario())

    assert response == ApprovalResponse(decision="approve")


def test_multiple_queued_requests_are_delivered_in_order() -> None:
    async def scenario() -> list[ApprovalResponse]:
        relay = ApprovalRelay()
        first_task = asyncio.ensure_future(relay.request_approval("RebaseStep", _OUTCOME))
        second_task = asyncio.ensure_future(relay.request_approval("ReviewStep", _OUTCOME))

        first_request = await relay.next_request()
        assert first_request.step_name == "RebaseStep"
        second_request = await relay.next_request()
        assert second_request.step_name == "ReviewStep"

        first_request.pending_response.set_result(ApprovalResponse(decision="skip"))
        second_request.pending_response.set_result(ApprovalResponse(decision="abort"))

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
        request = await next_request_task
        assert request.step_name == "RebaseStep"
        assert request.outcome is _OUTCOME

        request.pending_response.set_result(ApprovalResponse(decision="approve"))
        return await request_task

    response = asyncio.run(scenario())

    assert response == ApprovalResponse(decision="approve")


def test_fix_response_carries_the_humans_typed_instructions() -> None:
    """Issue #81: a "fix" `ApprovalResponse` carries free-text instructions through the
    same relay round-trip, unlike "approve"/"skip"/"abort" (which never set them)."""

    async def scenario() -> ApprovalResponse:
        relay = ApprovalRelay()
        request_task = asyncio.ensure_future(relay.request_approval("ReviewStep", _OUTCOME))

        request = await relay.next_request()
        request.pending_response.set_result(
            ApprovalResponse(decision="fix", instructions="rename the helper")
        )

        return await request_task

    response = asyncio.run(scenario())

    assert response == ApprovalResponse(decision="fix", instructions="rename the helper")
