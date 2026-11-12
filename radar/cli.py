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
from radar.models import Listing, ListingEvent, Run
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
    result = Pipeline(s, engine, build_sources(s, fetcher, source)).run()
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


@app.command("record-fixture")
def record_fixture(url: str, out: Path = typer.Argument(..., help="e.g. tests/fixtures/mubawab/search_p1.html"),
                   kind: str = typer.Option("search", help="search | detail")) -> None:
    """Fetch one page politely (robots + delay) and save it as a test fixture."""
    s = Settings()
    html = PoliteFetcher(s.http, s.data_dir / "cache").get(url, kind)  # type: ignore[arg-type]
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    typer.echo(f"saved {len(html)} bytes -> {out}")


if __name__ == "__main__":
    app()
