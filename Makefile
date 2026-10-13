# Run everything from the repo root. Bytecode caches go to .cache/ instead of __pycache__/ folders.
export PYTHONPYCACHEPREFIX := $(CURDIR)/.cache/pycache

.PHONY: check lint typecheck test bot search gmail-auth gmail-poll stats export

check: lint typecheck test

lint:
	uv run ruff check .

typecheck:
	uv run mypy .

test:
	uv run pytest

bot:  ## run the Telegram bot
	uv run python -m offer_finder.bot

search:  ## make search Q="65+ OLED TV around Mainz under 700" [NOTIFY=1]
	uv run python -m offer_finder.cli search "$(Q)" $(if $(NOTIFY),--notify)

gmail-auth:
	uv run python -m offer_finder.cli gmail-auth

gmail-poll:  ## make gmail-poll [NOTIFY=1]
	uv run python -m offer_finder.cli gmail-poll $(if $(NOTIFY),--notify)

stats:
	uv run python -m offer_finder.cli stats

export:  ## make export OUT=labels.csv
	uv run python -m offer_finder.cli export "$(or $(OUT),labels.csv)"
