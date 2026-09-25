# Architecture — offer-finder

Personal POC, one user. Read this before touching code; details of *why*
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
| `bot/conversation.py` | `ConversationHandler`: COLLECTING → CONFIRMING (Search / Change / Cancel). Allowlist enforced by handler filters and in the button handler. State in memory. |
| `bot/notify.py` | Reads unsent recommendations from the DB, sends album/photo/text, records the notification **after** Telegram accepted it. Run summary text. |
| `runner.py` | Builds real dependencies from `Settings`; `run_search_once()` and `poll_gmail_once()` — synchronous, shared by bot (worker thread) and CLI. |
| `cli.py` | Dev CLI: `search`, `gmail-auth`, `gmail-poll`, `stats`, `export`. |
| `core/search_spec.py` | `next_turn()` turns the conversation into a `SearchSpec` (or one follow-up question); `describe()` is the confirmation text. |
| `core/pipeline.py` | `Pipeline.run_search()` / `process_email()` → store → fetch details → `evaluate_pending()`. |
| `core/parse.py` | Regex parsing of search cards and detail pages (text, price, location, date, gallery images, structured fields like `Zustand`/`Versand`); `extract_facts()` = evaluation layer 1. |
| `core/checks.py` | `compute_checks()` = evaluation layer 2: distance, budget, missing fields, hard fails. |
| `core/evaluator.py` | Evaluation layer 3: prompt + strict schema, `evaluate()`, `verify_evidence()`, `is_recommended()`. |
| `core/redflags.py` | Cheap keyword hints (shipping only, Nachnahme, PayPal F&F, defect words) handed to the LLM as heuristics. |
| `core/email_parse.py` | Alert email → listing links (`ListingRef`); `match_spec()` maps an email to a saved `SearchSpec`. |
| `core/distance.py` | Haversine over `data/plz_de.csv` (PLZ → lat/lon, no place names). |
| `services/fetch.py` | **The only network path to Kleinanzeigen**: `RequestGate` (delay + jitter, per-run request cap, stop after N bad responses), `fetch_page()` (kill switch, dry run, cache; search pages expire after `SEARCH_CACHE_TTL_S`). |
| `services/search_url.py` | `SearchSpec` → `/s-suchanfrage.html?keywords=…&locationStr=<plz>&radius=…&minPrice=…&maxPrice=…` |
| `services/llm.py` | OpenAI-compatible transport (Gemini by default) + `complete_validated()` (pydantic validation, one retry). |
| `services/gmail.py` | Read-only Gmail client (OAuth installed-app flow) and MIME helpers. |
| `storage/db.py` / `core/models.py` / `config.py` | SQLite schema + queries / pydantic models / `.env` settings. |

## 3. Main flows

Sequence diagrams: [diagrams/flows.md](diagrams/flows.md). Component view: [diagrams/overview.md](diagrams/overview.md).

**Interactive search.** Text message → `next_turn()` (LLM) asks ≤3 questions
→ spec shown with buttons → *Search* → `search_and_notify()` background task:
under the lock, in a worker thread, `run_search_once()` stores the spec,
fetches one search page, stores **every** card, marks cards outside the
price range or radius `out_of_range` (no detail fetch), fetches remaining
detail pages, evaluates each fetched listing → back on the event loop,
`send_pending()` sends recommendations for that spec → summary message with
the Kleinanzeigen link to save as a saved search.

**Gmail monitoring.** `JobQueue` every `GMAIL_POLL_INTERVAL_S` →
`poll_gmail_once()`: list messages for `GMAIL_QUERY`, skip IDs already in
`gmail_messages`, extract listing links, match the email to a saved spec by
its query words (else evaluate without a spec), `process_email()` → same
fetch/evaluate path → `send_pending()` to `TELEGRAM_NOTIFY_CHAT_ID`
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
3. **LLM judgment**: `product_assessment` (verdict + requirements match,
   regardless of price) and `deal_assessment` (verdict, regardless of how
   good the product is) as separate objects, highlights/concerns each with
   evidence (verbatim quote or `fact:<name>`), unknowns, score 0–100,
   confidence. `verify_evidence()` checks every claim in Python; unverified
   claims are stored but never shown. No market-value claims (there's no
   market data).

`recommended = score ≥ RECOMMENDATION_THRESHOLD and no hard fail and requirements_match ≠ "mismatch"`.

## 4. Data model (`storage/db.py`)

| Table | Key | Purpose |
|---|---|---|
| `search_specs` | `id` | Confirmed specs (JSON) per chat; `active` flag used for Gmail matching. |
| `listings` | `listing_id` (Kleinanzeigen ad id) | Every listing ever seen, raw scraped fields, `image_urls`, `attributes` JSON, `source` (`search`/`gmail`, first seen wins), `status`. |
| `evaluations` | unique `(listing_id, IFNULL(search_spec_id,0))` | One evaluation per listing per spec (or per "no spec"): score, recommended, product/deal verdicts, `input_json` (facts + checks + exact prompt), `judgment_json`, raw response, model, prompt version. |
| `notifications` | `(listing_id, chat_id)` | **Dedup key**: a listing is sent to a chat at most once, even if several specs recommend it. |
| `gmail_messages` | `message_id` | Emails fully processed (listing ids, matched spec, error). |

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

## 7. Testing and local setup

- `make check` = ruff + mypy + pytest. Tests never touch the network: fetch,
  LLM, Gmail and Telegram are injected fakes (`fetcher=`, `provider=`,
  `client=`, fake bot). SQLite is real (in-memory / `tmp_path`).
- Offline end-to-end tests cover both flows: `tests/test_app.py`
  (confirmed spec → real fetch layer in DRY_RUN over fixture HTML → DB →
  evaluation → fake Telegram; alert email → … → Telegram exactly once).
- Evaluator replay: every evaluation stores the exact prompt, raw response
  and `EVALUATOR_PROMPT_VERSION`; `make export` writes a CSV with an empty
  `my_label` column for building a hand-labelled set.
- How to run it for real: [README.md](../README.md#how-to-test-locally).
