#!/usr/bin/env sh
# Installs the `code-review` CLI onto PATH via `uv tool install`, without requiring the
# user to clone this repo or know any `uv`-specific commands (see issue #31). Delegates
# all virtual environment management, dependency resolution, and PATH-shim placement to
# `uv tool install` -- this script only checks the precondition (`uv` is callable),
# invokes it, and records this project's own install-state bookkeeping.
#
# Safe to re-run: `uv tool install` reinstalls/refreshes in place rather than erroring
# when `code-review` is already installed.
#
# Env overrides (for hermetic testing -- see tests/test_install_script.py):
#   CODE_REVIEW_INSTALL_SOURCE  package source passed to `uv tool install` (default: this
#                               project's GitHub repo, tracked via a `git+https` URL so
#                               `uv` fetches it directly -- no local clone required)
#   CODE_REVIEW_STATE_DIR       this project's own state directory (default: ~/.code-review)
#   UV_TOOL_DIR / UV_TOOL_BIN_DIR  read directly by `uv` itself
set -eu

SOURCE="${CODE_REVIEW_INSTALL_SOURCE:-git+https://github.com/khayweee/code-review}"
STATE_DIR="${CODE_REVIEW_STATE_DIR:-$HOME/.code-review}"

if ! command -v uv >/dev/null 2>&1; then
    echo "error: 'uv' is not installed or not on PATH." >&2
    echo "code-review is installed via uv -- install it first, then re-run this script:" >&2
    echo "  https://docs.astral.sh/uv/getting-started/installation/" >&2
    exit 1
fi

uv tool install "$SOURCE"

mkdir -p "$STATE_DIR"
printf '%s installed from %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$SOURCE" >>"$STATE_DIR/install.log"

echo ""
echo "code-review is installed and ready. Run 'code-review --help' to get started."
