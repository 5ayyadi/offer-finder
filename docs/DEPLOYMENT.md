# Deployment — Docker Compose on one Linux host

The bot runs as one container on any Linux host with Docker Engine and the
Compose plugin. Nothing in the repo is specific to a cloud provider. The bot
only makes outbound connections (Telegram, Gemini, Gmail API, Kleinanzeigen),
so the host needs no open port except SSH.

```
host: ~/offer-finder/                  code from `git archive` (make deploy), replaced on each deploy
        compose.yaml, Dockerfile, offer_finder/, data/plz_de.csv, …
      ~/offer-finder/runtime/          state, never touched by a deploy (gitignored, mode 700)
        app.env                        settings + secrets (copy of your .env), mode 600
        secrets/gmail_token.json       Gmail token made on the laptop, mode 600
        data/                          offer_finder.db, cache/, logs/, backups/, heartbeat
container: /data = runtime/data, /secrets = runtime/secrets, runs as a non-root user
```

**Only one bot may poll a Telegram token.** Stop `make bot` on the laptop before
the container runs (otherwise Telegram answers `409 Conflict`). For CLI commands
in a running container use `docker compose exec bot python -m offer_finder.cli …`;
`docker compose run --rm bot <command>` is fine only with an explicit command
(without one it starts a second bot).

## What each check proves

| Command | Runs | Proves |
|---|---|---|
| `make check` | ruff, mypy, pytest; offline | code and logic, incl. access control and isolation |
| `make docker-smoke-test` | real `compose.yaml`, isolated temp data, a **test** bot | image builds, container gets healthy, data survives recreate and a crash, backup restores, non-root, no secrets in the image |
| healthy container (`make server-status`) | heartbeat file < 3 min old | the process and its event loop are alive — **not** that searches, Gmail or notifications work |
| a real search + a real alert email on the server | you, in Telegram | the bot actually works |

## 1. Local Docker with a test bot

1. In Telegram, create a second bot with @BotFather (e.g. `offer_finder_test_bot`).
   Nobody else knows it, so it can't message real users.
2. Write `runtime/test.env` (gitignored):
   ```
   TELEGRAM_BOT_TOKEN=<test bot token>
   TELEGRAM_ALLOWLIST=[<your Telegram id>]
   LLM_API_KEY=<your Gemini key>
   HOME_PLZ=<your PLZ>
   DRY_RUN=false
   ```
3. Start it with persistent data in `runtime/data`:
   ```bash
   mkdir -p runtime/data runtime/secrets && chmod 700 runtime runtime/data runtime/secrets
   OFFER_FINDER_ENV_FILE=runtime/test.env docker compose up -d --build --wait
   docker compose logs -f bot
   ```
4. Chat with the test bot: one search, tap one offer button. Then
   `docker compose down` and `up -d` again: your earlier picks are still there.

## 2. Smoke test

```bash
make docker-smoke-test      # reads runtime/test.env (or SMOKE_ENV_FILE=…)
```

Builds the image and runs the real `compose.yaml` as a separate project
(`offer-finder-smoke`, own container name and image tag) on a temporary
folder under `.cache/`, with `DRY_RUN`, `KILL_SWITCH` on and Gmail off.
Runs the relevant pytest suites first, then prints `PASS`/`FAIL` per check and
exits non-zero if any failed. It refuses to run with the token from `.env`,
never touches `runtime/` or `data/`, and removes only its own container and
temp folder. Bot tokens are masked in any log lines it prints.

## 3. Backups and restore

The bot writes `runtime/data/backups/offer_finder-<UTC time>.db` every night at
`BACKUP_HOUR_UTC` (2:00 UTC) and keeps `BACKUP_KEEP` (14). `make deploy` takes
one before every deploy. Backups use SQLite's online backup API, so they're
consistent while the bot runs.

- Manual backup: `docker compose exec bot python -m offer_finder.cli backup`
- Check one on the laptop: `uv run python -m offer_finder.cli verify-db <file>` (prints `ok`)
- To the laptop: `make backup-pull SERVER=…` (→ `local/backups/`, verified)

**Restore** (moves the current DB aside, deletes nothing):
```bash
cd ~/offer-finder
docker compose run --rm --no-deps bot python -m offer_finder.cli verify-db /data/backups/<file>   # must print ok
docker compose stop bot
mkdir -p runtime/data/pre-restore-$(date +%Y%m%d-%H%M)
mv runtime/data/offer_finder.db* runtime/data/pre-restore-$(date +%Y%m%d-%H%M)/
cp runtime/data/backups/<file> runtime/data/offer_finder.db
docker compose up -d --wait
docker compose exec bot python -m offer_finder.cli verify-db /data/offer_finder.db
```
Data written after that backup is lost.

