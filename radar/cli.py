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
from radar.alerts.engine import AlertStage
from radar.dedupe.stage import Deduper
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
    alerts = AlertStage(s)
    enricher.notify = alerts.buffer_error
    result = Pipeline(s, engine, build_sources(s, fetcher, source), notify=alerts.buffer_error,
                      stages=[enricher, Deduper(s, fetcher), Analyzer(s), alerts]).run()
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


def _resolve_property(db, ref: str) -> int:
    """Accept a property id or a listing URL."""
    if ref.isdigit():
        if db.get(Property, int(ref)) is None:
            raise typer.BadParameter(f"no property {ref}")
        return int(ref)
    l = db.exec(select(Listing).where(Listing.url == ref.split("?")[0])).first()
    if l is None:
        raise typer.BadParameter(f"no listing with url {ref}")
    return l.property_id


@app.command()
def note(ref: str = typer.Argument(..., help="Property id or listing URL"),
         status: str = typer.Option("watching", help="watching | visited | called | rejected"),
         text: str = typer.Option(None, "--text"), reason: str = typer.Option(None, "--reason"),
         sale_price: int = typer.Option(None, help="Actual sale price heard (MAD)")) -> None:
    """Add to watchlist / record a note. `--status rejected --reason "..."` silences alerts."""
    from datetime import datetime, timezone

    from radar.models import NoteStatus, UserNote

    _, engine = _ctx()
    with Session(engine) as db:
        pid = _resolve_property(db, ref)
        n = db.get(UserNote, pid) or UserNote(property_id=pid, updated_at=datetime.now(timezone.utc))
        n.status = NoteStatus(status)
        n.note = text if text is not None else n.note
        n.reject_reason = reason if reason is not None else n.reject_reason
        n.actual_sale_price_mad = sale_price if sale_price is not None else n.actual_sale_price_mad
        n.updated_at = datetime.now(timezone.utc)
        db.add(n)
        db.commit()
        typer.echo(f"property {pid}: {n.status}")


@app.command()
def watch(ref: str) -> None:
    """Shortcut: add a property (id or URL) to the watchlist."""
    note(ref, status="watching", text=None, reason=None, sale_price=None)


@app.command("alert-test")
def alert_test() -> None:
    """Send a test message to check the Telegram token + chat id (PRD M5)."""
    from radar.alerts.telegram import TelegramClient, TelegramError, format_alert

    s = Settings()
    if not (s.telegram_token and s.telegram_chat_id):
        typer.echo("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env")
        raise typer.Exit(2)
    try:
        TelegramClient(s.telegram_token, s.telegram_chat_id).send(
            format_alert({"type": "test", "messages": ["Telegram is wired up."]}))
    except TelegramError as e:
        typer.echo(f"failed: {e}")
        raise typer.Exit(1)
    typer.echo("sent")


@app.command()
def dupes(n: int = typer.Option(20, "-n")) -> None:
    """Recent merges with their listings' URLs: the M6 manual check (10 known pairs)."""
    from radar.models import PropertyMerge

    _, engine = _ctx()
    with Session(engine) as db:
        for m in db.exec(select(PropertyMerge).order_by(PropertyMerge.merged_at.desc()).limit(n)):
            typer.echo(f"#{m.survivor_id} <- #{m.loser_id}  {m.reason}  {m.merged_at:%Y-%m-%d}")
            for l in db.exec(select(Listing).where(Listing.property_id == m.survivor_id)):
                typer.echo(f"    {l.source:8} {l.price_mad or 0:>10,} MAD {l.surface_m2 or 0:>5g} m²  {l.url}")


@app.command()
def merge(a: str, b: str) -> None:
    """Manually merge two properties (ids or listing URLs) the matcher missed."""
    from datetime import datetime, timezone

    from radar.dedupe.merge import merge_properties

    _, engine = _ctx()
    with Session(engine) as db:
        keep = merge_properties(db, _resolve_property(db, a), _resolve_property(db, b), "manual",
                                datetime.now(timezone.utc))
        db.commit()
    typer.echo(f"merged into property {keep}")


@app.command()
def serve(host: str = typer.Option("127.0.0.1"), port: int = typer.Option(8000),
          insecure: bool = typer.Option(False, help="Allow a public bind without any credentials")) -> None:
    """Dashboard + API. Set RADAR_DASHBOARD_PASSWORD (and RADAR_API_TOKEN) before exposing it."""
    import os

    import uvicorn

    from radar.api.app import create_app

    s, engine = _ctx()
    creds = any(os.environ.get(k) for k in ("RADAR_DASHBOARD_PASSWORD", "RADAR_API_TOKEN", "RADAR_SHARE_TOKEN"))
    if host not in ("127.0.0.1", "localhost", "::1") and not creds and not insecure:
        typer.echo("Refusing to bind publicly without RADAR_DASHBOARD_PASSWORD / RADAR_API_TOKEN (or --insecure).")
        raise typer.Exit(2)
    uvicorn.run(create_app(s, engine), host=host, port=port)


