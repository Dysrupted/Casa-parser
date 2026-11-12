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

## M5 — Telegram alerts

35. **Raw Bot API over httpx, not python-telegram-bot.** We only need `sendMessage`/`sendPhoto`.
    One less dependency (and its async runtime), and it's testable with respx. Swap if we ever
    need inbound commands (v2: "/reject 123 ground floor" from the phone).

36. **Alerts are rows in `alert_log` with a unique `key`**: `new_high_score:<property>` (once
    ever), `price_drop:<property>:<day>`, `watch_removed:<property>:<day>`, `run_errors:<run>`.
    Reruns never double-send. The queue survives failures: retried up to `max_send_attempts`, then
    marked `failed`.

37. **"Realtime" = end of each run** (the system runs daily). Quiet hours *hold* alerts as
    `pending` and the first run after the window sends them. More than `max_alerts_per_run` → the
    overflow goes into one digest message. `mode: digest` sends a single summary per run.

38. **Triggers**: new in-profile listing with score ≥ threshold (also catches listings whose score
    rises later, e.g. after extraction); price drop ≥ 5 % on watched or high-score listings; watched
    listing removed. Listings noted `rejected` never alert.

39. **Fail-soft pings are aggregated**: everything the pipeline `notify`s during a run becomes one
    `run_errors` alert. Detail-page failures are now notified too. An FX feed outage only
    pings once no usable rate is left within `max_staleness_days`.

40. **`user_note` landed in M5** (PRD lists it under M7) because "watched listing removed" needs
    a watchlist. CLI: `radar watch <id|url>`, `radar note <id|url> --status rejected --reason ...`.
    It also has `actual_sale_price_mad` (PRD §9 mitigation).

41. **No credentials = dry run.** Alerts are queued and logged, nothing is sent, and they go out once
    the token is set.

## M6 — Avito, Sarouty, dedupe

42. **Generic HTML-card source.** The Mubawab parser became `HtmlCardSource`, fully driven by
    `sources.yaml` (selectors + `id_pattern`). Mubawab and Sarouty are 3-line subclasses, and a new
    server-rendered site (Yakeey…) is a YAML block plus a registry entry.

43. **Avito is read from the embedded `__NEXT_DATA__` JSON**, not the DOM. Paths are listed as
    alternatives in `sources.yaml` (`embedded_json`), so a schema drift is a config edit. Avito,
    Sarouty and the search URLs are all built from **synthetic fixtures**: verify on the live sites.

44. **Playwright is opt-in per source** (`fetcher: playwright`, `pip install .[playwright]`). It
    subclasses `PoliteFetcher`, so robots/delay/cache rules are identical. No source uses it by
    default: both Avito (embedded JSON) and Sarouty appear server-rendered.

45. **Dedupe = cheap candidates, then confirmation, precision first.** Candidates: same zone,
    surface ±3 m², price ±5 %. Confirmation: description bigram-Jaccard ≥ 0.6 (if both texts are
    ≥ 80 chars) **or** min photo pHash Hamming ≤ 8. If neither can be confirmed, nothing is merged.
    All thresholds are in `config/dedupe.yaml`.

46. **Photos are downloaded lazily**: only for candidate pairs that text didn't settle, at most 3
    per listing, cached (90 d), hashes stored on `listing.photo_hashes`. Every decision is stored
    in `dedupe_pair`, and "distinct" pairs are re-checked after 30 days, not daily.

47. **Merge semantics**: the survivor is the property with the earliest `first_seen`. Listings are
    re-pointed; features merge keeping the higher-confidence value; the note is kept (or
    appended); alert history is re-pointed so the flat isn't alerted twice; scores and simulations
    of the loser are dropped and recomputed. The loser row is deleted and audited in
    `property_merge`.

48. **Same-site reposts are dedupe merges too.** The "relisted > 3×" flag now counts relists plus
    extra same-site ads for one flat.

49. **Stage order: enrich → dedupe → analyze (index, simulation, scores) → alerts.** Simulation
    moved to `analyze` so it uses the merged property's cheapest listing.

50. **No automatic un-merge.** `radar dupes` lists merges for the manual 10-pair check and
    `radar merge A B` forces one. A wrong auto-merge would need an un-merge command (v2) or a
    threshold change plus a re-ingest.

## M7 — Dashboard, watchlist, Finance Cockpit API

