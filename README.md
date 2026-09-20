# offer-finder

A personal Telegram bot that finds underpriced TV listings on Kleinanzeigen
(German classifieds). You message it in plain language ("find me good TVs
under €700 around 55128"); it searches Kleinanzeigen, matches listings
against a small hand-curated list of TV models you actually trust, checks
the asking price against your own reference price for that model, runs a
few obvious scam checks, and replies with the best few — each with a short
reason and a direct link.

**The one rule that shapes everything:** the LLM only ever does *language* —
turning your message into a structured search, and (nowhere in this version,
but permitted later) tidying up messy listing text. It never decides
whether something is a good deal and never invents a price. Every score is
plain, deterministic Python you can read and re-run.

This is a V1/POC: one category (TVs), one person, run from a terminal or a
small VPS. See the bottom of this file for what's deliberately *not* built
yet.

## How it works

```
Telegram message → LLM query parsing → Kleinanzeigen search →
HTML parsing → catalog matching → price/value calculation →
scam checks → scoring → Telegram response
```

### 1. LLM query parsing — `services/llm.py`

- **What it does:** Turns your free-text message into a structured
  `StructuredQuery` (max price, minimum screen size, preferred brands, home
  PLZ, max distance).
- **Why it exists:** Because you write "under 700 around 55128, 65 inch or
  bigger" and the rest of the pipeline needs actual numbers to filter and
  score on. This is the *only* thing the LLM is used for in V1 — it never
  touches price or quality judgments.
- **Data in / out:** in — the raw message string (`"Find me good TVs under
  €700 around 55128"`); out — a `StructuredQuery` (`models.py`).
- **Connects to:** the parsed query's `home_plz` and price bounds flow
  straight into the pipeline (`core/pipeline.py`), and its `max_price_eur`
  is used as a cheap pre-filter before any detail page is even fetched.

### 2. Kleinanzeigen search — `services/fetch.py` + `core/pipeline.py`

- **What it does:** Fetches one broad Kleinanzeigen TV search page (not one
  search per catalog model — see "why the search stays broad" below), through
  a single rate-limited choke point (`RequestGate`) with jittered delays, a
  hard per-run request cap, and a local HTML cache so re-runs never re-hit
  the site.
- **Why it exists:** Kleinanzeigen has bot protection; hammering it risks
  getting the account/IP throttled or blocked. Every outbound request goes
  through `fetch_page()` — there's no other code path that can issue one.
- **Data in / out:** in — a URL and a `page_type` ("search" or "detail");
  out — raw HTML (`str`), served from cache when possible.
- **Connects to:** the returned HTML goes straight into HTML parsing (§3).

**Why the search stays broad:** German sellers rarely write exact model
codes ("LG 65 Zoll" far more often than "OLED65C27LA"). Searching once per
catalog model would mean dozens of requests — worse for blocking risk than
one broad search followed by *local* matching against the catalog (§4,
no network, no LLM). Detail pages are only fetched for listings that
survive the cheap filters.

### 3. HTML parsing — `core/parse.py`

- **What it does:** Regex-extracts structured fields from listing HTML:
  price, PLZ, screen size, condition text, posted date — plus, for search
  result pages, the list of listing summaries (id, url, title, price,
  location, posted date).
- **Why it exists:** Regex is more accurate than an LLM for price/PLZ/size —
  and every field it produces carries a `source` ("regex"/"llm"/"none") and
  a `confidence`, so a field the regex couldn't find is explicit (`None`,
  confidence `0.0`) rather than silently defaulted.
- **Data in / out:** in — raw HTML; out — `ListingSummary` list (search
  pages) or a `DetailPageText` (title/description/location/price/posted-date
  text) plus `FieldValue`-wrapped fields for a single listing.
- **Connects to:** the extracted title+description text feeds catalog
  matching (§4); the `FieldValue`-wrapped fields feed scoring (§6).

### 4. Catalog matching — `core/catalog.py`

