# offer-finder

A personal Telegram bot that finds worthwhile second-hand deals on
Kleinanzeigen.de, for any product. Tell it what you want ("a good 65+ inch
OLED TV around Mainz under €700"). It asks a question or two if needed,
shows you the search it will run, and once you confirm it searches
Kleinanzeigen, stores every listing it finds, has an LLM judge each one on
two separate things (is the product good for you, and is the listing a
good deal), and sends you the worthwhile ones with photos, score, reasons,
concerns and a link. It never sends the same listing twice.

For ongoing monitoring you save searches on Kleinanzeigen yourself. Their
alert emails go to a dedicated Gmail inbox, which the bot reads (read-only
OAuth) and feeds through the same pipeline.

```
Telegram ──▶ conversation ──▶ search spec ──▶ Kleinanzeigen ──▶ SQLite ──▶ evaluator ──▶ Telegram
Kleinanzeigen saved search ──▶ Gmail ──▶ listing links ─────────────┘
```

Code lives in `offer_finder/` (`bot/`, `core/`, `services/`, `storage/`, plus
`config.py`, `runner.py`, `cli.py`); tests and fixtures in `tests/`; runtime data in
`data/`. Design, data model and decisions: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md),
[docs/DECISIONS.md](docs/DECISIONS.md), [docs/diagrams/](docs/diagrams/).
All commands run from the repo root; `make` targets wrap them (see `Makefile`).

## How to test locally

### 0. Offline (no credentials)

Needs Python ≥ 3.11 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
make check          # ruff + mypy + pytest; never touches the network
```

### 1. Credentials

| What | Where to get it | Goes into |
|---|---|---|
| Gemini API key (free tier) | aistudio.google.com → Get API key | `.env`: `LLM_API_KEY` |
| Telegram bot token | Telegram → @BotFather → `/newbot` | `.env`: `TELEGRAM_BOT_TOKEN` |
| Your numeric Telegram user id | Telegram → @userinfobot | `.env`: `TELEGRAM_ALLOWLIST=[<id>]` |
| Gmail OAuth client (only for monitoring) | see step 4 | `data/gmail_credentials.json` |

```bash
cp .env.example .env
# edit .env: LLM_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_ALLOWLIST=[<your id>],
#            HOME_PLZ=<your PLZ>, DRY_RUN=false
```

`DRY_RUN=true` (the default) never touches the network, only the local cache.
`KILL_SWITCH=true` stops all fetching. `MAX_REQUESTS_PER_RUN` caps Kleinanzeigen
requests per search/poll (1 search page + detail pages).

### 2. A real search without Telegram

```bash
make search Q="good 65+ inch OLED TV around Mainz under 700"
# = uv run python -m offer_finder.cli search "..."
```

Prints the search spec, every evaluated listing (score, product/deal verdict,
hard fails, link) and a summary. Add `NOTIFY=1` to also send the recommended
ones to your Telegram chat. A run with 20 listings takes a minute or two: the
request gate waits 4–6 s between detail pages, and evaluations are spaced by
`LLM_MIN_INTERVAL_S` for the free tier.

### 3. The Telegram bot

```bash
make bot            # = uv run python -m offer_finder.bot
```

Message your bot: describe what you want → answer its question(s) → press
**Search**. You get one message per recommended listing, then a summary with
the Kleinanzeigen link for saving the search. Only users in
`TELEGRAM_ALLOWLIST` get any reply.

### 4. Gmail monitoring

1. Use a dedicated Gmail address for the Kleinanzeigen account whose saved-search
   alerts you want monitored (or forward the alerts to it).
2. Google Cloud Console → new project → enable **Gmail API** → OAuth consent
   screen (External, add that Gmail address as a test user) → Credentials →
   **Create OAuth client ID → Desktop app** → download the JSON as
   `data/gmail_credentials.json`.
3. One-time consent in the browser (writes `data/gmail_token.json`):
   ```bash
   make gmail-auth
   ```
4. On Kleinanzeigen, save a search with email alerts (e.g. from the link the bot
   sends after a search). Using the same keywords as a bot search lets alerts
   be judged against that search's requirements.
5. After an alert email arrived, run one poll cycle and look at the output:
   ```bash
   make gmail-poll            # add NOTIFY=1 to send to Telegram
   ```
   If it reports emails *without listing links*, the alert format differs from
   `tests/fixtures/gmail_alert_email.html`. Send me one redacted alert email and
   I'll adjust `offer_finder/core/email_parse.py`.
6. Set `GMAIL_ENABLED=true` and restart `make bot`: it polls every
   `GMAIL_POLL_INTERVAL_S` seconds and sends new recommendations to
   `TELEGRAM_NOTIFY_CHAT_ID` (default: first allowlisted user).

While the OAuth app is in Google's "Testing" status, its refresh token expires
after 7 days. Re-run `gmail-auth` when polling starts failing, or set the app to
"In production" (fine for personal use, you'll see an "unverified app" warning).

### 5. Inspecting results

```bash
make stats                   # row counts per table / listing status
make export OUT=labels.csv   # every evaluation + empty my_label column
sqlite3 data/offer_finder.db               # everything, including the exact LLM prompt per evaluation
```

Fill `my_label` in the export for a few dozen real listings to get a small
labelled set for checking the evaluator later.

## Known gaps

- The Gmail alert parser is built against a hand-made fixture; it needs one real
  alert email to confirm.
- Gemini retires model names without warning. `gemini-2.5-flash-lite` was
  closed to new keys by 2026-09-25. The default `gemini-3.5-flash-lite` was
  verified that day with the strict JSON schema, for both spec building and
  evaluation. Newer non-lite models (`gemini-3.8-flash`, `gemini-3.7-flash`)
  mostly answered `503 high demand` on the free tier at that time. If the
  bot says "the AI service returned an error (HTTP 404)", change `LLM_MODEL`.
  If Gemini rejects the schema, set `LLM_RESPONSE_FORMAT=json_object`.
- Only the first search results page (~25 listings) is read per search.
