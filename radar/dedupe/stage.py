"""Dedupe stage (M6): candidates (zone, surface, price) -> confirmation (pHash / text) -> merge."""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

from sqlmodel import Session, select

from radar.config import Settings
from radar.dedupe.merge import merge_properties
from radar.dedupe.similarity import description_similarity, min_hamming, phash_hex, within
from radar.http import FetchError
from radar.models import DedupePair, Listing, Status

log = logging.getLogger(__name__)


class Deduper:
    name = "dedupe"

    def __init__(self, settings: Settings, fetcher):
        self.s, self.cfg, self.fetcher = settings, settings.dedupe, fetcher

    # -- candidates ----------------------------------------------------------------
    def candidates(self, listings: list[Listing]) -> list[tuple[Listing, Listing]]:
        c = self.cfg.candidate
        ok = [l for l in listings if l.price_mad and l.surface_m2]
        ok.sort(key=lambda l: (l.zone or "", l.surface_m2))
        pairs = []
        for i, a in enumerate(ok):
            for b in ok[i + 1:]:
                if c.same_zone and b.zone != a.zone:
                    break
                if b.surface_m2 - a.surface_m2 > c.surface_tolerance_m2:
                    break
                if a.property_id != b.property_id and within(a.price_mad, b.price_mad, c.price_tolerance_pct):
                    pairs.append((a, b) if a.id < b.id else (b, a))
        return pairs

    # -- confirmation ----------------------------------------------------------------
    def _hashes(self, db: Session, l: Listing) -> list[str]:
        if l.photo_hashes is not None:
            return l.photo_hashes
        out = []
        for url in (l.photos or [])[: self.cfg.confirm.photos_per_listing]:
            try:
                h = phash_hex(self.fetcher.get_bytes(url, "photo"))
            except FetchError as e:
                log.info("photo fetch failed %s: %s", url, e)
                continue
            if h:
                out.append(h)
        if out or not l.photos:   # cache, but retry later if every download failed
            l.photo_hashes = out
            db.add(l)
        return out

    def confirm(self, db: Session, a: Listing, b: Listing) -> tuple[bool, int | None, float | None]:
        cc = self.cfg.confirm
        sim = None
        if min(len(a.description or ""), len(b.description or "")) >= cc.description_min_chars:
            sim = round(description_similarity(a.description, b.description), 3)
            if sim >= cc.description_min_similarity:
                return True, None, sim
        dist = min_hamming(self._hashes(db, a), self._hashes(db, b))
        return (dist is not None and dist <= cc.phash_max_distance), dist, sim

    # -- stage -----------------------------------------------------------------------
    def run(self, db: Session, now: datetime, today: date) -> dict:
        st = {"candidates": 0, "evaluated": 0, "merged": 0, "errors": []}
        if not self.cfg.enabled:
            return st | {"status": "disabled"}
        listings = db.exec(select(Listing).where(Listing.status != Status.removed)).all()
        pairs = self.candidates(listings)
        st["candidates"] = len(pairs)
        recheck = now - timedelta(days=self.cfg.recheck_rejected_after_days)
        for a, b in pairs:
            a, b = db.get(Listing, a.id), db.get(Listing, b.id)
            if a.property_id == b.property_id:   # merged transitively earlier in this loop
                continue
            prev = db.exec(select(DedupePair).where(DedupePair.listing_a == a.id,
                                                    DedupePair.listing_b == b.id)).first()
            if prev and prev.decision == "distinct" and prev.decided_at > recheck:
                continue
            dup, dist, sim = self.confirm(db, a, b)
            st["evaluated"] += 1
            row = prev or DedupePair(listing_a=a.id, listing_b=b.id, decision="", decided_at=now)
            row.decision, row.photo_distance, row.description_similarity, row.decided_at = (
                "duplicate" if dup else "distinct", dist, sim, now)
            db.add(row)
            if dup:
                why = f"auto: photo_dist={dist} desc_sim={sim}"
                merge_properties(db, a.property_id, b.property_id, why, now)
                st["merged"] += 1
            db.flush()
        return st
