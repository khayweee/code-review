"""Install-lifecycle state directory: holds only install-related bookkeeping (e.g. the
install log written by `scripts/install.sh`), never pipeline run state, config, or
credentials. `code-review uninstall` removes it entirely.

Location is `$CODE_REVIEW_STATE_DIR` if set, else `~/.code-review`; the env var lets tests
and `scripts/install.sh` point it at an isolated temp directory.
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
