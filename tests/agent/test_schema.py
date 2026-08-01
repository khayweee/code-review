"""Unit tests for the fixed extraction order and validation errors."""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from code_review.agent.errors import NoStructuredOutputError, OutputValidationError
from code_review.agent.schema import extract_json, validate_output


class Answer(BaseModel):
    value: int


def test_bare_json_response_is_extracted() -> None:
    assert extract_json('{"value": 1}') == {"value": 1}


def test_fenced_block_is_preferred_over_a_trailing_balanced_object() -> None:
    text = 'preamble\n```json\n{"value": 1}\n```\ntrailer {"value": 2}'

    assert extract_json(text) == {"value": 1}


def test_last_balanced_object_is_extracted_from_prose() -> None:
    text = 'Sure, here you go: {"value": 1} Hope that helps!'

    assert extract_json(text) == {"value": 1}


def test_no_json_anywhere_raises_no_structured_output_error() -> None:
    with pytest.raises(NoStructuredOutputError):
        extract_json("I can't help with that.")


def test_unbalanced_braces_raise_no_structured_output_error() -> None:
    with pytest.raises(NoStructuredOutputError):
        extract_json("here is a stray brace: }")


def test_validate_output_wraps_pydantic_validation_error() -> None:
    with pytest.raises(OutputValidationError) as exc_info:
        validate_output({"value": "not an int"}, Answer)

    assert exc_info.value.value == {"value": "not an int"}