@app.command("set-goal")
def set_goal_cmd(ref: str = typer.Argument(None, help="Property id or listing URL"),
                 price: int = typer.Option(None, help="Manual target price (MAD) instead of a listing"),
                 agency: bool = typer.Option(False)) -> None:
    """Set the purchase target exposed at GET /api/goal (Finance Cockpit)."""
    from radar.api import queries as Q

    s, engine = _ctx()
    with Session(engine) as db:
        pid = _resolve_property(db, ref) if ref else None
        Q.set_goal(db, pid, price, agency)
        typer.echo(json.dumps(Q.goal(db, s), indent=2))


@app.command()
def export(out: Path = typer.Option(Path("data/export"), help="Output folder")) -> None:
    """Snapshot listings + zone index as CSV (for the phone dashboard / spreadsheets)."""
    import csv
    from datetime import datetime, timezone

    from radar.api import queries as Q

    s, engine = _ctx()
    out.mkdir(parents=True, exist_ok=True)
    with Session(engine) as db:
        rows = Q.listing_rows(db, s, in_profile=None, include_removed=False, limit=100000)
        market = Q.market(db, s)
        last = db.exec(select(Run).order_by(Run.id.desc())).first()
    cols = ["id", "zone", "zone_label", "title", "url", "sources", "price_mad", "price_eur", "surface_m2",
            "price_m2", "price_vs_zone", "score", "in_profile", "dom", "dist_corniche_m", "reason_1",
            "reason_2", "reason_3", "first_seen"]
    with open(out / "listings.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in rows:
            reasons = (r["top_reasons"] + ["", "", ""])[:3]
            w.writerow({k: r.get(k) for k in cols if k in r} | {
                "sources": " + ".join(r["sources"]), "reason_1": reasons[0], "reason_2": reasons[1],
                "reason_3": reasons[2], "in_profile": "yes" if r["in_profile"] else "no",
                "dist_corniche_m": round(r["dist_corniche_m"]) if r["dist_corniche_m"] is not None else None})
    with open(out / "zones.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["zone", "zone_label", "median_m2", "p25_m2", "p75_m2", "listings_in_index", "reliable",
                    "inventory", "median_days_on_market"])
        for z in market:
            c = z["current"] or {}
            w.writerow([z["zone"], z["zone_label"], _r(c.get("median_m2")), _r(c.get("p25")), _r(c.get("p75")),
                        c.get("count"), "yes" if c.get("reliable") else "no", z["inventory"], z["median_dom"]])
    with open(out / "runs.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["source", "status", "cards_seen", "in_zone", "new", "notes", "errors", "run_at"])
        for name, st in (last.stats if last else {}).items():
            if name in s.sources:
                w.writerow([name, st.get("status"), st.get("cards"), st.get("in_zone"), st.get("new"),
                            len(st.get("notes", [])), len(st.get("errors", [])),
                            last.started_at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")])
    typer.echo(f"exported {len(rows)} listings, {len(market)} zones -> {out}")


@app.command()
def site(out: Path = typer.Option(Path("site"), help="Output folder for the static site")) -> None:
    """Build a self-contained public web page (index.html) from the latest data."""
    from datetime import timezone

    from jinja2 import Environment, FileSystemLoader, select_autoescape

    from radar.api import queries as Q
    from radar.enrich.fx import rate_for

    s, engine = _ctx()
    with Session(engine) as db:
        rows = Q.listing_rows(db, s, in_profile=None, limit=100000)
        market = Q.market(db, s)
        last = db.exec(select(Run).order_by(Run.id.desc())).first()
        eur = rate_for(db, s.fx, Q.today_local(s))
    listings = [{
        "zone": r["zone"], "zone_label": r["zone_label"], "title": r["title"], "url": r["url"],
        "sources": r["sources"], "price_mad": r["price_mad"], "price_eur": r["price_eur"],
        "surface_m2": r["surface_m2"], "price_m2": r["price_m2"], "price_vs_zone": r["price_vs_zone"],
        "score": r["score"], "in_profile": bool(r["in_profile"]), "first_seen": r["first_seen"],
        "reasons": r["top_reasons"][:3],
    } for r in rows]
    zones = [{"zone": z["zone"], "zone_label": z["zone_label"], "inventory": z["inventory"],
              **({"median_m2": z["current"]["median_m2"], "p25_m2": z["current"]["p25"],
                  "p75_m2": z["current"]["p75"], "listings_in_index": z["current"]["count"]}
                 if z["current"] else {"median_m2": None})} for z in market]
    run_at = last.started_at.astimezone(timezone.utc) if last else None
    meta = {"run_at_iso": run_at.isoformat() if run_at else "",
            "run_at_label": run_at.strftime("%-d %b %Y, %H:%M UTC") if run_at else "unknown",
            "eur_mad_label": f"1 EUR = {eur:.2f} MAD" if eur else "unavailable"}
    # "</" can't appear inside the inline JSON script
    data_json = json.dumps({"listings": listings, "zones": zones}, ensure_ascii=False).replace("</", "<\\/")
    env = Environment(loader=FileSystemLoader(Path(__file__).parent / "api" / "templates"),
                      autoescape=select_autoescape(["html"]))
    out.mkdir(parents=True, exist_ok=True)
    (out / "index.html").write_text(env.get_template("site.html").render(meta=meta, data_json=data_json),
                                    encoding="utf-8")
    (out / ".nojekyll").write_text("")
    typer.echo(f"site: {len(listings)} listings -> {out / 'index.html'}")


def _r(x):
    return None if x is None else round(x)


if __name__ == "__main__":
    app()
