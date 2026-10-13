# Decisions

Short ADR-style entries. Newest context: V2 redesign, 2026-09-25.

**Decision:** The SQLite database is the source of truth (stdlib `sqlite3`, no ORM).
**Reason:** Interactive searches and Gmail alerts must converge into one listing → evaluation → notification pipeline; dedup and retries need durable state. One user, five small tables — an ORM would be pure overhead.

**Decision:** Store every discovered listing; cards clearly outside the price range or radius are stored as `out_of_range` without a detail fetch.
**Reason:** "Nothing silently dropped" and "don't waste the request budget" both hold. Kleinanzeigen appends far-away results after the in-radius ones (seen live), so the radius check matters.

**Decision:** Retire the hand-curated TV catalog and the fixed-weight scorer.
**Reason:** The bot must work for any product. A per-category catalog doesn't scale to that; an LLM with the listing text does.

**Decision:** Evaluation is three separate layers: regex facts → deterministic checks → LLM judgment.
**Reason:** Keeps the LLM grounded: it never extracts prices or does distance/budget math, and each layer is stored on its own so mistakes can be traced to the layer that made them.

**Decision:** Every highlight/concern needs evidence (verbatim quote or `fact:<name>`), verified in Python; unverified claims are stored but never shown.
**Reason:** Cheap, deterministic hallucination filter. Citing an unknown fact doesn't count as evidence.

**Decision:** No "below market value" claims.
**Reason:** There is no market data. Value is judged against the buyer's budget and what the listing itself says.

**Decision:** `product_assessment` and `deal_assessment` are separate required objects with their own verdicts.
**Reason:** A great product at a bad price and a mediocre product at a great price are different answers; one blended opinion hides that.

**Decision:** Recommended = score ≥ `RECOMMENDATION_THRESHOLD` (default 70) and no hard fail and requirements match ≠ mismatch.
**Reason:** The threshold stays configurable, and deterministic hard fails (over budget, outside radius) can't be overruled by the LLM.

**Decision:** Keep Gemini's free tier via the OpenAI-compatible transport; strict `json_schema` output with a `json_object` fallback (`LLM_RESPONSE_FORMAT`); pydantic validation with one retry.
**Reason:** User's cost decision. Strict-schema support differs between endpoints, so the fallback is one setting away, and validation doesn't trust either mode.

**Decision:** Text-only evaluation; photos are stored and sent to Telegram but not analysed.
**Reason:** User's decision — cheaper and simpler for the POC.

**Decision:** Structured listing fields (`Zustand`, `Art`, shipping) are facts; condition prefers the `Zustand` field over text phrases.
**Reason:** Real detail pages carry them reliably (seen live); a regex over free text missed "in sehr gutem Zustand".

**Decision:** Notifications are read from the DB and recorded only after Telegram accepted them; `(listing_id, chat_id)` is the dedup key.
**Reason:** A failed send is retried next time, a successful one is never repeated, regardless of which spec or path found the listing.

**Decision:** Gmail via OAuth 2.0 (installed-app flow), scope `gmail.readonly`; processed messages tracked in the DB, not with Gmail labels.
**Reason:** Proper Google authentication without storing a password; least privilege; the DB stays the only state store.

**Decision:** Kleinanzeigen saved searches are created manually; the bot only reads their alert emails and offers the search link to save.
**Reason:** Explicit requirement — no automation of the Kleinanzeigen account.

**Decision:** An alert email counts as processed only once all its listings were fetched and evaluated.
**Reason:** A poll cut short by the request budget or an LLM error resumes on the next poll instead of losing listings.

**Decision:** Gmail polling runs in the bot process (`JobQueue`); a single lock serialises all scraping; pipeline work runs in a worker thread.
**Reason:** One process to run; a search and a poll never hit Kleinanzeigen at the same time; the bot stays responsive.

**Decision:** Search via Kleinanzeigen's search-form endpoint (`/s-suchanfrage.html`), but price and distance are always re-checked client-side.
**Reason:** The site resolves the PLZ to its internal location id (verified live); server-side filtering is not fully trusted.

**Decision:** Conversation state lives in memory (`chat_data`).
**Reason:** Single user; losing a half-finished chat on restart is acceptable. Confirmed specs are persisted.

**Decision:** Every evaluation stores the exact prompt, raw response, model and prompt version; `make export` produces a CSV with an empty `my_label` column.
**Reason:** Lets the evaluator be measured later against a small hand-labelled set of real listings without building an eval harness now.

**Decision:** All code in one `offer_finder/` package (role subfolders `bot/ core/ services/ storage/`), run from the repo root with `python -m offer_finder.…` / `make`; tool caches in `.cache/`; personal notes in gitignored `local/`.
**Reason:** User's choice (2026-09-25) for a clean root. Absolute `offer_finder.` imports avoid generic top-level names like `core`/`bot`. Still no `src/` and no install step.

