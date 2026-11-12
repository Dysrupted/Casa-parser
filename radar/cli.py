"""`radar` CLI. Cron calls `radar run`; `radar run` by hand is the manual trigger."""
from __future__ import annotations

import json
import logging
from pathlib import Path

import typer
from sqlmodel import Session, func, select

from radar.config import Settings
from radar.db import init_db, make_engine
from radar.http import PoliteFetcher
from radar.enrich.fx import rate_for
from radar.llm.provider import build_provider
from radar.models import Listing, ListingEvent, Property, PropertyFeature, Run, Status
from radar.pipeline.analyze import Analyzer
from radar.pipeline.enrich import Enricher
from radar.pipeline.run import Pipeline, build_sources

app = typer.Typer(no_args_is_help=True, add_completion=False)


def _ctx() -> tuple[Settings, object]:
    s = Settings()
    s.data_dir.mkdir(parents=True, exist_ok=True)
    engine = make_engine(s.db_url)
    init_db(engine)
    return s, engine


@app.callback()
def main(verbose: bool = typer.Option(False, "-v")) -> None:
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")


@app.command()
def run(source: list[str] = typer.Option(None, "--source", "-s", help="Limit to these sources")) -> None:
    """Crawl enabled sources, store listings + daily snapshots."""
    s, engine = _ctx()
    fetcher = PoliteFetcher(s.http, s.data_dir / "cache")
    enricher = Enricher(s, fetcher, build_provider(s.llm))
    result = Pipeline(s, engine, build_sources(s, fetcher, source), stages=[enricher, Analyzer(s)]).run()
    typer.echo(json.dumps({"run_id": result.id, "status": result.status, "stats": result.stats}, indent=2))
    raise typer.Exit(0 if result.status != "failed" else 1)


@app.command()
def status() -> None:
    """Quick DB summary."""
    _, engine = _ctx()
    with Session(engine) as db:
        by = db.exec(select(Listing.zone, Listing.status, func.count()).group_by(Listing.zone, Listing.status)).all()
        last = db.exec(select(Run).order_by(Run.id.desc())).first()
        events = db.exec(select(ListingEvent.type, func.count()).group_by(ListingEvent.type)).all()
    typer.echo(f"last run: {last.id if last else '-'} {last.status if last else ''} {last.started_at if last else ''}")
    for zone, st, n in by:
        typer.echo(f"  {zone or '?':10} {st:8} {n}")
    typer.echo("events: " + ", ".join(f"{t}={n}" for t, n in events))
    typer.echo("core-field coverage (active listings): " + json.dumps(_coverage(engine)))


def _coverage(engine) -> dict:
    """M2 acceptance metric: share of active listings with each core structured field."""
    with Session(engine) as db:
        rows = db.exec(select(Listing, Property).join(Property, Listing.property_id == Property.id)
                       .where(Listing.status != Status.removed)).all()
    if not rows:
        return {}
    checks = {
        "price": lambda l, p: l.price_mad is not None,
        "surface": lambda l, p: l.surface_m2 is not None,
        "rooms": lambda l, p: l.rooms is not None or l.bedrooms is not None,
        "zone": lambda l, p: l.zone is not None,
        "geo": lambda l, p: p.lat is not None,
        "description": lambda l, p: bool(l.description),
    }
    cov = {k: round(100 * sum(f(l, p) for l, p in rows) / len(rows), 1) for k, f in checks.items()}
    cov["all_core"] = round(100 * sum(all(f(l, p) for f in checks.values()) for l, p in rows) / len(rows), 1)
    cov["n"] = len(rows)
    return cov


@app.command()
def enrich() -> None:
    """Re-run FX, feature extraction and geo without crawling."""
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo

    s, engine = _ctx()
    fetcher = PoliteFetcher(s.http, s.data_dir / "cache")
    now = datetime.now(timezone.utc)
    with Session(engine) as db:
        st = Enricher(s, fetcher, build_provider(s.llm)).run(db, now, now.astimezone(ZoneInfo(s.pipeline.timezone)).date())
    typer.echo(json.dumps(st, indent=2))


