# Decisions log

Non-obvious choices, newest last. Format: decision — why — consequence.

## M1 — Ingestion, DB, snapshots

1. **Ingest scope = target zones only.** Cards whose location doesn't resolve to a zone in
   `search_profile.yaml` are dropped (counted in `stats.cards` vs `in_zone`).
   *Why:* removal detection needs *complete* crawls; city-wide crawls blow past `max_pages`.
   *Consequence:* the M4 zone index only covers target zones. Widen `zones` + `search_urls`
   if a city-wide index is wanted.

2. **Surface band (80–100 m²) is NOT applied at ingest.** Filtering happens in scoring/alerts.
   *Why:* the price/m² index needs more than 80–100 m² data points to reach ≥30/zone.

3. **One snapshot per listing per calendar day (Africa/Casablanca), upsert semantics.**
   Same-day reruns overwrite instead of appending. Events (`new`, `price_change`, `missing`,
   `removed`, `relisted`) are unique per (listing, day, type). *Why:* this *is* the
   idempotency contract of M1. *Consequence:* intra-day price flips collapse into one event
   (first old value, latest new value).

4. **Removal = absent from N complete crawls on distinct days** (`missing_runs_before_removed`,
   default 2). A crawl with any fetch/parse error or that hits `max_pages` is *incomplete* and
   never marks anything missing. *Why:* a 503 must not look like "everything sold".

5. **Added `listing_event` and `run` tables** (not in PRD §6). Alerts (M5) consume events
   rather than diffing snapshots; `run.stats` gives per-source observability.

6. **`property` is 1:1 with `listing` until M6.** Dedupe will re-point `listing.property_id`
   and keep the earliest `first_seen`.

7. **Selectors live in `config/sources.yaml` as fallback chains.** Site tweaks = YAML edit.

8. **robots.txt: 404 → allow all; any other failure → fail closed** (skip the site that run).

9. **Polite fetching**: randomized delay `uniform(min,max)` with a hard 3 s floor validated
   in config; delay only before network hits; disk cache (search 6 h, detail 7 d) so same-day
   reruns are near-free; no retry on 4xx except 429; non-browser User-Agent identifying the bot.

10. **Detail pages fetched only for new listings or every `refresh_detail_days`**; raw HTML kept
    at `data/raw/<source>/<external_id>/<date>.html` for re-parsing (M2 LLM pass).

11. **Mubawab fixtures are synthetic.** The build container's network policy blocked
    mubawab.ma, so fixtures mirror the known DOM and search URLs are marked VERIFY.
    Must be replaced via `radar record-fixture` before M1 is signed off on real data.

12. **selectolax lexbor backend** (Modest is removed in selectolax ≥1.0).

13. **Timestamps are tz-aware UTC in the DB**; the "day" is derived in Africa/Casablanca.

14. **No migrations framework yet** (`create_all`). Add Alembic when the first schema change
    lands on a DB holding real history (likely M2).
