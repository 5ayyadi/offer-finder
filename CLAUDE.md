# CLAUDE.md — offer-finder

Context for future Claude sessions. **Read [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
first**: it maps every module, both flows and the data model, so you don't
have to explore the repo. Reasons behind choices: [docs/DECISIONS.md](docs/DECISIONS.md).

## What this is

Personal POC, one user: a Telegram bot that turns a conversation into a
confirmed Kleinanzeigen search for any product, stores every listing in
SQLite, has an LLM (Gemini free tier, OpenAI-compatible endpoint) judge each
one, and sends one ranked list of the picks with buttons that open each offer
(photos, reasons, link). It searches around several places at once and
assumes the buyer wants the best product the budget can buy (quality guide
in the spec, product tier with Python score caps). Kleinanzeigen
saved-search alert emails (read-only Gmail OAuth) feed the same pipeline.
V1 (TV-only catalog + fixed-weight scorer) was replaced on 2026-09-25; it's
commit `62b2bb3` in git history.

## Rules — do not relitigate these silently

1. **The DB is the source of truth.** Every discovered listing gets a row
   (`out_of_range` if the card is clearly outside price/radius). "Already
   evaluated", "already notified" and "email processed" live only in SQLite.
   No JSON side files, no in-memory sets.
2. **The LLM judges; Python computes.** Evaluation = facts (`extract_facts`)
   → deterministic checks (`compute_checks`) → LLM judgment. The LLM never
   extracts the asking price, never does distance/budget math, and its hard
   fails can't be overruled. The spec-building LLM output is always
   confirmed by the user before searching.
3. **Grounding.** Unknown stays `null`, never guessed. Every highlight and
   concern needs evidence (verbatim quote or `fact:<name>`), checked by
   `verify_evidence()`; unverified claims are never shown. No market-value
   claims: there is no market data.
4. **Product quality and deal quality stay separate** (`product_assessment`,
   `deal_assessment`). Don't collapse them into one verdict.
5. **One network choke point for Kleinanzeigen**: `offer_finder.services.fetch.fetch_page()`
   gated by `RequestGate`. Respect `DRY_RUN` and `KILL_SWITCH`. In the bot,
   all scraping runs under one lock, in a worker thread.
6. **Notifications are recorded only after Telegram accepted them**;
   `(listing_id, chat_id)` is the dedup key (Gmail lists only new picks;
   search lists mark earlier ones "seen before").
7. **Never automate Kleinanzeigen saved searches** or anything else on the
   user's Kleinanzeigen account. Gmail access stays read-only.
8. **Changing the evaluator prompt or schema? Bump `EVALUATOR_PROMPT_VERSION`**
   so stored evaluations stay comparable against a labelled set.

## Repo layout (one package, no `src/`, no install step — explicit user choice)

```
offer_finder/            the code; run from the repo root with `python -m offer_finder.…` (no install step)
  config.py runner.py cli.py   settings / wiring shared by bot + CLI / dev CLI
  core/      models + logic: parse, redflags, distance, search_spec, checks, evaluator, email_parse, pipeline
  services/  external systems: fetch (Kleinanzeigen), search_url, llm (OpenAI-compatible), gmail
  storage/   db.py (SQLite)
  bot/       Telegram: app (process, background tasks, Gmail job), conversation, status (live progress message), notify (pick list + offer card), offers (offer buttons); `python -m offer_finder.bot`
tests/       pytest, mirrors the package; helpers/ = shared fakes + factories;
             fixtures/ = real, PII-redacted Kleinanzeigen HTML + a hand-built alert email
data/        plz_de.csv (committed); cache/, logs/, offer_finder.db, gmail_*.json (gitignored)
docs/        ARCHITECTURE.md, DECISIONS.md, diagrams/ (Mermaid)
local/       gitignored: the user's personal notes, guides, handoffs — put session handoffs and how-tos here
.cache/      gitignored: all tool caches (pytest, ruff, mypy, bytecode via make)
```

Use the `make` targets (`make check`, `make bot`, `make search Q="…"`, `make gmail-poll`, …).
Imports are absolute from the package: `from offer_finder.core.models import …`,
`from offer_finder.storage import db`.

## Fixtures

`tests/fixtures/tv_listing_normal.html`, `tv_listing_scam.html` (same page, text
edited to inject red flags) and `tv_search_results.html` are real scraped
pages with the seller's name and `userId`/`sellerId`/`posterid` scrubbed. If
you add freshly scraped pages, **redact those again before committing**.
`e2e_search_results.html` and `gmail_alert_email.html` are hand-built. The
email fixture is a guess at the real alert format and should be replaced by
a redacted real alert once the user provides one.

## Testing conventions

- TDD: a failing test first, then the code. Keep doing this.
- `pytest` never touches the network. External systems are injected:
  `fetcher=` (Kleinanzeigen), `provider=` (LLM), `client=` (Gmail), a fake
  bot object (Telegram). SQLite is real (`:memory:` / `tmp_path`).
- `tests/` mirrors the package (`core/ services/ storage/ bot/`, plus
  `test_runner.py` / `test_cli.py` / `test_config.py` at the top). Shared
  helpers live in `tests/helpers/`: `factories.py` (`make_*`, `load_fixture`),
  `fakes.py` (fake fetcher, LLMs, Telegram bot, Gmail), `offline.py` (real
  `Settings` + DRY_RUN cache filled with fixtures). `conftest.py` only holds
  the in-memory `conn` fixture. Test files don't import from each other.
- Offline end-to-end tests of both flows are in `tests/bot/test_app.py`;
  `tests/test_runner.py` exercises the real fetch layer via DRY_RUN over a
  temporary cache filled with fixtures.
- `make check` = ruff + mypy + pytest; keep it green.

## Logs

To see what the bot did, **read `data/logs/offer_finder.log` first** (gitignored,
rotating; the terminal shows the same lines). Lines are tagged `chat <id>`,
`search <spec id>`, `email <id>` / `gmail`, so one request can be followed from
the message to the notifications. New steps get one INFO line; problems a
WARNING. Per-request detail goes at DEBUG (`LOG_LEVEL=DEBUG`). Keep it at that level.

## Secrets

`.env`, `data/gmail_credentials.json`, `data/gmail_token.json` and
`data/offer_finder.db` are gitignored. Only `.env.example` (placeholders) is
committed. `setup_logging()` keeps the `httpx` and `httpcore` loggers at WARNING because
their INFO lines contain the bot token.

## Deliberately not built

Vision/photo analysis in evaluation, multi-page search results, a
labelled-eval runner (the data for it is already stored), commands to list
or deactivate saved specs, persisted conversation state, multi-user support.
Don't add these preemptively. The user explicitly capped V2's scope.
