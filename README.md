# code-review

An agentic code-review and gating pipeline for your own changes. It reads a diff, works out
what the change was meant to do, reviews it for correctness and risk, checks the tests are
good enough to catch a regression, and opens a PR with that evidence attached — stopping to
ask a human whenever an agent isn't confident enough to act alone.

**Status:** early. The package scaffold, CLI entry point, and tooling exist; the pipeline
logic does not. See [`docs/ROADMAP.md`](docs/ROADMAP.md) for the build order,
[`docs/GLOSSARY.md`](docs/GLOSSARY.md) for what the terminology means, and
[`AGENTS.md`](AGENTS.md) for the milestone currently in progress.

## What it aims to do

Five things, in this order:

1. **Intent** — establish what the change was supposed to achieve, from an explicit
   `--intent` description (inferring it from context comes later).
2. **Review** — a structured pass over the full diff for correctness and alignment with
   that intent, producing findings rather than prose.
3. **Risk** — a required risk level and rationale on the same schema as the review, so a
   review can't come back without one.
4. **Test sufficiency** — decide whether existing tests would catch a regression here,
   and if not, say what's missing.
5. **PR** — open a pull request whose body carries the intent, the risk verdict, and what
   the pipeline actually checked.

Two properties hold throughout: the step order is fixed in code so nothing can ship
unreviewed, and anything unclassified defaults to asking a human instead of acting.

## Install and run

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
uv run code-review --help
```

## Development

```bash
make sync     # uv sync
make check    # ruff format + ruff check + mypy + pytest
make test     # pytest only
```

Run `make check` before pushing — CI runs the same sequence.

## Layout

```
src/code_review/
  cli.py         Typer entry point
  config.py      trusted-vs-descriptive config split
  agent/         Agent abstraction — shells out to a coding-agent CLI (starting with `claude`)
  pipeline/      Step protocol, findings model, and the executor
  steps/         intent, review, test_sufficiency, pr
  scm/           GitHub wrapper (via the `gh` CLI)
tests/           mirrors src/code_review/ package-for-package
```

Contributor and agent conventions live in [`AGENTS.md`](AGENTS.md).
