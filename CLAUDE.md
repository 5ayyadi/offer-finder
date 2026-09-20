# CLAUDE.md — offer-finder

Context for future Claude sessions working on this repo. Read this before
making changes.

## What this is

A personal Telegram bot (V1/POC) that finds underpriced TV listings on
Kleinanzeigen (German classifieds). Message it in natural language, it
searches Kleinanzeigen, matches listings against a small hand-curated
catalog of TV models with a personal reference price, scores deterministically,
and replies with the best few. One user, no multi-tenancy, no web UI.

Full design rationale and worked example: **[README.md](README.md)**.
Original implementation plan (all phases, what was deferred to V2, and the
reasoning behind every scoping decision):
`/home/lee/.claude/plans/you-are-helping-me-swirling-wilkes.md` (local to
the machine this was built on — not part of this repo).

## Non-negotiable design rules — do not relitigate these silently

1. **The LLM only does language.** It parses natural-language requests into
   a `StructuredQuery` (`services/llm.py`). It must never decide whether a
   listing is a good deal and must never invent or estimate a price. All
   valuation, scoring, ranking, and filtering is deterministic Python
   (`core/scorer.py`, `core/catalog.py`).
2. **Regex first, LLM only for the residue** — and even that residue path
   isn't built yet in V1 (see below). Every extracted field carries a
   `source` ("regex"/"llm"/"none") and a `confidence` (`models.FieldValue`).
   A missing field must be explicit (`None`, confidence `0.0`), never
   silently defaulted.
3. **Catalog prices are personal opinions, not market data.** The field is
   named `fair_used_price_eur` deliberately — it's the catalog author's own
   hand-edited judgment. Nothing in the pipeline may compute, estimate, or
   ask an LLM for this value. If you add a fallback valuation path, keep
   this distinction explicit in the field name and in any new model class.
4. **The scorer (`core/scorer.py`) must stay pure.** No I/O, no network, no
   LLM calls, no imports of `services.*`. It should remain fully replayable:
   same inputs always produce the same `ScoreBreakdown`.
5. **Every outbound request to Kleinanzeigen goes through one choke point**
   — `services.fetch.fetch_page()`, gated by `RequestGate`. Don't add a
   second code path that calls `curl_cffi` (or anything else) directly
   against Kleinanzeigen. Respect `DRY_RUN` and `KILL_SWITCH`.
6. **Unmatched listings are dropped, not scored at zero.** No catalog match
   means "unidentified, skip" — a correct, expected outcome for most of a
   broad search's results, not a bug to work around.

## Repo layout (flat, no `src/` — this was an explicit user preference)

```
config.py, models.py, cli.py     — top-level, no package wrapper
core/        — pure domain logic: parse, catalog, scam, distance, scorer, pipeline
services/    — external integrations: llm.py (LLM provider), fetch.py (rate-limited HTTP + cache)
bot/         — Telegram adapter (app.py): allowlist + handlers, thin wiring only
catalog_tv.yaml — hand-curated TV models; edit this to change what the bot can ever surface
data/plz_de.csv — real German PLZ→lat/lon table (~8300 rows), committed
fixtures/    — real (PII-redacted) Kleinanzeigen HTML used by tests — see below
tests/       — pytest; `make check` runs ruff + mypy + pytest
```

Run everything from the repo root (`python cli.py ...`, `python -m bot.app`)
— no install step. Don't reintroduce a `src/` layout or packaging without
being asked; it was explicitly rejected during planning.

## Fixtures are real scraped HTML — handle with care

`fixtures/tv_listing_normal.html` and `fixtures/tv_listing_scam.html` are
derived from a real, live-fetched Kleinanzeigen listing page (the scam one
is the same page with the title/price/description text edited to inject
red-flag content for testing `core/scam.py`). Both had the real seller's
first name and numeric user ID scrubbed (replaced with `Verkäufer` /
`00000000`) before this repo went public — if you ever re-fetch or replace
these fixtures with fresh live HTML, **redact the seller's name and
`userId`/`sellerId`/`posterid` occurrences again before committing.**
`fixtures/tv_search_results.html` is a real broad search-results page (no
per-listing seller IDs are exposed on that page type). `fixtures/e2e_search_results.html`
is hand-built (not scraped) and has no real data in it at all.

## Testing conventions already established

- TDD throughout: a test existed and failed for the right reason before
  each piece of production code was written. Keep doing this for new work.
- Tests are right-sized for a V1 POC, not exhaustive — see the "What's
  deliberately not built in V1" section of README.md before adding a big
  new test-infrastructure category (fake-clock rate-limiter suite, eval
  harness, live LLM test marker group) — those are intentionally deferred,
  not forgotten.
- `pytest` with no flags must never make a network request. LLM and fetch
  calls are mocked/stubbed via dependency injection (`provider=`, `fetcher=`,
  `http_get=` parameters) — follow that pattern for new external calls.

## Secrets

`.env` is gitignored; only `.env.example` (placeholders only) is committed.
Never commit a real `TELEGRAM_BOT_TOKEN` or `LLM_API_KEY`. `data/cache/` is
also gitignored (it fills up with real fetched HTML at runtime).

## What's deliberately not built yet (V2)

A second category, a declarative category-registry system, LLM-based
ambiguous catalog-match disambiguation, persisted/backoff-hardened rate
limiting, a labelled evaluation harness, local-comps valuation fallback.
Don't build these preemptively — see README.md's closing section and the
plan file for the reasoning. If asked to generalize, read that reasoning
first rather than re-deriving the design from scratch.
