"""GitHub wrapper via the `gh` CLI. Not built yet.

Planned shape: shell out to `gh pr create`, piping the body via stdin (`--body-file -`) to
avoid shell-escaping/length limits, and always pass an explicit `--repo`/PR number rather
than relying on `gh` inferring context from cwd.
"""
