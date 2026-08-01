"""Shared structured-output extraction and schema validation."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import TypeAlias, TypeVar, cast

from pydantic import BaseModel
from pydantic import ValidationError as PydanticValidationError

from code_review.agent.errors import NoStructuredOutputError, OutputValidationError

JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]
OutputT = TypeVar("OutputT", bound=BaseModel)

_FENCED_BLOCK = re.compile(r"```(?:json)?\s*\n?(.*?)```", re.DOTALL)


def extract_json(text: str) -> JsonValue:
    """Extract a JSON response from possibly chatty agent output.

    https://github.com/khayweee/code-review/issues/4 - agents do not answer in a
    stable shape: the same prompt may return bare JSON, a fenced code block, or an
    object wrapped in a paragraph of preamble. None of that variance carries
    meaning, so extraction is tried in a fixed order until one strategy parses:
    the whole response as JSON, then a fenced JSON block, then the last balanced
    object in the text.
    """

    strategies: tuple[Callable[[], str | None], ...] = (
        lambda: text,
        lambda: _fenced_block(text),
        lambda: _last_balanced_object(text),
    )
    for strategy in strategies:
        candidate = strategy()
        if candidate is None:
            continue
        try:
            return cast(JsonValue, json.loads(candidate))
        except json.JSONDecodeError:
            continue
    raise NoStructuredOutputError(text)


def validate_output(value: JsonValue, output_schema: type[OutputT]) -> OutputT:
    """Validate extracted JSON and return the requested pydantic model."""

    try:
        return output_schema.model_validate(value)
    except PydanticValidationError as exc:
        raise OutputValidationError(value, exc) from exc


def _fenced_block(text: str) -> str | None:
    match = _FENCED_BLOCK.search(text)
    return match.group(1) if match else None


def _last_balanced_object(text: str) -> str | None:
    """Return the last top-level ``{...}`` span in ``text``, if any is balanced."""

    last: str | None = None
    start: int | None = None
    depth = 0
    for index, char in enumerate(text):
        if char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}" and depth > 0:
            depth -= 1
            if depth == 0 and start is not None:
                last = text[start : index + 1]
    return last
