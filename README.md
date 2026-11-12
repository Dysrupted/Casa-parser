# Casa Radar

Casablanca apartment monitoring, pricing intelligence & purchase simulation.
Spec: PRD "Casa Radar" (v1). Decisions: [`docs/DECISIONS.md`](docs/DECISIONS.md).

## Status

| Milestone | Scope | State |
|---|---|---|
| M1 | Mubawab + DB + daily snapshots, idempotent reruns | ✅ built — awaiting real fixtures |
| M2 | LLM extraction + geo + FX | ⏳ |
| M3 | Simulation (workbook port) | ⏳ needs workbook + reference cases |
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
radar status              # counts per zone/status, last run, events
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
radar/
  config.py      typed YAML loading (pydantic), zone resolver
  http.py        PoliteFetcher — robots.txt, ≥3 s randomized delay, disk cache, retries
  models.py      property · listing · listing_snapshot · listing_event · run
  sources/       Source ABC (generic paginated crawl) + mubawab.py
  pipeline/run.py  crawl → upsert → detail fetch → snapshot → missing/removed detection
  cli.py
tests/           fixture-based; never hits live sites
data/            radar.db, cache/, raw/ (gitignored)
```

## Adding a source

1. Subclass `radar.sources.base.Source`, implement `parse_search_page` and `parse_detail_page`.
2. Register it in `radar/sources/__init__.py`.
3. Add a block in `config/sources.yaml` (URLs, pagination, selectors).
4. Record fixtures, write parser tests.
