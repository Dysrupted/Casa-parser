"""Daily pipeline (M1): crawl -> upsert listings -> snapshot -> detect changes/removals.

Idempotency contract: running N times on the same calendar day yields the same rows as
running once (snapshots/events are unique per listing+day; counters advance once per day).
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from pathlib import Path
from typing import Callable

from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from radar.config import Settings
from radar.http import FetchError
from radar.normalize.extract import description_hash
from radar.models import EventType, Listing, ListingEvent, ListingSnapshot, Property, Run, Status
from radar.sources.base import RawListing, Source

log = logging.getLogger(__name__)
Notifier = Callable[[str], None]  # M5 plugs Telegram in here


def _log_notifier(msg: str) -> None:
    log.error(msg)


class Pipeline:
    def __init__(
        self,
        settings: Settings,
        engine: Engine,
        sources: list[Source],
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        notify: Notifier = _log_notifier,
        stages: list | None = None,
    ):
        """`stages` run after crawling, in order: objects with `.name` and `.run(db, now, today)`."""
        self.s, self.engine, self.sources, self.stages = settings, engine, sources, stages or []
        self.now, self.notify = now, notify
        self.tz = ZoneInfo(settings.pipeline.timezone)

    def run(self) -> Run:
        started = self.now()
        with Session(self.engine) as db:
            run = Run(started_at=started)
            db.add(run)
            db.commit()
            db.refresh(run)
            stats: dict = {}
            for src in self.sources:
                try:
                    stats[src.name] = self._run_source(db, run, src, started)
                    db.commit()
                except Exception as e:  # fail soft: one source never kills the run
                    db.rollback()
                    log.exception("source %s failed", src.name)
                    stats[src.name] = {"status": "failed", "errors": [repr(e)]}
                    self.notify(f"Casa Radar: source {src.name} failed: {e!r}")
            statuses = {v["status"] for v in stats.values()}
            for stage in self.stages:
                try:
                    stats[stage.name] = st = stage.run(db, started, started.astimezone(self.tz).date())
                    db.commit()
                except Exception as e:  # a stage bug must not lose the crawl
                    db.rollback()
                    log.exception("stage %s failed", stage.name)
                    stats[stage.name] = st = {"errors": [repr(e)]}
                if st.get("errors"):
                    self.notify(f"Casa Radar: {stage.name} issues: {st['errors'][:3]}")
            run.status = "ok" if statuses <= {"ok"} else ("failed" if statuses == {"failed"} else "partial")
            run.stats, run.finished_at = stats, self.now()
            db.add(run)
            db.commit()
            db.refresh(run)
            return run

    # ----------------------------------------------------------------------------
    def _run_source(self, db: Session, run: Run, src: Source, now: datetime) -> dict:
        today = now.astimezone(self.tz).date()
        crawl = src.crawl()
        status = "ok" if crawl.complete else ("failed" if crawl.pages_fetched == 0 else "partial")
        st = {"status": status, "pages": crawl.pages_fetched,
              "cards": len(crawl.listings), "in_zone": 0, "new": 0, "price_changes": 0,
              "relisted": 0, "missing": 0, "removed": 0, "details_fetched": 0,
              "errors": list(crawl.errors), "notes": list(crawl.notes)}
        if crawl.errors:
            self.notify(f"Casa Radar: {src.name} crawl incomplete: {crawl.errors[:3]}")

        seen_ids: set[int] = set()
        sf = self.s.pipeline.sale_filter
        rx = re.compile(sf.exclude_title_regex, re.I) if sf.exclude_title_regex else None
        st["excluded_not_sale"] = 0
        for raw in crawl.listings:
            zone = self.s.zones.resolve(raw.location_raw) or self.s.zones.resolve(raw.title)
            if zone not in self.s.target_zones:
                continue
            if (raw.price_mad is not None and raw.price_mad < sf.min_price_mad) or (
                    rx and rx.search(f"{raw.title or ''} {raw.url}".replace("-", " ").replace("_", " "))):
                st["excluded_not_sale"] += 1  # rental mixed into sale results
                continue
            st["in_zone"] += 1
            listing = self._upsert(db, run, raw, zone, now, st)
            if src.cfg.fetch_details and self._needs_detail(listing, now):
                self._fetch_detail(src, listing, now, st)
            self._snapshot(db, run, listing, today)
            seen_ids.add(listing.id)

        detail_errors = [e for e in st["errors"] if e.startswith("detail ")]
        if detail_errors:
            self.notify(f"Casa Radar: {src.name}: {len(detail_errors)} detail page error(s): {detail_errors[:2]}")

        # Removal detection only after a complete crawl: a partial crawl proves nothing.
        if crawl.complete:
            self._mark_unseen(db, run, src.name, seen_ids, today, st)
        return st

    def _upsert(self, db: Session, run: Run, raw: RawListing, zone: str, now: datetime, st: dict) -> Listing:
        today = now.astimezone(self.tz).date()
        listing = db.exec(
            select(Listing).where(Listing.source == raw.source, Listing.external_id == raw.external_id)
        ).first()
        if listing is None:
            prop = Property(zone=zone, surface_m2=raw.surface_m2, rooms=raw.rooms,
                            bedrooms=raw.bedrooms, first_seen=now, last_seen=now)
            db.add(prop)
            db.flush()
            listing = Listing(property_id=prop.id, zone=zone, first_seen=now, last_seen=now,
                              **raw.model_dump(exclude={"photos", "amenities"}), photos=raw.photos,
                              amenities=raw.amenities or None,
                              description_hash=description_hash(raw.description))
            db.add(listing)
            db.flush()
            self._event(db, run, listing, today, EventType.new, new=listing.price_mad)
            st["new"] += 1
            return listing

        # Existing listing: price change, relist, refresh card fields.
        if raw.price_mad is not None and listing.price_mad is not None and raw.price_mad != listing.price_mad:
            if self._event(db, run, listing, today, EventType.price_change,
                           old=listing.price_mad, new=raw.price_mad):
                st["price_changes"] += 1
        if listing.status != Status.active:
            if listing.status == Status.removed:
                listing.relist_count += 1
                self._event(db, run, listing, today, EventType.relisted, new=raw.price_mad)
                st["relisted"] += 1
            listing.status = Status.active
        listing.missing_runs = 0
        if raw.description and raw.description != listing.description and listing.detail_fetched_at is None:
            listing.description, listing.description_hash = raw.description, description_hash(raw.description)
        for f in ("url", "title", "location_raw", "surface_m2", "rooms", "bedrooms", "bathrooms", "agency"):
            v = getattr(raw, f)
            if v is not None:
                setattr(listing, f, v)
        if raw.price_mad is not None:
            listing.price_mad = raw.price_mad
        if raw.amenities:
            listing.amenities = sorted(set(listing.amenities or []) | set(raw.amenities))
        if raw.photos and not listing.photos:
            listing.photos = raw.photos
        listing.zone, listing.last_seen = zone, now
        prop = db.get(Property, listing.property_id)
        prop.last_seen, prop.status = now, Status.active
        db.add_all([listing, prop])
        return listing

    def _needs_detail(self, listing: Listing, now: datetime) -> bool:
        if listing.detail_fetched_at is None:
            return True
        return now - listing.detail_fetched_at > timedelta(days=self.s.pipeline.refresh_detail_days)

    def _fetch_detail(self, src: Source, listing: Listing, now: datetime, st: dict) -> None:
        try:
            html, d = src.fetch_detail(listing.url)
        except FetchError as e:
            st["errors"].append(f"detail {listing.url}: {e}")
            return
        except Exception as e:  # parser bug: keep the card data, log, move on
            log.exception("detail parse failed for %s", listing.url)
            st["errors"].append(f"detail {listing.url}: parse error {e!r}")
            return
        rel = Path("raw") / src.name / listing.external_id / f"{now.astimezone(self.tz).date().isoformat()}.html"
        path = self.s.data_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(html, encoding="utf-8")
        listing.raw_html_path, listing.detail_fetched_at = str(rel), now
        listing.description = d.description or listing.description
        listing.description_hash = description_hash(listing.description)
        listing.address_raw = d.address_raw or listing.address_raw
        if d.amenities:
            listing.amenities = sorted(set(listing.amenities or []) | set(d.amenities))
        if d.lat is not None and d.lon is not None:
            listing.page_lat, listing.page_lon = d.lat, d.lon
        if d.photos:
            listing.photos = d.photos
        listing.agency = listing.agency or d.agency
        for f in ("surface_m2", "rooms", "bedrooms", "bathrooms", "price_mad"):
            if getattr(listing, f) is None and getattr(d, f) is not None:
                setattr(listing, f, getattr(d, f))
        st["details_fetched"] += 1

    def _snapshot(self, db: Session, run: Run, listing: Listing, today: date) -> None:
        snap = db.exec(select(ListingSnapshot).where(
            ListingSnapshot.listing_id == listing.id, ListingSnapshot.run_date == today)).first()
        snap = snap or ListingSnapshot(listing_id=listing.id, run_date=today, run_id=run.id, status=listing.status)
        snap.run_id, snap.price_mad, snap.status = run.id, listing.price_mad, listing.status
        db.add(snap)

    def _mark_unseen(self, db: Session, run: Run, source: str, seen: set[int], today: date, st: dict) -> None:
        candidates = db.exec(select(Listing).where(
            Listing.source == source, Listing.status != Status.removed)).all()
        for l in candidates:
            if l.id in seen:
                continue
            # Advance the counter at most once per calendar day (same-day reruns are no-ops).
            if self._event(db, run, l, today, EventType.missing, old=l.price_mad):
                l.missing_runs += 1
                st["missing"] += 1
            if l.missing_runs >= self.s.pipeline.missing_runs_before_removed:
                if l.status != Status.removed:
                    l.status = Status.removed
                    self._event(db, run, l, today, EventType.removed, old=l.price_mad)
                    st["removed"] += 1
                    prop = db.get(Property, l.property_id)
                    if all(x.status == Status.removed for x in db.exec(
                            select(Listing).where(Listing.property_id == prop.id))):
                        prop.status = Status.removed
                        db.add(prop)
            else:
                l.status = Status.missing
            db.add(l)
            self._snapshot(db, run, l, today)

    def _event(self, db: Session, run: Run, listing: Listing, today: date, type_: EventType,
               old: int | None = None, new: int | None = None) -> bool:
        """Insert or update today's event of this type. Returns True if newly created."""
        ev = db.exec(select(ListingEvent).where(
            ListingEvent.listing_id == listing.id, ListingEvent.run_date == today,
            ListingEvent.type == type_)).first()
        if ev is not None:
            ev.new_value = new  # keep the day's first old_value, latest new_value
            db.add(ev)
            return False
        db.add(ListingEvent(listing_id=listing.id, run_id=run.id, run_date=today,
                            type=type_, old_value=old, new_value=new))
        db.flush()
        return True


def build_sources(settings: Settings, fetcher, only: list[str] | None = None) -> list[Source]:
    """`fetcher` serves plain-HTTP sources; `fetcher: playwright` sources get a rendering fetcher."""
    from radar.http import PlaywrightFetcher
    from radar.sources import REGISTRY

    pw = None
    out = []
    for name, cfg in settings.sources.items():
        if not cfg.enabled or (only and name not in only):
            continue
        if name not in REGISTRY:
            log.warning("source %s configured but not implemented; skipping", name)
            continue
        f = fetcher
        if cfg.fetcher == "playwright":
            pw = pw or PlaywrightFetcher(settings.http, settings.data_dir / "cache")
            f = pw
        out.append(REGISTRY[name](cfg, f))
    return out
