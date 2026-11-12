"""Enrichment stage (M2): FX, rules + LLM feature extraction, geo. Fail-soft per step."""
from __future__ import annotations

import logging
from datetime import date, datetime

from sqlmodel import Session, or_, select

from radar.config import Settings
from radar.enrich.fx import rate_for, refresh_rate
from radar.enrich.geo import PRECISION_RANK, Geocoder
from radar.http import Fetcher
from radar.llm.provider import LLMError, LLMProvider, LLMUnavailable
from radar.models import Listing, Property, Status
from radar.normalize.extract import apply_llm, apply_rules

log = logging.getLogger(__name__)


class Enricher:
    name = "enrich"

    def __init__(self, settings: Settings, fetcher: Fetcher, provider: LLMProvider | None, notify=log.error):
        self.s, self.fetcher, self.provider, self.notify = settings, fetcher, provider, notify
        self.geocoder = Geocoder(settings.geo, fetcher)

    def run(self, db: Session, now: datetime, today: date) -> dict:
        st = {"fx": None, "rules": 0, "llm": 0, "llm_rejected_fields": 0, "llm_errors": 0,
              "llm_pending": 0, "geo": {"page": 0, "address": 0, "zone": 0, "none": 0},
              "errors": []}
        for step in (self._fx, self._features, self._geo):
            try:
                step(db, now, today, st)
                db.commit()
            except Exception as e:
                db.rollback()
                log.exception("enrich step %s failed", step.__name__)
                st["errors"].append(f"{step.__name__}: {e!r}")
        return st

    def _fx(self, db, now, today, st):
        row = refresh_rate(db, self.s.fx, self.fetcher, today)
        st["fx"] = row.eur_mad if row else rate_for(db, self.s.fx, today)
        if row is None:
            if st["fx"] is None:  # nothing usable within max_staleness_days: EUR outputs go null
                st["errors"].append("fx: no usable EUR/MAD rate")
            else:
                st["fx_stale"] = True  # feed down today; yesterday's-ish rate still valid

    def _features(self, db: Session, now, today, st):
        listings = db.exec(select(Listing).where(Listing.status != Status.removed, or_(
            Listing.description.is_not(None), Listing.amenities.is_not(None)))).all()
        budget = self.s.llm.max_extractions_per_run
        for l in listings:
            apply_rules(db, l, now)  # cheap & deterministic: always refresh
            st["rules"] += 1
            if not l.description or l.extracted_hash == l.description_hash:
                continue
            if self.provider is None or budget <= 0:
                st["llm_pending"] += 1
                continue
            try:
                st["llm_rejected_fields"] += len(apply_llm(db, l, self.provider, now))
                st["llm"] += 1
                budget -= 1
            except LLMUnavailable as e:
                self.notify(f"Casa Radar: LLM unavailable, rules-only this run: {e}")
                self.provider = None
                st["llm_pending"] += 1
            except LLMError as e:
                st["llm_errors"] += 1
                st["llm_pending"] += 1
                log.warning("LLM extraction failed for listing %s: %s", l.id, e)

    def _geo(self, db: Session, now, today, st):
        rows = db.exec(select(Property, Listing).join(Listing, Listing.property_id == Property.id).where(
            Property.status != Status.removed,
            or_(Property.geo_precision.is_(None), Property.geo_precision != "page"))).all()
        for prop, l in rows:
            page = (l.page_lat, l.page_lon) if l.page_lat is not None and l.page_lon is not None else None
            # Don't re-geocode an address we already resolved; only upgrade precision.
            if prop.geo_precision == "address" and page is None:
                continue
            p, prec = self.geocoder.locate(page, l.address_raw, prop.zone)
            if p is None:
                st["geo"]["none"] += 1
                continue
            if PRECISION_RANK[prec] >= PRECISION_RANK[prop.geo_precision]:
                prop.lat, prop.lon, prop.geo_precision = p[0], p[1], prec
                prop.dist_corniche_m = round(self.geocoder.corniche_distance_m(p))
                db.add(prop)
            st["geo"][prec] += 1
