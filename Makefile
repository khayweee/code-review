.PHONY: sync fmt lint test check run install-dev prune-branches

sync:
	uv sync

fmt:
	uv run ruff format .

lint:
	uv run ruff check .
	uv run mypy src

test:
	uv run pytest

check: fmt lint test

run:
	uv run code-review --help

install-dev:
	-uv tool uninstall code-review
	CODE_REVIEW_INSTALL_SOURCE="$(CURDIR)" ./scripts/install.sh

prune-branches:
	git fetch --prune
	git branch -vv | awk '/: gone]/{print $$1}' | xargs -r git branch -D
