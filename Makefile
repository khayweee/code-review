.PHONY: sync fmt lint test check run install-dev

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
	CODE_REVIEW_INSTALL_SOURCE="git+file://$(CURDIR)" ./scripts/install.sh
