"""Per-zone asking-price/m² index (PRD F4). It is an *asking*-price index, not transactions."""
from __future__ import annotations

import statistics
from datetime import date, datetime, timedelta

from sqlmodel import Session, select

from radar.models import Listing, Property, Status, ZoneIndex


def quantiles(xs: list[float]) -> tuple[float, float, float]:
    """p25, median, p75 (inclusive method, matches Excel QUARTILE.INC)."""
    if len(xs) == 1:
        return xs[0], xs[0], xs[0]
    q = statistics.quantiles(xs, n=4, method="inclusive")
    return q[0], q[1], q[2]


def property_prices_m2(db: Session, since: datetime, zone: str | None = None) -> dict[str, list[float]]:
    """One price/m² per property (cheapest active listing) seen since `since`, grouped by zone."""
    q = (select(Property.id, Property.zone, Listing.price_mad, Listing.surface_m2)
         .join(Listing, Listing.property_id == Property.id)
         .where(Listing.last_seen >= since, Listing.status != Status.removed,
                Listing.price_mad.is_not(None), Listing.surface_m2 > 0, Property.zone.is_not(None)))
    if zone:
        q = q.where(Property.zone == zone)
    best: dict[int, tuple[str, float]] = {}
    for pid, z, price, surface in db.exec(q):
        v = price / surface
        if pid not in best or v < best[pid][1]:
            best[pid] = (z, v)
    out: dict[str, list[float]] = {}
    for z, v in best.values():
        out.setdefault(z, []).append(v)
    return out


def drop_outliers(xs: list[float], k: float) -> list[float]:
    if len(xs) < 4:
        return xs
    p25, _, p75 = quantiles(xs)
    lo, hi = p25 - k * (p75 - p25), p75 + k * (p75 - p25)
    return [x for x in xs if lo <= x <= hi]


def compute_index(db: Session, now: datetime, today: date, cfg: dict) -> dict[str, ZoneIndex]:
    since = now - timedelta(days=cfg["window_days"])
    out = {}
    for zone, xs in property_prices_m2(db, since).items():
        xs = sorted(drop_outliers(xs, cfg["outlier_iqr_k"]))
        p25, med, p75 = quantiles(xs)
        row = db.get(ZoneIndex, (zone, today)) or ZoneIndex(zone=zone, date=today, median_m2=med, p25=p25,
                                                            p75=p75, count=len(xs))
        row.median_m2, row.p25, row.p75, row.count = med, p25, p75, len(xs)  # unrounded: display rounds
        row.reliable = len(xs) >= cfg["min_points"]
        for days in cfg["trend_days"]:
            past = db.exec(select(ZoneIndex).where(ZoneIndex.zone == zone,
                                                   ZoneIndex.date <= today - timedelta(days=days))
                           .order_by(ZoneIndex.date.desc())).first()
            setattr(row, f"trend_{days}d", round(row.median_m2 / past.median_m2 - 1, 4) if past else None)
        db.add(row)
        out[zone] = row
    return out


def latest_index(db: Session, zone: str) -> ZoneIndex | None:
    return db.exec(select(ZoneIndex).where(ZoneIndex.zone == zone).order_by(ZoneIndex.date.desc())).first()
