"""Build ScoreInputs from the DB, score every active property, persist (one row/day)."""
from __future__ import annotations

from datetime import date, datetime, timedelta

from sqlmodel import Session, select

from radar.config import Settings
from radar.market.index import latest_index
from radar.models import Listing, ListingSnapshot, Property, Score, Status
from radar.normalize.extract import resolved_features
from radar.scoring.score import ScoreInput, ScoreResult, ZoneStats, score


def best_listing(db: Session, property_id: int) -> Listing | None:
    ls = db.exec(select(Listing).where(Listing.property_id == property_id,
                                       Listing.status != Status.removed)).all()
    priced = [l for l in ls if l.price_mad]
    return min(priced, key=lambda l: l.price_mad) if priced else (ls[0] if ls else None)


def price_drop_pct(db: Session, property_id: int, current: int | None, today: date, lookback: int) -> float:
    if not current:
        return 0.0
    ids = [l.id for l in db.exec(select(Listing).where(Listing.property_id == property_id))]
    snaps = db.exec(select(ListingSnapshot.price_mad).where(
        ListingSnapshot.listing_id.in_(ids), ListingSnapshot.run_date >= today - timedelta(days=lookback),
        ListingSnapshot.price_mad.is_not(None))).all()
    peak = max(snaps, default=current)
    return max(0.0, (peak - current) / peak)


def repost_count(db: Session, property_id: int) -> int:
    """Relists of the same ad + extra ads for the same flat on the same site (merged by dedupe)."""
    ls = db.exec(select(Listing).where(Listing.property_id == property_id)).all()
    per_source: dict[str, int] = {}
    for l in ls:
        per_source[l.source] = per_source.get(l.source, 0) + 1
    return sum(l.relist_count for l in ls) + sum(n - 1 for n in per_source.values())


def build_input(db: Session, s: Settings, prop: Property, l: Listing, today: date, cfg: dict) -> ScoreInput:
    zi = latest_index(db, prop.zone) if prop.zone else None
    feats = {k: v.value for k, v in resolved_features(db, prop.id, s.llm.min_confidence).items()}
    first_seen = prop.first_seen.date() if prop.first_seen else today
    return ScoreInput(
        zone=prop.zone, price_mad=l.price_mad, surface_m2=l.surface_m2 or prop.surface_m2,
        zone_stats=ZoneStats(zi.p25, zi.median_m2, zi.p75, zi.count, zi.reliable) if zi else None,
        dist_corniche_m=prop.dist_corniche_m, geo_precision=prop.geo_precision, features=feats,
        days_on_market=(today - first_seen).days,
        price_drop_pct=price_drop_pct(db, prop.id, l.price_mad, today,
                                      cfg["factors"]["market_signals"]["price_drop_lookback_days"]),
        relist_count=repost_count(db, prop.id), has_photos=bool(l.photos), has_description=bool(l.description),
        current_year=today.year)


def score_all(db: Session, s: Settings, now: datetime, today: date) -> dict:
    cfg = s.scoring
    n, in_profile = 0, 0
    for prop in db.exec(select(Property).where(Property.status != Status.removed)).all():
        l = best_listing(db, prop.id)
        if l is None:
            continue
        res: ScoreResult = score(build_input(db, s, prop, l, today, cfg), cfg, s.profile)
        row = db.exec(select(Score).where(Score.property_id == prop.id, Score.date == today)).first()
        row = row or Score(property_id=prop.id, date=today, computed_at=now, total=0, in_profile=False)
        row.total, row.in_profile, row.breakdown, row.computed_at = res.total, res.in_profile, res.to_dict(), now
        db.add(row)
        n += 1
        in_profile += res.in_profile
    return {"scored": n, "in_profile": in_profile}


def latest_score(db: Session, property_id: int) -> Score | None:
    return db.exec(select(Score).where(Score.property_id == property_id).order_by(Score.date.desc())).first()
