# Architecture — offer-finder

Personal POC for the owner and a few approved friends. Read this before touching code; details of *why*
live in [DECISIONS.md](DECISIONS.md), diagrams in [diagrams/](diagrams/).

## 1. Goal and context

A Telegram bot that finds worthwhile second-hand deals on Kleinanzeigen.de
for **any product**. You describe what you want ("a good 65+ OLED TV around
Mainz under €700"), the bot asks at most a few questions, shows a search
spec for you to confirm, searches Kleinanzeigen, stores every listing, has
an LLM judge each one, and sends you the worthwhile ones with photos. For
ongoing monitoring you create Kleinanzeigen saved searches yourself; their
alert emails land in a dedicated Gmail inbox that the bot polls, and those
listings go through the same pipeline. Each listing is sent to you at most
once. **The SQLite database is the source of truth.**

## 2. Main components

All code is in the `offer_finder/` package (paths below are relative to it);
tests and fixtures in `tests/`, runtime data in `data/`. Run from the repo root
via `make` (`make bot`, `make search Q="…"`, …) — no install step.

| Module | Responsibility |
|---|---|
| `bot/app.py` | Bot process: builds the PTB `Application`, runs confirmed searches as background tasks, schedules the Gmail job. One `asyncio.Lock` makes sure only one unit of work scrapes at a time. |
| `bot/access.py` | `authorize()` at the start of every handler: private chats only, user row read from SQLite on each update, admin check for admin actions. A stranger's first message becomes a `pending` user and one Approve/Block message to the admins. `/users`, `/approve <id>`, `/block <id>`. |
| `bot/conversation.py` | `ConversationHandler`: COLLECTING → CONFIRMING (Search / Change / Cancel). Uses the user's home postcode/radius. *Search* reserves a run (`db.reserve_search_run`: one at a time per user, `USER_DAILY_SEARCHES` per 24 h for non-admins) before starting it. State in memory. |
| `bot/preferences.py` | `/settings`, `/home <PLZ>`, `/radius <km\|any>`: the caller's own defaults, falling back to `.env`. |
| `bot/status.py` | The live status message of a running search: one Telegram message edited in place (results page(s) n/N → reading listing pages n/N → evaluating n/N → done, or ❌ why it stopped). The pipeline reports `Progress` from the worker thread; edits happen at most every 3 s, and a failed edit never breaks the search. |
| `bot/notify.py` | The ranked pick list (`send_search_results` for a search, `send_new_picks` for Gmail), split at 4096 chars, with numbered `offer:<evaluation id>` buttons; records the picks **after** Telegram accepted the list. `send_offer()` = one offer's card (album/photo/text). Run summary text. |
| `bot/offers.py` | Handles the offer buttons: `authorize()`, then `db.evaluation_visible_to()` (a user only opens their own offers), then `send_offer()`. Works after restarts. |
| `runner.py` | Builds real dependencies from `Settings`; `run_search_once()` and `poll_gmail_once()` — synchronous, shared by bot (worker thread) and CLI. |
| `cli.py` | Dev CLI: `search`, `gmail-auth`, `gmail-poll`, `stats`, `export`. |
| `core/search_spec.py` | `next_turn()` turns the conversation into a `SearchSpec` (or one follow-up question): broad category query, every named place with its PLZ, one radius (`DEFAULT_RADIUS_KM` if none given), and a `quality_guide` (top tier / mid / avoid for the category and budget, from the LLM's product knowledge). The buyer is assumed to want the best product the budget buys. `describe()` is the confirmation text. |
| `core/pipeline.py` | `Pipeline.run_search()` / `process_email()` → store → fetch details → `evaluate_pending()`. |
| `core/parse.py` | Regex parsing of search cards and detail pages (text, price, location, date, gallery images, structured fields like `Zustand`/`Versand`); `extract_facts()` = evaluation layer 1. |
| `core/checks.py` | `compute_checks()` = evaluation layer 2: distance to the nearest searched place, budget, missing fields, hard fails. |
| `core/evaluator.py` | Evaluation layer 3: prompt + strict schema, `evaluate()`, `verify_evidence()`, `cap_score()` (tier caps), `is_recommended()`. |
| `core/redflags.py` | Cheap keyword hints (shipping only, Nachnahme, PayPal F&F, defect words) handed to the LLM as heuristics. |
| `core/email_parse.py` | Alert email → listing links (`ListingRef`); `match_spec()` maps an email to a saved `SearchSpec`. |
| `core/distance.py` | Haversine over `data/plz_de.csv` (PLZ → lat/lon, no place names). |
| `services/fetch.py` | **The only network path to Kleinanzeigen**: `RequestGate` (delay + jitter, per-run request cap, stop after N bad responses), `fetch_page()` (kill switch, dry run, cache; search pages expire after `SEARCH_CACHE_TTL_S`). |
| `services/search_url.py` | `SearchSpec` → one `/s-suchanfrage.html?keywords=…&locationStr=<plz>&radius=…&minPrice=…&maxPrice=…` per place |
| `services/llm.py` | OpenAI-compatible transport (Gemini by default) + `complete_validated()` (pydantic validation, one retry). |
| `services/gmail.py` | Read-only Gmail client (OAuth installed-app flow) and MIME helpers. |
| `storage/db.py` / `core/models.py` / `config.py` | SQLite schema + queries / pydantic models / `.env` settings. |

## 3. Main flows

**Access.** Every update goes through `access.authorize()`. Admins are the IDs
in `TELEGRAM_ALLOWLIST` (synced into `users` whenever the DB is opened).
Anyone else is `pending` until an admin approves them, or `blocked`. Who may
see an evaluation is one rule (`db._VISIBLE`): the owner of its search, plus
admins for alert-email evaluations (no spec, or an admin's spec).

Sequence diagrams: [diagrams/flows.md](diagrams/flows.md). Component view: [diagrams/overview.md](diagrams/overview.md).

**Interactive search.** Text message → `next_turn()` (LLM) asks ≤3 questions
→ spec shown with buttons (incl. "Best looks like: …") → *Search* →
`search_and_notify()` background task: under the lock, in a worker thread,
`run_search_once()` stores the spec, fetches one search page **per place**,
merges the cards by listing id, stores **every** card, marks cards outside
the price range or outside every place's radius `out_of_range` (no detail
fetch), fetches remaining detail pages, evaluates each fetched listing →
back on the event loop, `send_search_results()` sends one ranked list of
that spec's picks (picks listed before are marked "seen before") → summary
message with one Kleinanzeigen link per place to save as a saved search.
Tapping a number under the list → `offers.py` → `send_offer()`.

**Gmail monitoring.** `JobQueue` every `GMAIL_POLL_INTERVAL_S` →
`poll_gmail_once()`: list messages for `GMAIL_QUERY`, skip IDs already in
`gmail_messages`, extract listing links, match the email to a saved spec **of
an admin** by its query words (else evaluate without a spec), `process_email()` → same
fetch/evaluate path → `send_new_picks()` (a list of picks not listed before) to `TELEGRAM_NOTIFY_CHAT_ID`
(default: first allowlisted user). An email is marked processed only when
all its listings were fetched and evaluated, so budget stops and LLM
failures resume on the next poll.

**Evaluation (both flows).** Three layers, kept separate in code and storage:

1. **Facts** (`extract_facts`): what the listing states — price only from
   the price field (never a "Neupreis" in the text), PLZ, condition
   (structured `Zustand` field first), posting age, photos, structured
   fields, red-flag hints. Unknown stays `null`.
2. **Deterministic checks** (`compute_checks`): distance, within radius,
   within budget, € over budget, missing fields, hard-fail reasons.
3. **LLM judgment**: `product_assessment` (verdict, requirements match and
   `tier` = top / upper_mid / mid / budget / unknown, judged against the
   spec's quality guide, regardless of price) and `deal_assessment` (verdict, regardless of how
   good the product is) as separate objects, highlights/concerns each with
   evidence (verbatim quote or `fact:<name>`), unknowns, score 0–100,
   confidence. `verify_evidence()` checks every claim in Python; unverified
   claims are stored but never shown. No market-value claims (there's no
   market data).

Python then caps the score by tier (`mid` ≤ 70, `budget` ≤ 55; the raw
response keeps what the model said), so a cheap generic product can't
outscore a top-tier one.

`recommended = score ≥ RECOMMENDATION_THRESHOLD and no hard fail and requirements_match ≠ "mismatch"`.

## 4. Data model (`storage/db.py`)

| Table | Key | Purpose |
|---|---|---|
| `search_specs` | `id` | Confirmed specs (JSON) per chat; `active` flag used for Gmail matching. Specs stored before 2026-10-06 have one `plz` and are converted to `locations` when loaded. |
| `listings` | `listing_id` (Kleinanzeigen ad id) | Every listing ever seen, raw scraped fields, `image_urls`, `attributes` JSON, `source` (`search`/`gmail`, first seen wins), `status`. |
| `evaluations` | unique `(listing_id, IFNULL(search_spec_id,0))` | One evaluation per listing per spec (or per "no spec"): score, recommended, product/deal verdicts, `input_json` (facts + checks + exact prompt), `judgment_json`, raw response, model, prompt version. |
| `notifications` | `(listing_id, chat_id)` | A listing appeared in a pick list for this chat. Gmail lists only show picks without a row here (each listed once); search lists show all picks of the search and mark these as "seen before". |
| `gmail_messages` | `message_id` | Emails fully processed (listing ids, matched spec, error). |
| `users` | `telegram_id` | Everyone who wrote to the bot: `status` pending/approved/blocked, `is_admin` (synced from `TELEGRAM_ALLOWLIST`), optional `home_plz` / `radius_km` (0 = any distance). |
| `search_runs` | `id` | One row per confirmed search: `chat_id`, `status` queued/running/done/failed/interrupted, `search_spec_id`. The daily limit counts these. |

Schema changes are additive only (new tables, new nullable columns), so an
older release still runs against a newer database.

Listing status: `discovered` → `fetched` | `out_of_range` | `fetch_failed`.
A `discovered` listing is fetched on the next run that sees it (e.g. after
the request budget ran out). A fetched listing without an evaluation for a
spec is *pending* and is evaluated on the next run (e.g. after a 429).

## 5. External services

| Service | How | Notes |
|---|---|---|
| Kleinanzeigen.de | HTML scraping with `curl_cffi` (Chrome impersonation), regex parsing | Search-form endpoint verified live 2026-09-25: keywords, PLZ, radius and max price are applied by the site; results from outside the radius are appended after the in-radius ones (filtered client-side). Parsers raise on unknown layouts; an empty results page is recognised by its `srchrslt-` container. |
| Gemini (OpenAI-compatible endpoint) | `httpx`, `response_format: json_schema` (strict) or `json_object` fallback | Free tier: requests/minute are low → `LLM_MIN_INTERVAL_S` between evaluations; failures leave listings pending. |
| Telegram Bot API | python-telegram-bot 22, long polling, `JobQueue` | Photos are sent by URL (Telegram fetches from `img.kleinanzeigen.de`); text fallback if rejected. |
| Gmail API | `google-api-python-client`, OAuth 2.0 installed-app flow, scope `gmail.readonly` | Token in `data/gmail_token.json` (gitignored). |

## 6. Important design decisions

See [DECISIONS.md](DECISIONS.md). The load-bearing ones: DB as source of
truth; store every listing; LLM judges, Python computes; claims need
verified evidence; product vs deal kept separate; one fetch choke point;
notifications recorded only after a successful send.

## 7. Logs

`runner.setup_logging()` (bot and CLI) writes to the terminal and to a rotating
file, `LOG_PATH` (default `data/logs/offer_finder.log`, gitignored). At INFO
there is one line per step, tagged with the request: `chat <id>` (conversation),
`search <spec id>` (results page, detail fetches, one line per evaluated listing
with score and why it was or wasn't recommended, `done in …`), and
`email <message id>` / `gmail` for alert emails. Problems are WARNINGs.
`LOG_LEVEL=DEBUG` adds every Kleinanzeigen fetch and LLM call. `httpx`/`httpcore`
are always WARNING, because their INFO lines contain the bot token.

## 8. Testing and local setup

- `make check` = ruff + mypy + pytest. Tests never touch the network: fetch,
  LLM, Gmail and Telegram are injected fakes (`fetcher=`, `provider=`,
  `client=`, fake bot). SQLite is real (in-memory / `tmp_path`).
- `tests/` mirrors the package layout; shared fakes and factories are in
  `tests/helpers/`.
- Offline end-to-end tests cover both flows: `tests/bot/test_app.py`
  (confirmed spec → real fetch layer in DRY_RUN over fixture HTML → DB →
  evaluation → fake Telegram; alert email → … → Telegram exactly once).
- Evaluator replay: every evaluation stores the exact prompt, raw response
  and `EVALUATOR_PROMPT_VERSION`; `make export` writes a CSV with an empty
  `my_label` column for building a hand-labelled set.
- How to run it for real: [README.md](../README.md#how-to-test-locally).

## 9. Deployment

One Docker Compose service ([DEPLOYMENT.md](DEPLOYMENT.md)). The image holds
only the code and `plz_de.csv`; settings, secrets and data are mounted
(`/data`, `/secrets`). The bot itself writes a heartbeat file every 60 s (the
container health check) and a nightly SQLite backup (`storage/backup.py`,
online backup API, keeps `BACKUP_KEEP`). `cli backup` / `cli verify-db` are the
same functions for deploys, restores and `make backup-pull`.
