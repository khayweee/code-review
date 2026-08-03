#!/usr/bin/env python3
"""Fake Claude CLI for the full-pipeline CLI test (`tests/test_cli_review.py`, issue #60):
a clean, no-findings answer shaped as a *superset* of both `ReviewOutput`
(`steps/review.py`) and `TestSufficiencyOutput` (`steps/test_sufficiency.py`).

`tests/test_cli_review.py`'s end-to-end test cannot pass a different `--json-schema` per
call to two separate fake scripts the way `tests/steps/test_review.py`/
`test_test_sufficiency.py` do (one fake per step's own test file) -- `cli.py` builds
`ReviewStep()`/`TestSufficiencyStep()` via `IMPLEMENTED_STEPS` with no executable override,
so both steps' `ClaudeCLI` calls resolve the same literal `"claude"` on `PATH` (see that
test file's `_env_with_fake_claude`). One script covering every field either schema
requires works for both, because pydantic v2's default `extra="ignore"` behavior (neither
`Finding`, `ReviewOutput`, nor `TestSufficiencyOutput` sets `model_config`/`Config` to
forbid extra fields) means the caller's own schema simply ignores whichever half of this
payload it didn't ask for.
"""

from __future__ import annotations

import json
import sys

sys.stdin.read()  # drain the prompt; this fixture's answer doesn't depend on its contents

response = {
    "structured_output": {
        "findings": [],
        "risk_level": "low",
        "risk_rationale": "clean",
        "tested": [],
        "testing_summary": "clean",
        "artifacts": [],
    }
}
print(json.dumps(response))