- **What it does:** Checks a listing's title (then description) for a
  substring match against the `aliases` of any model in `catalog_tv.yaml`.
  First confident hit wins. No match → the listing is dropped and skipped —
  a correct, expected outcome, not a failure.
- **Why it exists:** This is the core idea of the whole project: instead of
  estimating a fair price for *every* listing, only listings for models
  *you've already decided are worth buying* get valued and scored at all.
  Most broad-search results won't match anything, and that's fine — the
  catalog only needs the models worth watching for.
- **Data in / out:** in — listing title + description + the loaded catalog
  dict; out — a `CatalogMatch` (model name, your reference price, a quality
  note) or `None`.
- **Connects to:** a `CatalogMatch` is required to proceed to valuation and
  scoring (§5–6); `None` ends that listing's journey right here.

### 5. Price / value calculation — inside `core/scorer.py`

- **What it does:** Compares the listing's asking price against the
  catalog's `fair_used_price_eur` for the matched model.
- **Why it exists:** `fair_used_price_eur` is **your own hand-curated
  reference judgment** — not an estimate, not something computed, and
  never something the LLM is asked for. It's deliberately named
  `fair_*_used_price`, not `market_price`, to keep it honest about what it
  is: a personal opinion you wrote into a YAML file and can edit any time.
- **Data in / out:** in — the listing's extracted price + the matched
  `CatalogMatch`; out — a `value_gap_score` (0–100, how underpriced the
  listing is relative to your reference — capped so one wild outlier can't
  dominate the total score).
- **Connects to:** one of five terms summed into the final score (§7).

### 6. Scam checks — `core/scam.py`

- **What it does:** Scans the listing description for a small, fixed list
  of obvious red flags: shipping-only ("nur Versand"), "Nachnahme",
  "PayPal Freunde und Familie", and defect keywords ("Displaybruch",
  "Bastler", "defekt", "Ersatzteil", "Riss"). Each triggers a fixed penalty
  point value; they sum and cap at 100.
- **Why it exists:** The biggest price gaps are disproportionately scams or
  broken items. This is intentionally simple — a short, readable keyword
  list, not a fraud-detection system — because the loud, obvious signals
  are what actually shows up in practice.
- **Data in / out:** in — the listing description; out — a penalty (float)
  and the list of which specific signals fired (for the reply text).
- **Connects to:** the penalty is subtracted directly in the final score
  (§7) — it can single-handedly drag an otherwise "amazing deal" down to
  near zero.

### 7. Scoring — `core/scorer.py`

- **What it does:** Combines value gap, distance from home, listing
  freshness, a missing-data penalty, and the scam penalty into one
  `total_score` (0–100), plus a short plain-English `explanation`.
- **Why it exists:** This is the one deterministic, pure function the whole
  system's trustworthiness rests on — no I/O, no LLM, no network, so it's
  fully unit-tested with golden cases and safe to re-run against stored
  listings after editing a weight.
- **Data in / out:** in — `ExtractedListing`, `CatalogMatch`, your home PLZ
  and max distance; out — a `ScoreBreakdown`.
- **Connects to:** `core/pipeline.py` collects one `ScoredListing` per
  matched listing and sorts them by `total_score` before replying.
- **The formula** (see `core/scorer.py` for the exact constants):
  `total = 0.60·value_gap + 0.25·distance + 0.15·freshness − missing_data_penalty − scam_penalty`,
  clipped to `[0, 100]`.

### 8. Telegram response — `bot/app.py`

- **What it does:** Checks the sender's Telegram user ID against
  `TELEGRAM_ALLOWLIST` (silently ignoring anyone else), runs the pipeline,
  and formats the top 5 `ScoredListing`s into a reply with model name,
  price, score, the one-line reason, and a direct link.
- **Why it exists:** This is a personal bot — the allowlist is the only
  access control, and it fails closed (unknown senders get no response at
  all, not even an error, so as not to confirm the bot exists to strangers).
- **Data in / out:** in — a Telegram `Update`; out — a Telegram reply
  message.
- **Connects to:** nothing further — this is the end of the pipeline. The
  same `core.pipeline.run_search()` function is called identically from
  `cli.py run-search "..."` (no Telegram needed) and from the bot handler —
  that's the seam a future scheduled/polling mode would also call into.

