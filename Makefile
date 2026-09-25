# Bytecode caches go to .cache/ instead of __pycache__/ folders.
export PYTHONPYCACHEPREFIX := $(CURDIR)/.cache/pycache

.PHONY: check lint typecheck test

check: lint typecheck test

lint:
	uv run ruff check .

typecheck:
	uv run mypy .

test:
	uv run pytest
