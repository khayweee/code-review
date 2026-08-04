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

**To install `code-review` onto your `PATH`** (no clone required -- the script fetches
the package directly via `uv tool install`):

```bash
curl -fsSL https://raw.githubusercontent.com/khayweee/code-review/main/scripts/install.sh | sh
```

The script checks that `uv` is callable first and fails with an actionable message if it
isn't. It's safe to re-run at any time (idempotent reinstall). Once installed:

```bash
code-review --help
code-review update      # pull in the latest version
code-review uninstall   # remove the tool and its state directory
```

**To work on this repo instead**, clone it and run via `uv`:

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
  cli.py           Typer entry point (`review`, `update`, `uninstall`)
  config.py        trusted-vs-descriptive config split
  install_state.py install-lifecycle state directory (`~/.code-review`)
  agent/           Agent abstraction — shells out to a coding-agent CLI (starting with `claude`)
  pipeline/        Step protocol, findings model, and the executor
  steps/           the pipeline steps themselves — see below
  scm/             GitHub wrapper (via the `gh` CLI)
scripts/
  install.sh       one-shot install script (`uv tool install` under the hood)
tests/             mirrors src/code_review/ package-for-package
```
