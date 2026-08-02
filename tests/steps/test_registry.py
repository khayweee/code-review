"""Tests for the canonical step registry (Milestone 13, issue #40).

`STEP_REGISTRY` is the single source of truth for step display names; `IMPLEMENTED_STEPS`
only ever holds classes, never a second list of name strings (see `registry.py`'s module
docstring). These tests pin the invariant that keeps the two in sync: an implemented step
added out of registry order must fail loudly, not silently render under the wrong name.
"""

from __future__ import annotations

from code_review.steps.registry import IMPLEMENTED_STEPS, STEP_REGISTRY


def test_step_registry_is_non_empty() -> None:
    assert len(STEP_REGISTRY) > 0


def test_step_registry_has_no_duplicate_entries() -> None:
    assert len(STEP_REGISTRY) == len(set(STEP_REGISTRY))


def test_implemented_steps_names_match_the_registrys_prefix_in_order() -> None:
    """Every entry in `IMPLEMENTED_STEPS` must have a `get_name()` matching the
    corresponding position in `STEP_REGISTRY` -- a future step appended to
    `IMPLEMENTED_STEPS` out of registry order would otherwise render as a pending
    placeholder under its own name while its real events land under the wrong row."""

    assert len(IMPLEMENTED_STEPS) <= len(STEP_REGISTRY)
    for registry_name, step_cls in zip(STEP_REGISTRY, IMPLEMENTED_STEPS, strict=False):
        assert step_cls().get_name() == registry_name