**Decision:** A running search shows one Telegram status message that is edited in place (steps with counts), throttled to one edit every 3 s and only when the text changed.
**Reason:** The user wants to see where a request is, not a black box. Editing one message keeps the chat clean and stays under Telegram's edit rate limits. Failed edits are ignored so progress reporting can't break a search.

**Decision:** Logs go to the terminal and a rotating file (`data/logs/offer_finder.log`), with one INFO line per step tagged by request (`chat`, `search`, `email`). Per-request detail is at DEBUG.
**Reason:** Both the user and future Claude sessions can follow what happened after the fact without it getting noisy. `httpx` stays at WARNING because it logs the bot token.

**Decision:** Notifications are short (Telegram HTML): score, the seller's title shortened and the price; town and distance; up to 3 verified matches and concerns (worst first) as short phrases; unknowns; one verdict line naming both the deal and the product verdict; a "View listing" link. The long summaries stay in the DB.
**Reason:** The user reads them on a phone. The title stays the seller's own words, because an AI-rewritten title could invent a model name. Deal and product stay separate in the verdict line. Everything is HTML-escaped, and if Telegram still rejects the markup the message goes out as plain text.

---

Context: several places, "best of category", list-first results, 2026-10-06.

**Decision:** A spec holds several places with one shared radius (`DEFAULT_RADIUS_KM`, 20, when none is given); one results page per place, cards merged by listing id, distance measured to the nearest place.
**Reason:** The user searches e.g. Wiesbaden, Mainz and Mannheim at once. One radius keeps the spec and the confirm card simple. The old "most central place + big radius" covered towns in between nobody asked for.

**Decision:** "Best" comes from one broad category search plus smarter scoring, not from searching shortlisted model names. The spec carries a `quality_guide` (top tier / mid / avoid), shown on the confirm card and handed to every evaluation.
**Reason:** User's choice. One query keeps the request budget and finds listings with vague titles; a shared, visible, editable guide makes all scores of a run use the same yardstick instead of each evaluation improvising one.

**Decision:** `product_assessment.tier` (top / upper_mid / mid / budget / unknown); Python caps the score at 70 for mid and 55 for budget.
**Reason:** Live runs scored generic budget TVs 85–90 because they matched the query and the budget. The caps make "the buyer wants the best" deterministic, like the hard fails; the raw response keeps the model's own score for replay.

**Decision:** Results are one ranked text list with numbered buttons (`offer:<evaluation id>`); a tap sends that offer's card with photos. Search lists show all picks of the run (earlier ones marked "seen before"); Gmail lists only new picks.
**Reason:** The user wants to read the picks and the AI's reasons in one place and open only the ones they care about, instead of a stream of photo albums. Carrying the evaluation id makes buttons survive restarts without extra state.

---

Context: friends can use the bot, 2026-10-13.

**Decision:** Multi-user with admin approval. `TELEGRAM_ALLOWLIST` lists the admins; anyone else who writes is stored as `pending` in a `users` table and the admins get one message with Approve/Block buttons.
**Reason:** The bot is public on Telegram and every search spends the owner's Gemini quota and Kleinanzeigen request budget, so access stays invite-only. Approval needs no restart and no `.env` edit.

**Decision:** Authorization is checked inside every handler (`access.authorize()`), reading the user row from SQLite on each update; button data is matched with strict patterns and ownership is checked server-side (`db.evaluation_visible_to()`).
**Reason:** Telegram filters only see the update, not who owns an evaluation id. Forged or stale callback data must get nowhere, and a block takes effect immediately.

**Decision:** Gmail monitoring stays admin-only: alert emails are matched only to admins' searches, evaluated under those or under no search, and listed only to the admins.
**Reason:** The inbox and the Kleinanzeigen saved searches belong to the owner. Matching by query words could otherwise attach the owner's alerts to a friend's search; this also closed a leak where alert lists contained friends' picks.

**Decision:** A search = one confirmed press of *Search*. `search_runs` + `db.reserve_search_run()` allow one queued/running search per user and `USER_DAILY_SEARCHES` (5) per rolling 24 hours for non-admins, checked and recorded in one `BEGIN IMMEDIATE` transaction. Failed runs count; runs cut off by a restart are marked `interrupted`.
**Reason:** Protects the shared free-tier quota and the server's IP from one user, and a double tap can't start two runs. Counting attempts (not successes) means retries can't get around the limit.

**Decision:** Per-user home postcode and radius (`/home`, `/radius`) fall back to `HOME_PLZ` / `DEFAULT_RADIUS_KM`.
**Reason:** Friends live elsewhere; the `.env` values stay the defaults so nothing changes for the owner.
