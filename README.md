# code-review

A personal, from-scratch Python rebuild of a [no-mistakes](../no-mistakes)-style agentic
code-review/gating pipeline: detect the intent behind a change, review it for correctness
and risk, check test sufficiency, and open a PR with evidence — with a human approval gate
whenever an agent isn't confident enough to act alone.

This is not a port. It borrows the design lessons from studying that Go tool (see
[`docs/ROADMAP.md`](docs/ROADMAP.md)) but is being built step by step, in Python, so the
control flow is fully understood at every stage rather than inherited as a black box.

**Status:** scaffold only. No pipeline logic exists yet — see `docs/ROADMAP.md` for the
milestone plan and `AGENTS.md` for the current milestone.

## Quick start

```bash
uv sync
uv run code-review --help
uv run pytest
```

Or via the Makefile: `make sync`, `make check` (format + lint + test), `make run`.

## Repo layout

```
src/code_review/
  cli.py         Typer entry point
  config.py      trusted-vs-descriptive config split (not built yet)
  agent/         Agent abstraction — shells out to a coding-agent CLI (starting with `claude`)
  pipeline/      Step protocol + executor (fixed step order, fix/approval loop)
  steps/         intent, review, test_sufficiency, pr
  scm/           GitHub (`gh` CLI) wrapper
```

See `AGENTS.md` for the harness/contributor conventions and `docs/ROADMAP.md` for the
build order and the design lessons this project is carrying over.