## 4. Choosing the host (before creating anything)

Any Linux VM with ~1 GB RAM works. For Azure for Students:

1. *Subscriptions*: the offer is "Azure for Students" and **Spending limit: On**.
2. Cloud Shell: allowed regions — `az policy assignment list -o table` (and its
   parameters); sizes — `az vm list-skus --location <region> --size Standard_B --all -o table`.
   B1s or B2ats v2 must be available in an allowed region.
3. *Create VM* → Ubuntu 24.04 → that size → SSH key → inbound ports: SSH only →
   go to **Review + create and stop**. Write down **every cost line the portal
   shows**. Public estimates (VM within the free hours, a 64 GiB P6 disk free for
   12 months, ≈ $3.65/month for a Standard IPv4) are only estimates; the portal
   decides. If anything else is charged, decide with those numbers.
4. After creating it: a budget alert in Cost Management, and after 48 h check
   that the charges match.

**Kleinanzeigen IP test** (cloud IPs are often blocked): during the server setup
below (step 6), before the bot runs, ship the code and run twice with different products:
```bash
git archive --format=tar HEAD | ssh user@host 'mkdir -p offer-finder && tar -x -C offer-finder'   # on the laptop
cd ~/offer-finder && docker compose run --rm --no-deps -e DRY_RUN=false bot \
  python -m offer_finder.cli search "<product>"                                                  # on the server
```
Pass = results and listing pages load, no `blocked`/`throttled` in the output.
Fail = delete the VM **and its public IP**, and use another host (e.g. a
Raspberry Pi at home: the same steps work there).

## 5. Server setup (first time)

1. Install Docker Engine + the Compose plugin from Docker's apt repository, and
   add your user to the `docker` group (log out and in again).
2. If `id -u` on the server isn't 1000, write `APP_UID=<uid>` and `APP_GID=<gid>`
   into `~/offer-finder/.env` (compose reads it; the bot doesn't).
3. On Google Cloud's OAuth consent screen, set the app to **In production**
   (in Testing, Gmail tokens expire after 7 days). Then on the laptop:
   `make gmail-auth`.
4. From the laptop: `make push-secrets SERVER=user@host` (copies `.env` →
   `runtime/app.env` and `data/gmail_token.json`, both mode 600; never the DB
   or `gmail_credentials.json`). Set `DRY_RUN=false` in `.env` first.
5. Optional, keep your history: copy `data/offer_finder.db` to
   `runtime/data/offer_finder.db` on the server (with the laptop bot stopped).
6. Run the Kleinanzeigen IP test above.
7. **Stop the laptop bot**, then `make deploy SERVER=user@host`.
8. Live checks: one real search in Telegram (list arrives, an offer button
   opens), one real alert email processed (`gmail` lines in `make server-logs`),
   then the two-account test (approve, own results only, limits, block).

## 6. Routine

| Command | Does |
|---|---|
| `make deploy SERVER=…` | refuses uncommitted changes, runs `make check`, backs up the DB, ships the last commit, rebuilds and restarts, waits until healthy |
| `make server-logs SERVER=…` | follow the logs |
| `make server-status SERVER=…` | state, health, newest backups |
| `make backup-pull SERVER=…` | fresh backup to `local/backups/`, verified |
| `make push-secrets SERVER=…` | after changing `.env` or the Gmail token; then `make deploy` |

**Rollback:** `git switch --detach <previous commit>` → `make deploy SERVER=…` →
`git switch -`. Schema changes are additive only (new tables, new nullable
columns), so the older code runs against the newer database. If a release
ever changes data incompatibly, also restore the backup that `make deploy`
took just before it (section 3); data written since then is lost.

## Troubleshooting

- `409 Conflict` in the logs: another process polls the same token (the laptop bot?).
- `Telegram rejected TELEGRAM_BOT_TOKEN`: wrong token in `runtime/app.env`. Tokens are
  redacted from all logs as `<bot token>`.
- `Permission denied` under `/data`: the mounted folders must belong to `APP_UID`
  (step 2). Docker creates missing mount folders as root, so create them first
  (`make push-secrets` does).
- `env file …/runtime/app.env not found`: run `make push-secrets` (or, locally, set
  `OFFER_FINDER_ENV_FILE`).
