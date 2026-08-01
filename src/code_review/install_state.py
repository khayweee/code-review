"""Install-lifecycle state directory (Milestone 12, issues #31-#33).

Distinct from `config.py`'s trusted-vs-descriptive pipeline config split (not built yet --
see docs/ROADMAP.md milestone 9): this directory holds only this feature's own
install-related bookkeeping (e.g. an install log written by `scripts/install.sh`), never
pipeline run state, config, or credentials. `code-review uninstall` removes it entirely,
so there is nothing left behind once the tool is gone.

Location is `$CODE_REVIEW_STATE_DIR` if set, else `~/.code-review`. The env var exists so
tests -- and `scripts/install.sh`, which reads the same override -- can point it at an
isolated temp directory instead of a real machine's home directory.
"""

from __future__ import annotations

import os
from pathlib import Path

STATE_DIR_ENV_VAR = "CODE_REVIEW_STATE_DIR"


def state_dir() -> Path:
    """Return this project's install-state directory, honoring `$CODE_REVIEW_STATE_DIR`."""
    override = os.environ.get(STATE_DIR_ENV_VAR)
    if override:
        return Path(override)
    return Path.home() / ".code-review"
