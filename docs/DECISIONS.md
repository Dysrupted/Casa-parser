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

## M2 — LLM extraction, geo, FX

15. **Alembic migrations from M2 on.** `init_db` = `alembic upgrade head`. 0001 = M1 schema,
    0002 = M2 delta. New schema changes → `alembic revision --autogenerate` (render_as_batch
    for SQLite).

16. **Two extractors, both stored.** `rules` (regex, French) always re-runs (cheap, deterministic);
    `llm:<model>` runs only when `description_hash` changes. `propertyfeature` keeps one row per
    (property, field, extractor); `resolved_features()` picks the highest-confidence non-null value
    ≥ `llm.min_confidence`. *Why:* comparing the extractors is free QA, and turning the LLM off
    degrades gracefully instead of failing.

17. **Anti-hallucination = verified spans.** Each LLM value must come with a `source_span` that is
    literally in the description (case/accent/whitespace-insensitive). Otherwise the value is
    nulled and counted in `llm_rejected_fields`. Null = "not stated", never a guess.

18. **LLM call:** Claude via the official SDK, `beta.messages.parse` with a Pydantic schema
    (structured outputs), `effort: low` (simple extraction; tune in `enrich.yaml`), server-side
    `fallbacks: "default"` in case a request is declined. Error handling: auth/permission →
    provider disabled for the run (rules-only + notify); rate limit/5xx/refusal/bad output →
    skip this listing and retry next run. Cost guard: `max_extractions_per_run`.

19. **Geo precision ladder:** page coordinates > Nominatim address > zone centroid, recorded in
    `property.geo_precision`. Results outside the Casablanca bbox are rejected. Zone centroids
    and the Corniche polyline are **approximate, hand-placed**: refine on a map.
    `dist_corniche_m` = point-to-polyline distance (local equirectangular projection).

20. **robots.txt is not checked for JSON APIs** (`kind="api"`: Nominatim, FX). Those are governed
    by their usage policies; we still identify ourselves, wait ≥3 s and cache (30 d).

21. **FX:** EUR/MAD from open.er-api.com (ECB doesn't publish MAD), stored once per day in `fxrate`.
    A sanity band rejects broken values; past `max_staleness_days` EUR is reported as null rather
    than stale. `manual_rate` overrides it, e.g. with your bank's quote. EUR is computed on read
    (`to_eur`), never stored on listings.

## M3 — Purchase simulation

22. **No rates in code or in the shipped config.** `config/financing.yaml` ships with every rate
    `null`. `simulate()` refuses (`ok=false` + `missing_params`) rather than guess, and a test
    (`test_shipped_config_has_no_invented_rates`) guards that. Fill it from the workbook.

23. **Generic cost lines instead of hardcoded Moroccan fee formulas.** Each line is
    `clamp(rate × base + fixed, min, max) × (1 + VAT)` with `base ∈ {price, loan}` and
    `applies ∈ {always, agency_listing, never}`. This should express the workbook's lines. If a
    line doesn't fit (e.g. tiered notary scale), extend `CostLine` rather than special-casing.

24. **Decimal math, Excel ROUND (half away from zero) on outputs only.** That is needed to match the
    workbook "to the dirham". PMT follows Excel's formula. Insurance supports `initial_capital`
    (constant) and `outstanding_capital` (declining).

25. **Financed acquisition costs** (optional) are solved as a fixed point, because loan-based costs
    (mortgage registration) depend on the loan, which then includes them.

26. **Output definitions**: `total_cash_needed` = down payment + acquisition costs (unless financed)
    + bank fees; `total_cost_of_credit` = interest + insurance + bank fees; `all_in_price` =
    price + acquisition costs; `all_in_cost` = all-in price + cost of credit. EUR band = all-in at
    EUR/MAD × (1 ± sensitivity). **Check these against the workbook's definitions.**

27. **Agency fee applies** when resolved feature `agency_or_private == agency`, falling back to the
    listing having an agency name.

28. **Reference cases** live in `tests/fixtures/simulation/reference_cases.yaml`. The test is skipped
    until they are filled, then it must match every listed output exactly.

## M4 — Zone index & scoring

29. **The index is an asking-price/m² index**, with one price per *property* (its cheapest active
    listing, so post-M6 duplicates don't double count), over a 90-day window, with IQR outliers removed.
    Quantiles use the inclusive method (= Excel QUARTILE.INC). A zone is `reliable` once it has ≥ `min_points`
    (default 5; the PRD target is 30). An unreliable zone gives a *neutral* price factor, not a zero.

30. **Score = Σ weightᵢ·factorᵢ − data-quality penalty**, weights normalised to 100, all in
    `config/scoring.yaml`. Price-vs-zone is piecewise-linear on (p25 → 1, median → 0.5, p75 → 0).
    Every factor stores value, points and a human reason. `top_reasons()` feeds alerts.

31. **Unknown ≠ absent.** A factor that can't be computed takes `missing_factor_value` (0.5), is
    marked `value: null` and explained. Missing data is charged once, via the penalty.
    Exception: in `features`, an unconfirmed nice-to-have counts 0, because a "nice to have" is only
    worth points when confirmed.

32. **Profile fit is separate from the score.** `in_profile=false` for surface/price/zone outside
    the profile or a must-have explicitly false. Unverified must-haves are noted, not excluded.
    Alerts (M5) only consider in-profile listings.

33. **Market signals**: cumulative price drop over the lookback (peak snapshot vs current) and days
    on market (negotiation room). Relisted > 3× → flag "possibly stale/fake" (PRD §9).

34. **Pipeline stages** are now a list (`enrich`, `analyze`) run after crawling, each fail-soft
    and committed separately.