51. **FastAPI + server-rendered Jinja + inline SVG, not React+Vite.** One process, no JS build, no
    CDN, fast on a phone (verified at iPhone width: no horizontal scroll). The JSON API
    (`/api/listings`, `/api/properties/{id}`, `/api/market[/{zone}]`, `/api/goal`) is complete, so a
    React front end can replace the templates later without backend changes. OpenAPI at `/api/docs`.

52. **`/api/goal` (PRD F9)**: one `goal` row, holding either a target property or a manual price. It
    returns `total_cash_needed` and `monthly_payment` (MAD/EUR) from the simulation, plus
    `simulation_ok`/`missing_params` so the Cockpit can tell "not configured" apart from zero.
    Set it with `PUT /api/goal`, the 🎯 button, or `radar set-goal`. Merges re-point it.

53. **Auth**: `RADAR_DASHBOARD_PASSWORD` (HTTP Basic, works natively on phones), `RADAR_API_TOKEN`
    (Bearer, `/api/*` only, for the Cockpit), `RADAR_SHARE_TOKEN` (read-only HTML via
    `?share=`, remembered in a cookie, never writes, no API). `radar serve` refuses a public bind
    with no credentials. Put it behind HTTPS (Caddy/nginx) on the VPS: Basic auth over plain
    HTTP leaks the password.

54. **Index stats are stored unrounded** (rounding is display-only). Rounding the median broke
    equality in degenerate zones (p25 = median = p75, e.g. one listing). That gave the only listing
    in a zone full "below p25" points. A degenerate distribution now scores the median listing as
    neutral. Regression tests cover both.

## First contact with the live sites (2026-11-12)

55. **Own robots.txt matcher** (`radar/robots.py`). `urllib.robotparser` ignores `*`/`$` in paths,
    so Mubawab's `Disallow: /*:` (its `:p:N` pagination) would have been crawled. `Crawl-delay` is
    now honoured too (Sarouty asks 10 s).

56. **Mubawab = page 1 per neighbourhood.** Real URLs are `/fr/sd/casablanca/<slug>/appartements-a-vendre`
    (ain-diab, anfa, racine, gauthier). Totals are 71–317 per zone, but page 2+ is forbidden by
    robots.txt, so we take the first ~30 listings per zone daily. The crawl is never "complete",
    so Mubawab gets no removal detection. That's the price of following robots.txt.

57. **Mubawab has a daily request quota.** After ~25 detail pages it answers 429 with a
    Retry-After of ~29 h (a fixed reset time). A Retry-After longer than `max_retry_after_s`
    (120 s) now pauses that host and persists the pause in `data/cache/backoff.json` across runs.
    Listings whose detail page wasn't fetched keep their search-card data.

