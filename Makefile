# Run everything from the repo root. Bytecode caches go to .cache/ instead of __pycache__/ folders.
export PYTHONPYCACHEPREFIX := $(CURDIR)/.cache/pycache

.PHONY: check lint typecheck test bot search gmail-auth gmail-poll stats export backup \
	docker-smoke-test push-secrets deploy server-logs server-status backup-pull

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

backup:  ## online SQLite backup into data/backups (local, without Docker)
	uv run python -m offer_finder.cli backup

# --- Docker -------------------------------------------------------------------------
# docs/DEPLOYMENT.md explains the setup. Remote targets need SERVER=user@host.

docker-smoke-test:  ## build + isolated container checks; needs a TEST bot token in runtime/test.env
	./deploy/smoke-test.sh

SERVER ?=
REMOTE_DIR ?= offer-finder
REMOTE_COMPOSE = docker compose -f $(REMOTE_DIR)/compose.yaml

_server:
	@test -n "$(SERVER)" || { echo "Set SERVER=user@host"; exit 1; }

push-secrets: _server  ## copy .env and data/gmail_token.json to the server (mode 600)
	ssh $(SERVER) 'umask 077 && mkdir -p $(REMOTE_DIR)/runtime/data $(REMOTE_DIR)/runtime/secrets'
	ssh $(SERVER) 'umask 077 && cat > $(REMOTE_DIR)/runtime/app.env' < .env
	@if [ -f data/gmail_token.json ]; then \
		ssh $(SERVER) 'umask 077 && cat > $(REMOTE_DIR)/runtime/secrets/gmail_token.json' < data/gmail_token.json; \
		echo "Copied .env and gmail_token.json."; \
	else echo "Copied .env (no data/gmail_token.json here, so Gmail stays off unless you add it)."; fi

deploy: _server  ## ship the last commit, back up the DB, rebuild and restart; never touches runtime/
	@git diff --quiet && git diff --cached --quiet || { echo "Uncommitted changes: deploy ships the last commit only."; exit 1; }
	$(MAKE) check
	ssh $(SERVER) 'if $(REMOTE_COMPOSE) ps --status running --services 2>/dev/null | grep -qx bot; then \
		$(REMOTE_COMPOSE) exec -T bot python -m offer_finder.cli backup; else echo "bot not running, no backup taken"; fi'
	ssh $(SERVER) 'mkdir -p $(REMOTE_DIR) && rm -rf $(REMOTE_DIR)/offer_finder'
	git archive --format=tar HEAD | ssh $(SERVER) 'tar -x -C $(REMOTE_DIR)'
	ssh $(SERVER) '$(REMOTE_COMPOSE) up -d --build --wait --wait-timeout 180'
	@echo "Deployed $$(git rev-parse --short HEAD). A healthy container only means the process runs:"
	@echo "check a real search and the logs (make server-logs) before calling it done."

server-logs: _server  ## follow the bot's logs
	ssh -t $(SERVER) '$(REMOTE_COMPOSE) logs -f --tail=200 bot'

server-status: _server  ## container state and health, newest backups
	ssh $(SERVER) '$(REMOTE_COMPOSE) ps; echo; echo "Newest backups:"; ls -1t $(REMOTE_DIR)/runtime/data/backups 2>/dev/null | head -3'

backup-pull: _server  ## fresh backup on the server -> local/backups/, then verified locally
	mkdir -p local/backups
	@file=$$(ssh $(SERVER) '$(REMOTE_COMPOSE) exec -T bot python -m offer_finder.cli backup' | tail -n1 | tr -d '\r'); \
	test -n "$$file" || { echo "No backup was made (is the bot running?)"; exit 1; }; \
	scp -q "$(SERVER):$(REMOTE_DIR)/runtime/data/$${file#/data/}" local/backups/ && \
	uv run python -m offer_finder.cli verify-db "local/backups/$$(basename $$file)"