@app.command()
def spotcheck(n: int = typer.Option(20, "-n"), out: Path = typer.Option(Path("data/spotcheck.md")),
              seed: int = typer.Option(0)) -> None:
    """Write a Markdown sheet of N random listings: description vs extracted values + spans.

    M2 acceptance: review it and confirm no value is fabricated.
    """
    import random

    s, engine = _ctx()
    with Session(engine) as db:
        listings = db.exec(select(Listing).where(Listing.description.is_not(None))).all()
        random.Random(seed).shuffle(listings)
        lines = ["# Extraction spot-check", ""]
        for l in listings[:n]:
            lines += [f"## [{l.title or l.external_id}]({l.url})", "", f"> {l.description}", "",
                      "| field | value | conf | by | span |", "|---|---|---|---|---|"]
            feats = db.exec(select(PropertyFeature).where(PropertyFeature.property_id == l.property_id,
                                                          PropertyFeature.value.is_not(None))
                            .order_by(PropertyFeature.field, PropertyFeature.extracted_by)).all()
            for f in feats:
                lines.append(f"| {f.field} | {f.value} | {f.confidence:.2f} | {f.extracted_by} | {f.source_span or ''} |")
            lines.append("")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines), encoding="utf-8")
    typer.echo(f"wrote {min(n, len(listings))} listings -> {out}")


@app.command("record-fixture")
def record_fixture(url: str, out: Path = typer.Argument(..., help="e.g. tests/fixtures/mubawab/search_p1.html"),
                   kind: str = typer.Option("search", help="search | detail")) -> None:
    """Fetch one page politely (robots + delay) and save it as a test fixture."""
    s = Settings()
    html = PoliteFetcher(s.http, s.data_dir / "cache").get(url, kind)  # type: ignore[arg-type]
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    typer.echo(f"saved {len(html)} bytes -> {out}")


@app.command("simulate")
def simulate_cmd(price: float = typer.Argument(..., help="Price in MAD"),
                 agency: bool = typer.Option(False, help="Listing sold through an agency"),
                 eur_mad: float = typer.Option(None, help="Override EUR/MAD (default: latest stored)")) -> None:
    """Simulate the all-in cost of buying at PRICE with config/financing.yaml."""
    from datetime import date

    from radar.simulation import simulate

    s, engine = _ctx()
    if eur_mad is None:
        with Session(engine) as db:
            eur_mad = rate_for(db, s.fx, date.today())
    res = simulate(price, s.financing, agency_listing=agency, eur_mad=eur_mad)
    if not res.ok:
        typer.echo("Missing parameters in config/financing.yaml (copy them from the workbook):")
        for m in res.missing_params:
            typer.echo(f"  - {m}")
        raise typer.Exit(2)
    typer.echo(json.dumps(res.to_dict(), indent=2))


@app.command()
def top(n: int = typer.Option(10, "-n"), all_: bool = typer.Option(False, "--all", help="Include out-of-profile")) -> None:
    """Best-scored active listings with the reasons behind each score."""
    from radar.models import Score

    _, engine = _ctx()
    with Session(engine) as db:
        latest = db.exec(select(Score.date).order_by(Score.date.desc())).first()
        q = select(Score).where(Score.date == latest).order_by(Score.total.desc())
        if not all_:
            q = q.where(Score.in_profile)
        for sc in db.exec(q.limit(n)):
            l = db.exec(select(Listing).where(Listing.property_id == sc.property_id)).first()
            b = sc.breakdown
            pvz = f"{b['price_vs_zone']:.2f}x zone" if b.get("price_vs_zone") else "-"
            typer.echo(f"{sc.total:5.1f}  {l.zone or '?':9} {l.price_mad or 0:>10,} MAD  {l.surface_m2 or 0:>5g} m²  {pvz}")
            typer.echo(f"       {l.url}")
            for r in b.get("top_reasons", []):
                typer.echo(f"       + {r}")
            for r in b.get("penalty_reasons", []) + b.get("flags", []) + b.get("profile_notes", []):
                typer.echo(f"       - {r}")


@app.command()
def index() -> None:
    """Latest asking-price/m² index per zone."""
    from radar.market.index import latest_index

    s, engine = _ctx()
    with Session(engine) as db:
        for z in s.profile.zones:
            r = latest_index(db, z)
            if r is None:
                typer.echo(f"{z:10} no data")
                continue
            tr = " ".join(f"{d}d {getattr(r, f'trend_{d}d'):+.1%}" for d in (30, 90) if getattr(r, f"trend_{d}d") is not None)
            typer.echo(f"{z:10} median {r.median_m2:>7,.0f}  p25 {r.p25:>7,.0f}  p75 {r.p75:>7,.0f}  n={r.count:<3}"
                       f"{'' if r.reliable else ' (unreliable)'}  {tr}")


if __name__ == "__main__":
    app()