58. **Avito**: neighbourhood slugs don't filter, so we crawl Casablanca apartments newest-first
    (8 pages ≈ 370 ads) and keep target zones locally. `immoneuf.avito.ma` project units are
    skipped. Real JSON paths: `props.pageProps.componentProps.ads.ads`, surface/bedrooms in
    `params.secondary[key=…]` (Avito's "rooms" key means bedrooms).

59. **Sarouty disabled.** It answers 403 to user agents that identify as bots. We don't spoof.

60. **Zone matching strips punctuation.** Real cards say "Ain Diab, Casablanca", and the old normaliser
    kept the comma, so *no* real listing matched a zone. Covered by a test on recorded pages.

61. **Structured amenities** (Mubawab's "Ascenseur", "Terrasse", "Concierge"…) are stored on the
    listing and fed to the rules extractor alongside the description.

62. **Recorded real pages** in `tests/fixtures/real/` (Mubawab search + detail, Avito search) back
    `tests/test_real_pages.py`. Refresh them with `radar record-fixture` when a site changes.

63. **Unknown surface = not a profile match.** The first real run's "best match" was a studio
    with no stated surface. The 80–100 m² requirement can't be verified, so it no longer counts as
    in-profile (it's still scored and listed).

64. **Sale filter.** Both sites mix rentals into "for sale" results ("à louer", 11 000 MAD/month).
    `pipeline.sale_filter` skips titles/URLs matching rental words and prices under 250 000 MAD.

65. **Avito detail pages are skipped** (`fetch_details: false`): search results already carry the
    description, photos, price and surface, and the detail JSON is an empty placeholder server-side.

66. **Phone dashboard = claude.ai artifact fed by `radar export`.** The container can't serve a
    public URL, so each refresh exports CSVs (listings, zones, run status) and uploads them as
    the dashboard's data files. Once the VPS runs `radar serve` behind HTTPS, the built-in dashboard
    (M7) covers the same need live.

## Public web page (2026-11-12)

67. **Public site = GitHub Pages, built by a daily GitHub Action** (`.github/workflows/site.yml`,
    18:20 UTC, after Mubawab's quota resets). `radar site` renders one self-contained HTML file
    (data embedded, no external requests). The DB and HTTP cache persist between runs via
    `actions/cache`, so price history and first-seen dates carry over.
    Requires a public repo (free plan) and Pages source set to "GitHub Actions".

68. **The public page shows less than the private tools.** It shows title, price, surface, zone, score,
    reasons and a link to the original ad. It carries no copied descriptions and no photos, which
    keeps it closer to "pointing to" the sites than republishing them.

69. **Detail fetches are capped per run** (`max_details_per_run`, Mubawab: 25 = its daily quota).
    The first GitHub run spent ~20 min on ~130 Mubawab detail pages that the quota would cut off anyway.
    Listings over the cap keep their card data and get their detail page on a later day.

70. **Public page redesigned with the ui-ux-pro-max skill** (github.com/nextlevelbuilder/ui-ux-pro-max-skill,
    run locally, stdlib-only). Its first suggestion (hero landing page, glassmorphism, display serif)
    didn't fit a data page. Following the skill's own retry rule, the dashboard query gave: Data-Dense
    Dashboard style, Fira Sans with tabular figures, blue data + amber highlight, a box plot for the
    per-zone distribution (30–39 points/zone, above its 20-point minimum), and sortable keyboard-operable
    table headers. Tables turn into cards on phones, the sort control and filters are tap-sized buttons,
    and a statistics table backs the chart. Off-scale prices (site typos like 245 000 MAD/m²) are pinned
    to the chart edge so they don't flatten every box.

## Purchase financing (2026-11-12)

71. **`config/financing.yaml` now ships researched market defaults instead of nulls.** The workbook
    values never arrived, and a simulation that refuses to run answers nothing. Each figure has an
    entry under `sources:` (URL, date, confidence) and a test fails if a non-zero value has none;
    a null still makes the simulation refuse. The research and its limits are in
    `docs/research/morocco-purchase-financing.md`: it was done through search extracts only
    (official sites were unreachable from the session), so most bank and notary figures are low
    confidence and must be replaced by the bank's offer and the notary's fee note.

72. **Scenarios are partial overrides of the `financing` block**, with `default_scenario` naming
    the one the pipeline, the dashboard and the public page use ("MRE – typical bank"). A config
    without scenarios behaves as before. Four ship: MRE typical / best case / cautious, and Morocco
    resident.

73. **Quoted rates are before tax: `interest_vat_rate` (10 %) is applied on top.** Moroccan banks
    advertise rates "HT" and add VAT on interest, so 4.80 % costs 5.28 %. Kept as a separate
    parameter (default 0) so a bank quote given tax-included can be entered as is.

74. **Cost lines accept progressive `tiers`.** Mortgage registration is 0.5 % up to 250 000 MAD,
    1.5 % to 5 M, 0.5 % above; a single rate could not express that.

75. **Affordability = two constraints** (`radar/simulation/affordability.py`): cash at signing
    ≤ savings with the down payment at least the scenario minimum (MRE: 20 %, the Office des
    Changes 80 % loan ceiling), and payment + existing debts ≤ `max_debt_ratio` (40 %) × net
    income. `max_price` searches whole dirhams and puts every spare dirham of savings into the down
    payment, since that minimises the payment. Income is entered in EUR and converted at the day's
    rate; banks' possible haircut on foreign income is not modelled.

76. **Income needed uses the first month's payment** (instalment + insurance). With insurance on
    outstanding capital the payment declines slightly afterwards, so this is the conservative figure.

77. **The page calculator is a separate file, `templates/finance_calc.js`, included into
    `site.html`,** so a test can run it under node against the Python engine on 54 cases and
    require identical dirham amounts. The expected values live in a JSON fixture that Python is
    also checked against, so the guard still works where node is missing.

78. **Per-listing cash / monthly payment / income needed on the page come from Python**, computed
    when `radar site` runs with the default scenario and the listing's agency flag; only the
    interactive calculator computes in the browser.

