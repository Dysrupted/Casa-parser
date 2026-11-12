# Casa Radar

Casablanca apartment monitoring, pricing intelligence & purchase simulation.
Spec: PRD "Casa Radar" (v1). Decisions: [`docs/DECISIONS.md`](docs/DECISIONS.md).

## Status

| Milestone | Scope | State |
|---|---|---|
| M1 | Mubawab + DB + daily snapshots, idempotent reruns | ✅ built — awaiting real fixtures |
| M2 | LLM extraction + geo + FX | ✅ built — needs API key + spot-check |
| M3 | Simulation (workbook port) | ✅ engine built — needs workbook rates + reference cases |
| M4 | Zone index + scoring | ⏳ |
| M5 | Telegram alerts | ⏳ |
| M6 | Avito, Sarouty + dedupe | ⏳ |
| M7 | Dashboard + watchlist + `/api/goal` | ⏳ |

## Setup

```bash
python3.12 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
pytest
```

## Usage

```bash
radar run                 # crawl all enabled sources (manual "run now")
radar run -s mubawab      # one source
radar status              # counts per zone/status, last run, events, core-field coverage
radar enrich              # re-run FX + feature extraction + geo without crawling
radar spotcheck -n 20     # data/spotcheck.md: description vs extracted values + spans (M2 review)
radar simulate 2500000 --agency   # all-in cost, cash needed, monthly payment (config/financing.yaml)
radar record-fixture "<url>" tests/fixtures/mubawab/search_p1.html   # capture a test fixture
```

Daily cron (VPS / Raspberry Pi), 07:17 Casablanca time:

```cron
CRON_TZ=Africa/Casablanca
17 7 * * * cd /opt/casa-radar && .venv/bin/radar run >> data/cron.log 2>&1
```

Exit code is non-zero only if every source failed.

## Layout

```
config/          search_profile.yaml · zones.yaml (alias table) · sources.yaml (http, pipeline, selectors)
                 enrich.yaml (LLM, geo centroids + Corniche polyline, FX)
radar/
  config.py      typed YAML loading (pydantic), zone resolver
  http.py        PoliteFetcher — robots.txt, ≥3 s randomized delay, disk cache, retries
  models.py      property · listing · listing_snapshot · listing_event · run
  sources/       Source ABC (generic paginated crawl) + mubawab.py
  pipeline/run.py  crawl → upsert → detail fetch → snapshot → missing/removed detection
  pipeline/enrich.py  FX → rules + LLM features → geo (fail-soft per step)
  normalize/     schema.py (strict extraction schema + span verification) · rules.py · extract.py
  llm/provider.py  LLMProvider protocol; Anthropic implementation
  enrich/        geo.py (precision ladder, Corniche distance) · fx.py
  simulation/    params.py (config model) · engine.py (pure, Decimal) · store.py (per-listing)
  migrations/    Alembic
  cli.py
tests/           fixture-based; never hits live sites
data/            radar.db, cache/, raw/ (gitignored)
```

## Adding a source

1. Subclass `radar.sources.base.Source`, implement `parse_search_page` and `parse_detail_page`.
2. Register it in `radar/sources/__init__.py`.
3. Add a block in `config/sources.yaml` (URLs, pagination, selectors).
4. Record fixtures, write parser tests.
