"""Install-lifecycle state directory: install-related bookkeeping (e.g. the install log
written by `scripts/install.sh`), plus, in a separate `runs/` subdirectory, per-run pipeline
log files (`run_log.py`) -- kept under the same root only to reuse this module's directory/
`$CODE_REVIEW_STATE_DIR`-override plumbing, not because a run log is install-lifecycle data
itself. Never config or credentials. `code-review uninstall` removes this whole directory,
`runs/` included.

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
