"""Shared structured-output extraction and schema validation."""

from __future__ import annotations

import json
from typing import TypeAlias, TypeVar, cast

from pydantic import BaseModel

JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]
OutputT = TypeVar("OutputT", bound=BaseModel)


def extract_json(text: str) -> JsonValue:
    """Extract a clean JSON response.

    https://github.com/khayweee/code-review/issues/3 - round-trip a prompt through the
    agent CLI to a schema-validated answer; this slice deliberately supports only a
    well-behaved response. Later extraction strategies for fenced and prose-wrapped
    objects belong here, not in a backend.
    """

    return cast(JsonValue, json.loads(text))


def validate_output(value: JsonValue, output_schema: type[OutputT]) -> OutputT:
    """Validate extracted JSON and return the requested pydantic model."""

    return output_schema.model_validate(value)
