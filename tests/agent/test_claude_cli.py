"""Public-call tests for the Claude CLI backend."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from pydantic import BaseModel

from code_review.agent import (
    ClaudeCLI,
    NoStructuredOutputError,
    OutputValidationError,
    ProcessExitError,
    ProcessStartError,
    RunOpts,
    Usage,
)

FAKES = Path(__file__).parent / "fakes"
FAKE_CLI = FAKES / "valid_output.py"
FENCED_CLI = FAKES / "chatty_fenced_output.py"
PROSE_CLI = FAKES / "chatty_prose_output.py"
NONZERO_EXIT_CLI = FAKES / "nonzero_exit.py"
NO_JSON_CLI = FAKES / "no_json_output.py"
INVALID_SCHEMA_CLI = FAKES / "invalid_schema_output.py"


class Answer(BaseModel):
    answer: str
    cwd: str
    schema_title: str
    pid: int
    process_group: int
    model: str
    system_prompt: str | None
    append_system_prompt: str | None
    tools_allowlist: list[str]
    permission_mode: str | None
    dangerously_skip_permissions: bool


def run_fake(
    prompt: str,
    cwd: Path,
    *,
    executable: Path = FAKE_CLI,
    model: str = "sonnet",
    system_prompt: str | None = None,
    append_system_prompt: str | None = None,
    tools_allowlist: tuple[str, ...] = (),
    permission_mode: str | None = None,
) -> tuple[Answer, str, Usage | None]:
    agent = ClaudeCLI()
    result = asyncio.run(
        agent.run(
            RunOpts(
                prompt=prompt,
                cwd=cwd,
                output_schema=Answer,
                executable=executable,
                model=model,
                system_prompt=system_prompt,
                append_system_prompt=append_system_prompt,
                tools_allowlist=tools_allowlist,
                permission_mode=permission_mode,
            )
        )
    )
    asyncio.run(agent.close())
    return result.output, result.text, result.usage


def test_round_trips_prompt_to_validated_answer(tmp_path: Path) -> None:
    output, text, usage = run_fake("inspect this change", tmp_path)

    assert isinstance(output, Answer)
    assert output.answer == "received: inspect this change"
    assert output.cwd == str(tmp_path)
    assert output.schema_title == "Answer"
    assert output.process_group == output.pid
    assert json.loads(text)["structured_output"]["answer"] == output.answer
    assert usage == Usage(input_tokens=12, output_tokens=4, total_cost_usd=0.25)


def test_missing_usage_remains_unknown(tmp_path: Path) -> None:
    _, _, usage = run_fake("omit usage", tmp_path)

    assert usage is None


def test_large_prompt_is_sent_over_stdin(tmp_path: Path) -> None:
    prompt = "x" * 200_000

    output, _, _ = run_fake(prompt, tmp_path)

    assert output.answer == f"received: {prompt}"


def test_defaults_to_sonnet_model(tmp_path: Path) -> None:
    output, _, _ = run_fake("inspect this change", tmp_path)

    assert output.model == "sonnet"


def test_custom_model_is_passed_through(tmp_path: Path) -> None:
    output, _, _ = run_fake("inspect this change", tmp_path, model="opus")

    assert output.model == "opus"


def test_system_prompt_omitted_by_default(tmp_path: Path) -> None:
    output, _, _ = run_fake("inspect this change", tmp_path)

    assert output.system_prompt is None


def test_system_prompt_is_passed_through(tmp_path: Path) -> None:
    output, _, _ = run_fake(
        "inspect this change", tmp_path, system_prompt="You are a careful reviewer."
    )

    assert output.system_prompt == "You are a careful reviewer."


def test_append_system_prompt_omitted_by_default(tmp_path: Path) -> None:
    output, _, _ = run_fake("inspect this change", tmp_path)

    assert output.append_system_prompt is None


def test_append_system_prompt_is_passed_through(tmp_path: Path) -> None:
    output, _, _ = run_fake(
        "inspect this change",
        tmp_path,
        append_system_prompt="Classify the risk of this diff.",
    )

    assert output.append_system_prompt == "Classify the risk of this diff."


def test_dangerously_skip_permissions_by_default(tmp_path: Path) -> None:
    output, _, _ = run_fake("inspect this change", tmp_path)

    assert output.tools_allowlist == []
    assert output.permission_mode is None
    assert output.dangerously_skip_permissions is True


def test_tools_allowlist_enables_auto_permission_mode(tmp_path: Path) -> None:
    output, _, _ = run_fake(
        "inspect this change",
        tmp_path,
        tools_allowlist=("Bash(uv run pytest *)", "Bash(uv run ruff *)"),
    )

    assert output.tools_allowlist == ["Bash(uv run pytest *)", "Bash(uv run ruff *)"]
    assert output.permission_mode == "auto"
    assert output.dangerously_skip_permissions is False


def test_permission_mode_is_overridable(tmp_path: Path) -> None:
    output, _, _ = run_fake(
        "inspect this change",
        tmp_path,
        tools_allowlist=("Bash(uv run pytest *)",),
        permission_mode="bypassPermissions",
    )

    assert output.permission_mode == "bypassPermissions"
    assert output.dangerously_skip_permissions is False


def test_permission_mode_overrides_skip_permissions_default(tmp_path: Path) -> None:
    output, _, _ = run_fake("inspect this change", tmp_path, permission_mode="bypassPermissions")

    assert output.tools_allowlist == []
    assert output.permission_mode == "bypassPermissions"
    assert output.dangerously_skip_permissions is False


def test_envelope_without_structured_output_raises_no_structured_output_error(
    tmp_path: Path,
) -> None:
    class PermissiveAnswer(BaseModel):
        answer: str = "default"

    agent = ClaudeCLI()
    with pytest.raises(NoStructuredOutputError):
        asyncio.run(
            agent.run(
                RunOpts(
                    prompt="omit structured output",
                    cwd=tmp_path,
                    output_schema=PermissiveAnswer,
                    executable=FAKE_CLI,
                )
            )
        )


def test_fenced_json_block_is_extracted(tmp_path: Path) -> None:
    output, _, _ = run_fake("inspect this change", tmp_path, executable=FENCED_CLI)

    assert output.answer == "received: inspect this change"


def test_prose_wrapped_object_is_extracted(tmp_path: Path) -> None:
    output, _, _ = run_fake("inspect this change", tmp_path, executable=PROSE_CLI)

    assert output.answer == "received: inspect this change"


def test_process_that_cannot_start_raises_process_start_error(tmp_path: Path) -> None:
    agent = ClaudeCLI()
    with pytest.raises(ProcessStartError) as exc_info:
        asyncio.run(
            agent.run(
                RunOpts(
                    prompt="inspect this change",
                    cwd=tmp_path,
                    output_schema=Answer,
                    executable=tmp_path / "does-not-exist",
                )
            )
        )

    assert exc_info.value.executable == str(tmp_path / "does-not-exist")


def test_nonzero_exit_raises_process_exit_error_with_context(tmp_path: Path) -> None:
    agent = ClaudeCLI()
    with pytest.raises(ProcessExitError) as exc_info:
        asyncio.run(
            agent.run(
                RunOpts(
                    prompt="inspect this change",
                    cwd=tmp_path,
                    output_schema=Answer,
                    executable=NONZERO_EXIT_CLI,
                )
            )
        )

    assert exc_info.value.returncode == 2
    assert "permission denied" in exc_info.value.stderr


def test_no_json_anywhere_raises_no_structured_output_error(tmp_path: Path) -> None:
    agent = ClaudeCLI()
    with pytest.raises(NoStructuredOutputError):
        asyncio.run(
            agent.run(
                RunOpts(
                    prompt="inspect this change",
                    cwd=tmp_path,
                    output_schema=Answer,
                    executable=NO_JSON_CLI,
                )
            )
        )


def test_schema_mismatch_raises_output_validation_error(tmp_path: Path) -> None:
    agent = ClaudeCLI()
    with pytest.raises(OutputValidationError):
        asyncio.run(
            agent.run(
                RunOpts(
                    prompt="inspect this change",
                    cwd=tmp_path,
                    output_schema=Answer,
                    executable=INVALID_SCHEMA_CLI,
                )
            )
        )
