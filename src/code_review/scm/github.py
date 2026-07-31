"""GitHub wrapper via the `gh` CLI — Milestone 7 (see docs/ROADMAP.md).

Note: `gh` is not installed on this machine as of the scaffold — install it before
starting this milestone. Planned shape: shell out to `gh pr create`, piping the body via
stdin (`--body-file -`) to sidestep shell-escaping/length limits, and always pass an
explicit `--repo`/PR number rather than relying on `gh` inferring context from cwd.
"""