## Worked example

Message: **"Find me good TVs under €700 around 55128"**

1. **Query parsing** → `StructuredQuery(max_price_eur=700, home_plz="55128", ...)`
2. **Search** → one fetch of the Kleinanzeigen TV search page, cached after
   the first request.
3. **HTML parsing** finds, among many others, this search result:
   `ListingSummary(title="Grundig 43 GUS Fernseher– 43 Zoll 4K UHD LED Smart TV", price_raw="99 €", location_text="57074 Siegen", posted_at_raw="Heute, 17:56")`
   — it passes the cheap price filter (99 ≤ 700), so its detail page gets
   fetched. Regex extraction from that detail page gives:
   `price_eur=99 (regex, 0.95)`, `plz="57074" (regex, 0.9)`,
   `screen_size_inch=43.0 (regex, 0.9)`, `condition_text=None (none, 0.0)`
   — this real listing's description is all technical specs and never
   states a condition, so that field is honestly `None`, not guessed.
4. **Catalog matching**: the title contains the alias `"Grundig 43 GUS"` →
   `CatalogMatch(model_name="Grundig 43 GUS 8768", fair_used_price_eur=180.0, quality="budget 4K LED, fine as a second TV")`.
5. **Value calculation**: €99 vs. a €180 personal reference →
   `value_gap_score = 45.0`.
6. **Scam checks**: no red-flag keywords found → `scam_penalty = 0.0`.
7. **Scoring**: 57074 (Siegen) is ~100 km from 55128 — beyond the default
   50 km radius, so `distance_score = 0.0`; posted today →
   `freshness_score = 100.0`; the missing `condition_text` costs a small
   `missing_data_penalty = 3.0`. Total:
   `0.60·45.0 + 0.25·0.0 + 0.15·100.0 − 3.0 − 0.0 = 39.0`.
8. **Reply**: *"Grundig 43 GUS 8768 — €99 (score 39/100) — €99 vs. a
   personal reference of €180 for Grundig 43 GUS 8768 — [link]"*

These are real numbers from the actual code (`tests/test_pipeline_e2e.py`
and `cli.py run-fixtures` run against saved fixtures), not made up for
illustration — you can reproduce this exact walk-through by running
`uv run python cli.py run-fixtures`.

## Running it

```bash
uv sync
cp .env.example .env   # fill in TELEGRAM_BOT_TOKEN, TELEGRAM_ALLOWLIST, LLM_API_KEY
uv run python cli.py run-fixtures     # offline: prove the core pipeline works, zero network/LLM
uv run python cli.py run-live         # tiny live fetch against real Kleinanzeigen (respects DRY_RUN/caps)
uv run python cli.py run-search "..." # full pipeline incl. LLM query parsing, no Telegram
uv run python -m bot.app              # the actual Telegram bot (polling)
make check                            # lint + typecheck + full offline test suite
```

Edit `catalog_tv.yaml` to add models you'd actually consider buying and your
own sense of a fair used price for each — that file is the main lever on
how many deals the bot ever surfaces, more so than any scoring weight.

## What's deliberately not built in V1

- Only TVs — no category system. Adding a second category later means
  building the same kind of generic pieces (a schema, a catalog, weights) but
  that's future work, not designed in detail here.
- Catalog matching is plain substring matching — no LLM disambiguation for
  ambiguous titles. Sparse titles ("TV 65 Zoll, Top Zustand") that don't
  clearly match a catalog alias get skipped rather than guessed at.
- Rate limiting is simple (a per-run cap + jittered delays + "stop after N
  failures") — not a persisted, cross-restart budget with exponential
  backoff.
- No local "median of my own scrape history" valuation fallback — a
  listing either matches the catalog or gets skipped.
- No labelled evaluation harness or fake-clock rate-limiter test suite yet —
  the test suite covers regex edge cases, catalog matching, the scorer's
  golden cases, blocking detection, and one full offline end-to-end run.
