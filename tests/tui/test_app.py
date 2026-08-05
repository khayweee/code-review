"""`ReviewApp` tests, driven with fake `StepEvent`s through Textual's `Pilot`/`run_test()`.

No `run_steps`, no `StepContext`, no `Agent`/`ClaudeCLI` subprocess anywhere here -- every
scenario below hands `ReviewApp` a hand-written async generator yielding `StepEvent`s
directly, per this issue's acceptance criteria (see `app.py`'s module docstring for why the
constructor takes `events: AsyncIterator[StepEvent]` rather than building its own stream).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from io import StringIO

from rich.console import Console
from textual.pilot import Pilot
from textual.widgets import Input, OptionList, Static

from code_review.pipeline.executor import RunAbortedError
from code_review.pipeline.findings import Finding
from code_review.pipeline.step import ApprovalResponse, StepEvent, StepOutcome
from code_review.steps.review import ReviewOutput
from code_review.steps.test_sufficiency import TestSufficiencyOutput
from code_review.tui.activity import ActivityEvent, ActivityRelay
from code_review.tui.app import ReviewApp, _tag_activity_events
from code_review.tui.approval_relay import ApprovalRelay
from code_review.tui.input_relay import InputRelay
from code_review.tui.screens import InputPromptScreen
from code_review.tui.widgets import FindingsBox, PipelineBox, StatusBox

REGISTRY = ("IntentStep", "RebaseStep", "ReviewStep")


def _pipeline_box_content(box: PipelineBox) -> str:
    """Render `PipelineBox.content` (a Rich `Table`, see `widgets.py`'s `render_rows_live`)
    to plain text the same way a real terminal would, so assertions here can keep
    comparing readable strings instead of a `Table` object."""

    buffer = StringIO()
    Console(file=buffer, force_terminal=True, width=80, color_system=None).print(box.content)
    return buffer.getvalue().rstrip()


def _findings_box_content(box: FindingsBox) -> str:
    """Render every option in `box`'s child `OptionList` to plain text, one line each,
    joined -- `FindingsBox` is a `Vertical` with an `OptionList` child, not a single
    renderable `.content` the way `PipelineBox` is (issue #88), so this renders each
    option's `prompt` individually rather than reading one shared attribute."""

    buffer = StringIO()
    console = Console(file=buffer, force_terminal=True, width=80, color_system=None)
    for option in box.query_one(OptionList).options:
        console.print(option.prompt)
    return buffer.getvalue().rstrip()


async def _wait_until_done(pilot: Pilot[None], app: ReviewApp) -> None:
    """Poll until `app` marks its run done (a `StatusBox` mounted -- see `app.py`'s
    `_render_status`), replacing the old "wait until `is_running` goes False" pattern now
    that the app deliberately stays alive, showing a Status box, until "e" is pressed."""

    for _ in range(20):
        if list(app.query(StatusBox)):
            return
        await pilot.pause()
        await asyncio.sleep(0.01)
    raise AssertionError("ReviewApp never reached its done state (no StatusBox mounted)")


_OUTCOME = StepOutcome(needs_approval=False, auto_fixable=False, findings=None)


async def _one_step_completes() -> AsyncIterator[StepEvent]:
    started = time.monotonic()
    yield StepEvent(
        step_name="IntentStep", status="running", outcome=None, started_at=started, duration=None
    )
    await asyncio.sleep(0)
    yield StepEvent(
        step_name="IntentStep",
        status="completed",
        outcome=_OUTCOME,
        started_at=started,
        duration=0.01,
    )


async def _second_step_raises() -> AsyncIterator[StepEvent]:
    started = time.monotonic()
    yield StepEvent(
        step_name="IntentStep", status="running", outcome=None, started_at=started, duration=None
    )
    await asyncio.sleep(0)
    yield StepEvent(
        step_name="IntentStep",
        status="completed",
        outcome=_OUTCOME,
        started_at=started,
        duration=0.01,
    )
    rebase_started = time.monotonic()
    yield StepEvent(
        step_name="RebaseStep",
        status="running",
        outcome=None,
        started_at=rebase_started,
        duration=None,
    )
    await asyncio.sleep(0)
    raise RuntimeError("rebase conflict")


def test_review_app_renders_every_registered_step_as_pending_before_any_event() -> None:
    async def never_yields() -> AsyncIterator[StepEvent]:
        return
        yield  # pragma: no cover - makes this an async generator

    async def scenario() -> None:
        app = ReviewApp(REGISTRY, never_yields())
        async with app.run_test() as pilot:
            await pilot.pause()
            box = app.query_one(PipelineBox)
            assert _pipeline_box_content(box) == "◌ IntentStep\n◌ RebaseStep\n◌ ReviewStep"
            app.exit()

    asyncio.run(scenario())


def test_review_app_shows_not_yet_implemented_steps_as_pending_throughout_the_run() -> None:
    async def scenario() -> None:
        app = ReviewApp(REGISTRY, _one_step_completes())
        async with app.run_test() as pilot:
            await pilot.pause()
            box = app.query_one(PipelineBox)
            # RebaseStep and ReviewStep have no class yet -- they stay pending placeholders
            # for the whole run, even once IntentStep has finished.
            content = _pipeline_box_content(box)
            assert "◌ RebaseStep" in content
            assert "◌ ReviewStep" in content

    asyncio.run(scenario())


def test_review_app_does_not_exit_itself_on_success_until_e_is_pressed() -> None:
    """The app used to exit itself the instant `events` was exhausted -- with today's
    pipeline being a single near-instant `IntentStep`, that made a real run flash onto the
    terminal and vanish in well under a second (see `app.py`'s module docstring). It now
    waits for "e" instead."""

    async def scenario() -> None:
        app = ReviewApp(REGISTRY, _one_step_completes())
        async with app.run_test() as pilot:
            await _wait_until_done(pilot, app)

            assert app.is_running is True
            assert app.error is None

            await pilot.press("e")
            await pilot.pause()

            assert app.is_running is False

    asyncio.run(scenario())


def test_review_app_e_before_the_run_is_done_does_not_exit() -> None:
    async def scenario() -> None:
        async def hangs_until_cancelled() -> AsyncIterator[StepEvent]:
            await asyncio.Future()
            yield  # pragma: no cover - unreachable, only makes this an async generator

        app = ReviewApp(REGISTRY, hangs_until_cancelled())
        async with app.run_test() as pilot:
            await pilot.pause()

            await pilot.press("e")
            await pilot.pause()

            assert app.is_running is True
            app.exit()

    asyncio.run(scenario())


def test_review_app_final_render_shows_intent_step_completed() -> None:
    async def scenario() -> None:
        app = ReviewApp(REGISTRY, _one_step_completes())
        async with app.run_test() as pilot:
            await _wait_until_done(pilot, app)
            box = app.query_one(PipelineBox)
            assert "● IntentStep" in _pipeline_box_content(box)

    asyncio.run(scenario())


def test_review_app_records_the_error_and_marks_the_mid_flight_step_failed() -> None:
    async def scenario() -> None:
        app = ReviewApp(REGISTRY, _second_step_raises())
        async with app.run_test() as pilot:
            await _wait_until_done(pilot, app)

        assert isinstance(app.error, RuntimeError)
        assert str(app.error) == "rebase conflict"

    asyncio.run(scenario())


def test_review_app_does_not_exit_itself_on_failure_until_e_is_pressed() -> None:
    """A broken run stays on screen exactly like a successful one -- seeing the failed step
    and the error message matters at least as much as a clean exit does."""

    async def scenario() -> None:
        app = ReviewApp(REGISTRY, _second_step_raises())
        async with app.run_test() as pilot:
            await _wait_until_done(pilot, app)

            assert app.is_running is True

            await pilot.press("e")
            await pilot.pause()

            assert app.is_running is False

    asyncio.run(scenario())


def test_review_app_shows_no_status_box_while_the_run_is_still_in_progress() -> None:
    async def scenario() -> None:
        async def hangs_until_cancelled() -> AsyncIterator[StepEvent]:
            await asyncio.Future()
            yield  # pragma: no cover - unreachable, only makes this an async generator

        app = ReviewApp(REGISTRY, hangs_until_cancelled())
        async with app.run_test() as pilot:
            await pilot.pause()
            assert list(app.query(StatusBox)) == []
            app.exit()

    asyncio.run(scenario())


def test_review_app_status_box_shows_success_message_after_a_clean_run() -> None:
    async def scenario() -> None:
        app = ReviewApp(REGISTRY, _one_step_completes())
        async with app.run_test() as pilot:
            await _wait_until_done(pilot, app)
            box = app.query_one(StatusBox)
            assert box.content.startswith("Pipeline ran successfully.")
            assert "Press 'e' to exit." in box.content
            app.exit()

    asyncio.run(scenario())


def test_review_app_status_box_shows_the_error_message_after_a_failed_run() -> None:
    async def scenario() -> None:
        app = ReviewApp(REGISTRY, _second_step_raises())
        async with app.run_test() as pilot:
            await _wait_until_done(pilot, app)
            box = app.query_one(StatusBox)
            assert box.content.startswith("Pipeline failed: rebase conflict")
            app.exit()

    asyncio.run(scenario())


def test_review_app_relays_a_queued_input_request_through_a_modal() -> None:
    """Issue #41 end-to-end proof: a concurrent `relay.request_input(...)` call (standing
    in for a blocked backend subprocess) surfaces as a modal with the right prompt text;
    typing an answer and pressing enter dismisses it and resolves the original call with
    that answer."""

    async def hangs_until_cancelled() -> AsyncIterator[StepEvent]:
        # Unlike `never_yields()` elsewhere in this file, this deliberately never lets
        # `_consume_events` finish and call `self.exit()` on its own -- the test needs the
        # app to stay alive for the whole modal round-trip, and calls `app.exit()` itself
        # once done (matching the pattern other tests in this file already use).
        await asyncio.Future()
        yield  # pragma: no cover - unreachable, only makes this an async generator

    async def scenario() -> str:
        relay = InputRelay()
        app = ReviewApp(REGISTRY, hangs_until_cancelled(), input_relay=relay)
        async with app.run_test() as pilot:
            await pilot.pause()

            request_task = asyncio.ensure_future(relay.request_input("Allow write? (y/n): "))
            for _ in range(20):
                if isinstance(app.screen, InputPromptScreen):
                    break
                await pilot.pause()
                await asyncio.sleep(0.01)

            assert isinstance(app.screen, InputPromptScreen)
            prompt_text = " ".join(str(widget.content) for widget in app.screen.query(Static))
            assert "Allow write? (y/n): " in prompt_text

            await pilot.click(Input)
            await pilot.press(*"yes", "enter")
            await pilot.pause()

            answer = await request_task
            app.exit()
            return answer

    answer = asyncio.run(scenario())

    assert answer == "yes"


# --- The `ApprovalRelay` seam (issue #80) -----------------------------------------------

_PARK_OUTCOME = StepOutcome(
    needs_approval=True,
    auto_fixable=False,
    findings=[
        Finding(severity="error", description="unpushed local commits", review_scope="source")
    ],
)


def _rebase_step_parks_then_maybe_continues(relay: ApprovalRelay) -> AsyncIterator[StepEvent]:
    """A synthetic events generator standing in for `pipeline.executor.run_steps`'s own
    park/approve/skip/abort logic (see that module's docstring's "The approval park"
    section) -- proves `ReviewApp`'s modal/relay independent of any real step or a real
    `run_steps` call, per this issue's own explicit acceptance criterion (mirroring how
    #41/#66 each first proved `InputRelay`/`ActivityRelay` against a hand-built fake before
    any real producer existed).

    `IntentStep` completes normally, then `RebaseStep` completes with a
    `needs_approval=True` outcome and calls `relay.request_approval` itself -- exactly what
    `run_steps` does once `StepContext.on_approval_needed` is wired to
    `relay.request_approval` for real. "abort" raises `RunAbortedError`, matching
    `run_steps`'s own behavior; any other decision ("approve" or "skip") lets a third step,
    `ReviewStep`, run to completion -- proving later steps still run either way.
    """

    async def _events() -> AsyncIterator[StepEvent]:
        started = time.monotonic()
        yield StepEvent(
            step_name="IntentStep",
            status="running",
            outcome=None,
            started_at=started,
            duration=None,
        )
        await asyncio.sleep(0)
        yield StepEvent(
            step_name="IntentStep",
            status="completed",
            outcome=_OUTCOME,
            started_at=started,
            duration=0.01,
        )

        rebase_started = time.monotonic()
        yield StepEvent(
            step_name="RebaseStep",
            status="running",
            outcome=None,
            started_at=rebase_started,
            duration=None,
        )
        await asyncio.sleep(0)
        yield StepEvent(
            step_name="RebaseStep",
            status="completed",
            outcome=_PARK_OUTCOME,
            started_at=rebase_started,
            duration=0.01,
        )

        response = await relay.request_approval("RebaseStep", _PARK_OUTCOME)
        if response.decision == "abort":
            raise RunAbortedError("RebaseStep")

        review_started = time.monotonic()
        yield StepEvent(
            step_name="ReviewStep",
            status="running",
            outcome=None,
            started_at=review_started,
            duration=None,
        )
        await asyncio.sleep(0)
        yield StepEvent(
            step_name="ReviewStep",
            status="completed",
            outcome=_OUTCOME,
            started_at=review_started,
            duration=0.01,
        )

    return _events()


async def _wait_for_park(pilot: Pilot[None], app: ReviewApp, step_name: str) -> None:
    """Poll until `app._parked_step` names `step_name` and the Findings box has actually
    mounted its `OptionList` -- the inline-flow (issue #87) equivalent of the old
    `_wait_for_approval_prompt`'s "pushed but not yet composed" concern, now for a mounted
    widget instead of a pushed `ApprovalPromptScreen`."""

    for _ in range(20):
        if app._parked_step == step_name and list(app.query(FindingsBox)):
            return
        await pilot.pause()
        await asyncio.sleep(0.01)
    raise AssertionError(f"{step_name} never parked")


def test_review_app_parks_on_a_synthetic_outcome_and_approve_continues_the_run() -> None:
    async def scenario() -> None:
        relay = ApprovalRelay()
        app = ReviewApp(
            REGISTRY, _rebase_step_parks_then_maybe_continues(relay), approval_relay=relay
        )
        async with app.run_test() as pilot:
            await _wait_for_park(pilot, app, "RebaseStep")

            box = app.query_one(FindingsBox)
            assert "unpushed local commits" in _findings_box_content(box)

            # While parked, the Pipeline box shows RebaseStep as "parked" even though its
            # own StepEvent already says "completed" -- the design nuance `state.py`'s
            # module docstring documents.
            parked_row = next(row for row in app._rows() if row.name == "RebaseStep")
            assert parked_row.status == "parked"

            box.query_one(OptionList).focus()
            await pilot.press("a")
            await pilot.pause()
            await _wait_until_done(pilot, app)

        assert app.error is None
        rebase_row = next(row for row in app._rows() if row.name == "RebaseStep")
        assert rebase_row.status == "completed"
        review_row = next(row for row in app._rows() if row.name == "ReviewStep")
        assert review_row.status == "completed"

    asyncio.run(scenario())


def test_review_app_choosing_skip_marks_the_step_skipped_and_the_run_continues() -> None:
    async def scenario() -> None:
        relay = ApprovalRelay()
        app = ReviewApp(
            REGISTRY, _rebase_step_parks_then_maybe_continues(relay), approval_relay=relay
        )
        async with app.run_test() as pilot:
            await _wait_for_park(pilot, app, "RebaseStep")

            box = app.query_one(FindingsBox)
            box.query_one(OptionList).focus()
            await pilot.press("s")
            await pilot.pause()
            await _wait_until_done(pilot, app)

        assert app.error is None
        rebase_row = next(row for row in app._rows() if row.name == "RebaseStep")
        assert rebase_row.status == "skipped"
        # Skip is not an error: the run continues to ReviewStep, which still completes.
        review_row = next(row for row in app._rows() if row.name == "ReviewStep")
        assert review_row.status == "completed"

    asyncio.run(scenario())


def test_review_app_choosing_abort_stops_the_run_and_records_the_error() -> None:
    async def scenario() -> None:
        relay = ApprovalRelay()
        app = ReviewApp(
            REGISTRY, _rebase_step_parks_then_maybe_continues(relay), approval_relay=relay
        )
        async with app.run_test() as pilot:
            await _wait_for_park(pilot, app, "RebaseStep")

            box = app.query_one(FindingsBox)
            box.query_one(OptionList).focus()
            await pilot.press("x")
            await pilot.pause()
            await _wait_until_done(pilot, app)

        assert isinstance(app.error, RunAbortedError)
        assert "RebaseStep" in str(app.error)
        # No further step ran: ReviewStep never even reached "running".
        review_row = next(row for row in app._rows() if row.name == "ReviewStep")
        assert review_row.status == "pending"

    asyncio.run(scenario())


# --- The "fix" response and the inline chat widget (issue #81, reworked by #87) ---------


def test_review_app_choosing_fix_prompts_for_instructions_then_resolves_with_them() -> None:
    """Issue #81's TUI-side acceptance criterion, reworked by issue #87: the "f" shortcut
    opens `_InlineApprovalChat`, mounted directly into the already-parked `FindingsBox`,
    rather than pushing a second `InputPromptScreen` modal -- and
    `ReviewApp._relay_approval` only resolves the pending `ApprovalRelay.request_approval`
    future -- with the full `ApprovalResponse(decision="fix", instructions=...)`, not just
    the bare decision -- once that widget's `Input` is submitted."""

    async def scenario() -> ApprovalResponse:
        relay = ApprovalRelay()
        captured: list[ApprovalResponse] = []

        async def _events() -> AsyncIterator[StepEvent]:
            started = time.monotonic()
            yield StepEvent(
                step_name="RebaseStep",
                status="running",
                outcome=None,
                started_at=started,
                duration=None,
            )
            await asyncio.sleep(0)
            yield StepEvent(
                step_name="RebaseStep",
                status="completed",
                outcome=_PARK_OUTCOME,
                started_at=started,
                duration=0.01,
            )

            response = await relay.request_approval("RebaseStep", _PARK_OUTCOME)
            captured.append(response)
            # Keep the app alive until the test tears it down itself (mirrors
            # `test_review_app_relays_a_queued_input_request_through_a_modal`'s own
            # `hangs_until_cancelled` pattern) -- this test is about the relay round-trip,
            # not about the run reaching a Status box.
            await asyncio.Future()
            yield  # pragma: no cover - unreachable, only makes this an async generator

        app = ReviewApp(REGISTRY, _events(), approval_relay=relay)
        async with app.run_test() as pilot:
            await _wait_for_park(pilot, app, "RebaseStep")

            box = app.query_one(FindingsBox)
            box.query_one(OptionList).focus()
            await pilot.press("f")
            await pilot.pause()

            input_widget = box.query_one(Input)
            await pilot.click(input_widget)
            await pilot.press(*"rename the helper", "enter")
            await pilot.pause()

            for _ in range(20):
                if captured:
                    break
                await pilot.pause()
                await asyncio.sleep(0.01)

            app.exit()

        return captured[0]

    response = asyncio.run(scenario())

    assert response == ApprovalResponse(decision="fix", instructions="rename the helper")


def test_review_app_parks_with_a_review_output_outcome_without_crashing_on_markup() -> None:
    """Regression test: a real `ReviewStep`/`TestSufficiencyStep` park carries a
    `ReviewOutput`/`TestSufficiencyOutput`, not a bare `list[Finding]` -- a
    `Finding.description` is agent-produced, untrusted text that can legitimately contain
    `[...]`-shaped substrings, which an earlier version of the (now-removed)
    `ApprovalPromptScreen`'s `str(...)`-based rendering mis-parsed as Rich markup and
    raised `MarkupError` on. `FindingsBox`'s `OptionList` options are built directly as
    Rich renderables (`_finding_option_prompt`), never `str`-interpolated through a
    markup-enabled `Static.update`, so this proves the box still mounts and renders this
    outcome shape cleanly."""

    async def _events(relay: ApprovalRelay) -> AsyncIterator[StepEvent]:
        started = time.monotonic()
        yield StepEvent(
            step_name="ReviewStep",
            status="running",
            outcome=None,
            started_at=started,
            duration=None,
        )
        await asyncio.sleep(0)
        outcome = StepOutcome(
            needs_approval=True,
            auto_fixable=False,
            findings=ReviewOutput(
                findings=[
                    Finding(
                        severity="error",
                        description="drops error handling required by the caller's contract",
                        action="ask-user",
                        review_scope="source",
                    )
                ],
                risk_level="high",
                risk_rationale="drops error handling on a path the caller depends on",
            ),
        )
        yield StepEvent(
            step_name="ReviewStep",
            status="completed",
            outcome=outcome,
            started_at=started,
            duration=0.01,
        )
        await relay.request_approval("ReviewStep", outcome)

    async def scenario() -> None:
        relay = ApprovalRelay()
        app = ReviewApp(REGISTRY, _events(relay), approval_relay=relay)
        async with app.run_test() as pilot:
            await _wait_for_park(pilot, app, "ReviewStep")

            box = app.query_one(FindingsBox)
            assert "drops error handling required by the caller's contract" in (
                _findings_box_content(box)
            )

            box.query_one(OptionList).focus()
            await pilot.press("a")
            await pilot.pause()
            await _wait_until_done(pilot, app)

        assert app.error is None

    asyncio.run(scenario())


def _review_output(*findings: Finding) -> ReviewOutput:
    return ReviewOutput(findings=list(findings), risk_level="low", risk_rationale="fine")


def _test_sufficiency_output(*findings: Finding) -> TestSufficiencyOutput:
    return TestSufficiencyOutput(
        findings=list(findings), tested=[], testing_summary="fine", artifacts=[]
    )


async def _review_step_completes_with_findings() -> AsyncIterator[StepEvent]:
    started = time.monotonic()
    yield StepEvent(
        step_name="ReviewStep", status="running", outcome=None, started_at=started, duration=None
    )
    await asyncio.sleep(0)
    output = _review_output(
        Finding(severity="error", description="removes error handling", review_scope="source")
    )
    outcome = StepOutcome(needs_approval=True, auto_fixable=False, findings=output)
    yield StepEvent(
        step_name="ReviewStep",
        status="completed",
        outcome=outcome,
        started_at=started,
        duration=0.01,
    )


async def _test_sufficiency_step_completes_with_findings() -> AsyncIterator[StepEvent]:
    started = time.monotonic()
    yield StepEvent(
        step_name="TestSufficiencyStep",
        status="running",
        outcome=None,
        started_at=started,
        duration=None,
    )
    await asyncio.sleep(0)
    output = _test_sufficiency_output(
        Finding(
            severity="warning", description="no test covers the retry path", review_scope="source"
        )
    )
    outcome = StepOutcome(needs_approval=False, auto_fixable=False, findings=output)
    yield StepEvent(
        step_name="TestSufficiencyStep",
        status="completed",
        outcome=outcome,
        started_at=started,
        duration=0.01,
    )


async def _review_step_completes_with_no_findings() -> AsyncIterator[StepEvent]:
    started = time.monotonic()
    yield StepEvent(
        step_name="ReviewStep", status="running", outcome=None, started_at=started, duration=None
    )
    await asyncio.sleep(0)
    outcome = StepOutcome(needs_approval=False, auto_fixable=False, findings=_review_output())
    yield StepEvent(
        step_name="ReviewStep",
        status="completed",
        outcome=outcome,
        started_at=started,
        duration=0.01,
    )


async def _two_steps_complete_with_findings() -> AsyncIterator[StepEvent]:
    started = time.monotonic()
    earlier = _review_output(
        Finding(severity="info", description="first pass finding", review_scope="source")
    )
    later = _review_output(
        Finding(severity="error", description="second pass finding", review_scope="source")
    )
    yield StepEvent(
        step_name="ReviewStep",
        status="completed",
        outcome=StepOutcome(needs_approval=False, auto_fixable=False, findings=earlier),
        started_at=started,
        duration=0.01,
    )
    await asyncio.sleep(0)
    yield StepEvent(
        step_name="TestSufficiencyStep",
        status="completed",
        outcome=StepOutcome(needs_approval=True, auto_fixable=False, findings=later),
        started_at=started,
        duration=0.01,
    )


def test_review_app_shows_a_findings_box_once_a_step_completes_with_non_empty_findings() -> None:
    async def scenario() -> None:
        app = ReviewApp(REGISTRY, _review_step_completes_with_findings())
        async with app.run_test() as pilot:
            await _wait_until_done(pilot, app)

            box = app.query_one(FindingsBox)
            assert "removes error handling" in _findings_box_content(box)
            assert box.border_title == "Findings -- ReviewStep"

    asyncio.run(scenario())


def test_review_app_e_still_exits_after_the_findings_box_option_list_has_focus() -> None:
    """Regression test for issue #88: `FindingsBox`'s child `OptionList` can take focus
    and handle its own up/down bindings without shadowing the app-level "e" exit binding
    (registered on `App`, not on `OptionList`)."""

    async def scenario() -> None:
        app = ReviewApp(REGISTRY, _review_step_completes_with_findings())
        async with app.run_test() as pilot:
            await _wait_until_done(pilot, app)

            box = app.query_one(FindingsBox)
            box.query_one(OptionList).focus()
            await pilot.pause()

            assert app.is_running is True
            await pilot.press("e")
            await pilot.pause()

            assert app.is_running is False

    asyncio.run(scenario())


def test_review_app_shows_a_findings_box_for_a_completed_test_sufficiency_output() -> None:
    async def scenario() -> None:
        app = ReviewApp(REGISTRY, _test_sufficiency_step_completes_with_findings())
        async with app.run_test() as pilot:
            await _wait_until_done(pilot, app)

            box = app.query_one(FindingsBox)
            assert "no test covers the retry path" in _findings_box_content(box)
            assert box.border_title == "Findings -- TestSufficiencyStep"

    asyncio.run(scenario())


def test_review_app_shows_no_findings_box_when_the_only_completed_outcome_has_no_findings() -> None:
    async def scenario() -> None:
        app = ReviewApp(REGISTRY, _one_step_completes())
        async with app.run_test() as pilot:
            await _wait_until_done(pilot, app)

            # `_one_step_completes` reports an outcome with `findings=None` -- not a
            # `ReviewOutput` -- so no Findings box should ever mount.
            assert list(app.query(FindingsBox)) == []

    asyncio.run(scenario())


def test_review_app_shows_no_findings_box_when_the_completed_review_output_is_empty() -> None:
    async def scenario() -> None:
        app = ReviewApp(REGISTRY, _review_step_completes_with_no_findings())
        async with app.run_test() as pilot:
            await _wait_until_done(pilot, app)

            assert list(app.query(FindingsBox)) == []

    asyncio.run(scenario())


def test_review_app_findings_box_shows_the_later_of_two_completed_findings() -> None:
    async def scenario() -> None:
        app = ReviewApp(REGISTRY, _two_steps_complete_with_findings())
        async with app.run_test() as pilot:
            await _wait_until_done(pilot, app)

            box = app.query_one(FindingsBox)
            content = _findings_box_content(box)
            assert "second pass finding" in content
            assert "first pass finding" not in content

    asyncio.run(scenario())


def test_review_app_shows_synthetic_activity_events_nested_under_the_running_step() -> None:
    """Issue #66 end-to-end proof, mirroring `test_review_app_relays_a_queued_input_
    request_through_a_modal`'s own shape for `InputRelay`: a real `ActivityRelay` fed
    synthetic `ActivityEvent`s the same way #41 proved `InputRelay`'s queueing contract
    before any real producer existed -- no `gitutils`/`ReviewStep` involved here, just a
    hand-built `relay.activity(...)` call standing in for a future real one (#64/#65).

    Proves: the reported activity renders as a nested (indented) line under `RebaseStep`
    -- the step `ReviewApp._running_step` names at the moment the event arrives -- ticks
    live while open, and collapses to a completed icon once the `async with` block exits.
    """

    async def rebase_step_running_forever() -> AsyncIterator[StepEvent]:
        started = time.monotonic()
        yield StepEvent(
            step_name="RebaseStep",
            status="running",
            outcome=None,
            started_at=started,
            duration=None,
        )
        # Never completes -- keeps RebaseStep "running" (and thus the activity's owner)
        # for the whole test; the test calls `app.exit()` itself once done.
        await asyncio.Future()
        yield  # pragma: no cover - unreachable, only makes this an async generator

    async def _fetch_line(pilot: Pilot[None], app: ReviewApp) -> str | None:
        content = _pipeline_box_content(app.query_one(PipelineBox))
        return next((line for line in content.splitlines() if "fetch" in line), None)

    async def scenario() -> None:
        relay = ActivityRelay()
        app = ReviewApp(REGISTRY, rebase_step_running_forever(), activity_relay=relay)
        async with app.run_test() as pilot:
            await pilot.pause()

            async with relay.activity("fetch"):
                fetch_line: str | None = None
                for _ in range(20):
                    fetch_line = await _fetch_line(pilot, app)
                    if fetch_line is not None:
                        break
                    await pilot.pause()
                    await asyncio.sleep(0.01)

                assert fetch_line is not None
                # Nested: indented beneath RebaseStep's own line, not a flush-left row of
                # its own.
                assert fetch_line.startswith(" ")
                assert "RebaseStep" in _pipeline_box_content(app.query_one(PipelineBox))

                # Live-ticking: let at least one real 0.25s tick interval pass (see
                # `app.py`'s `_TICK_INTERVAL`) and confirm the rendered line actually
                # changed, i.e. the duration is advancing, not frozen.
                ticked_line = fetch_line
                for _ in range(60):
                    await pilot.pause()
                    await asyncio.sleep(0.02)
                    ticked_line = await _fetch_line(pilot, app)
                    if ticked_line != fetch_line:
                        break
                assert ticked_line != fetch_line

            # Once the `async with` exits, the activity's "finished" event collapses the
            # line to a final, completed-status duration.
            final_line: str | None = None
            for _ in range(20):
                final_line = await _fetch_line(pilot, app)
                if final_line is not None and "✔" in final_line:
                    break
                await pilot.pause()
                await asyncio.sleep(0.01)

            assert final_line is not None
            assert "✔" in final_line
            assert final_line.startswith(" ")

            app.exit()

    asyncio.run(scenario())


def test_tag_activity_events_keeps_a_span_with_the_step_that_completed_before_it() -> None:
    """Issue #65 regression: a real producer (`ReviewStep`) closes its activity right at
    the tail of `run`, with no further `await` before the next step starts -- so the
    "finished" `ActivityEvent`'s own timestamp can land *after* its owning step's
    "completed" `StepEvent` and even after the next step's "running" `StepEvent`, exactly
    the ordering `run_steps` produces when the executor advances the instant `ReviewStep.
    run` returns. The old design (tagging each `ActivityEvent` with whichever step
    `_consume_activities` saw as `self._running_step` live, at dequeue time) mis-attributed
    that "finished" event to the *next* step, splitting one span's "started"/"finished"
    pair across two different owners and crashing `state.backfill_activities`' duration
    lookup (`KeyError`) -- see this module's own docstring paragraph on `activity_relay`.
    `_tag_activity_events` fixes this by attributing purely from timestamps against each
    step's own running window, computed fresh from `StepEvent`s already seen."""

    review_started = 10.0
    review_completed = 10.03  # ReviewStep's "completed" StepEvent.
    test_sufficiency_started = 10.03  # The very next step starts immediately after.

    seen = [
        StepEvent(
            step_name="ReviewStep",
            status="running",
            outcome=None,
            started_at=review_started,
            duration=None,
        ),
        StepEvent(
            step_name="ReviewStep",
            status="completed",
            outcome=_OUTCOME,
            started_at=review_started,
            duration=review_completed - review_started,
        ),
        StepEvent(
            step_name="TestSufficiencyStep",
            status="running",
            outcome=None,
            started_at=test_sufficiency_started,
            duration=None,
        ),
    ]
    activity_events = [
        ActivityEvent(1, None, "Agent: reviewing diff via claude", "started", 10.01),
        # Queued (and would be dequeued) after ReviewStep's own "completed" StepEvent --
        # the exact ordering that broke the old live-tag design.
        ActivityEvent(1, None, "Agent: reviewing diff via claude", "finished", 10.031),
    ]

    tagged = _tag_activity_events(seen, activity_events)

    owners = [owner for owner, _event in tagged]
    assert owners == ["ReviewStep", "ReviewStep"]


def test_review_app_keeps_a_finished_activity_under_its_starting_step_across_a_fast_handoff() -> (
    None
):
    """Regression for a real bug issue #64's real `RebaseStep` producer exposed in `#66`'s
    own consuming code: the app used to tag an `ActivityEvent` with `self._running_step` at
    receipt time, which -- unlike `_tag_activity_events` (see the test above and `app.py`'s
    module docstring) -- is not sound, since `_consume_events` and the activity worker are
    two independently scheduled tasks with no ordering guarantee between them.

    `_consume_events` (driving the `StepEvent` stream) and the activity worker are two
    independently scheduled tasks. Entering/exiting `relay.activity(...)` never itself
    suspends (`asyncio.Queue.put` on an unbounded queue returns without a real checkpoint),
    so both an activity's "started" and "finished" `ActivityEvent`s get queued perfectly
    synchronously with whatever code enclosed them -- the activity worker only gets a
    chance to actually drain the queue at the *next* genuine checkpoint (a real `await` that
    suspends) anywhere in the app. This scenario reproduces the exact gap that bit
    `RebaseStep` in production: one checkpoint inside the activity's own body (standing in
    for `run_git`'s real subprocess spawn) lets the worker dequeue "started" while RebaseStep
    is still the running step, then -- with no further checkpoint -- RebaseStep's "completed"
    and ReviewStep's "running" `StepEvent`s render before the worker ever gets scheduled
    again, so by the time it dequeues "finished", `self._running_step` has already become
    "ReviewStep". `_tag_activity_events`'s timestamp-window attribution (not tied to live
    scheduling order at all) keeps both halves under RebaseStep regardless.
    """

    relay = ActivityRelay()

    async def events() -> AsyncIterator[StepEvent]:
        started = time.monotonic()
        yield StepEvent(
            step_name="RebaseStep",
            status="running",
            outcome=None,
            started_at=started,
            duration=None,
        )

        async with relay.activity("git rebase origin/main"):
            # A real checkpoint (stands in for `run_git`'s real subprocess spawn) -- gives
            # the activity worker its one chance to dequeue "started" while RebaseStep is
            # still `self._running_step`.
            await asyncio.sleep(0)

        # Deliberately no checkpoint here, matching production: "finished" was just queued
        # synchronously, and nothing suspends again until the render calls below return and
        # this generator is asked for its next item.
        yield StepEvent(
            step_name="RebaseStep",
            status="completed",
            outcome=_OUTCOME,
            started_at=started,
            duration=0.01,
        )
        review_started = time.monotonic()
        yield StepEvent(
            step_name="ReviewStep",
            status="running",
            outcome=None,
            started_at=review_started,
            duration=None,
        )

        # First real checkpoint since "finished" was queued -- this is where the activity
        # worker finally gets to run, with `self._running_step` already "ReviewStep".
        await asyncio.sleep(0.05)
        yield StepEvent(
            step_name="ReviewStep",
            status="completed",
            outcome=_OUTCOME,
            started_at=review_started,
            duration=0.01,
        )

    async def scenario() -> None:
        app = ReviewApp(REGISTRY, events(), activity_relay=relay)
        async with app.run_test() as pilot:
            await _wait_until_done(pilot, app)

            content = _pipeline_box_content(app.query_one(PipelineBox))
            lines = content.splitlines()
            rebase_idx = next(i for i, line in enumerate(lines) if "RebaseStep" in line)
            review_idx = next(i for i, line in enumerate(lines) if "ReviewStep" in line)
            activity_idx = next(
                i for i, line in enumerate(lines) if "git rebase origin/main" in line
            )

            # The activity's line sits under RebaseStep's own row, not ReviewStep's, and
            # appears exactly once -- not split across both.
            assert rebase_idx < activity_idx < review_idx
            assert sum(1 for line in lines if "git rebase origin/main" in line) == 1
            # Nested and shows a final, completed duration -- not stuck "running" forever
            # (the old bug's other failure shape, when the split landed the other way).
            assert lines[activity_idx].startswith(" ")
            assert "✔" in lines[activity_idx]

    asyncio.run(scenario())


def test_review_app_final_render_on_failure_shows_the_broken_step_as_failed() -> None:
    async def scenario() -> None:
        app = ReviewApp(REGISTRY, _second_step_raises())
        async with app.run_test() as pilot:
            await _wait_until_done(pilot, app)
            box = app.query_one(PipelineBox)
            content = _pipeline_box_content(box)
            # Completed and failed both render the same "●" dot glyph, colored by status --
            # `_pipeline_box_content` prints through a `color_system=None` console, so it
            # can't see that distinction here (`test_widgets.py`'s `_render_row` icon-color
            # test covers it directly). This only confirms both rows moved off "pending".
            assert "● IntentStep" in content
            assert "● RebaseStep" in content
            # ReviewStep never ran -- still a pending placeholder even after the failure.
            assert "◌ ReviewStep" in content

    asyncio.run(scenario())
