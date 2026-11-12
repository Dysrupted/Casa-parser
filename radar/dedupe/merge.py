"""Merge two properties into one (survivor keeps the earliest first_seen)."""
from __future__ import annotations

from datetime import datetime

from sqlmodel import Session, delete, select

from radar.enrich.geo import PRECISION_RANK
from radar.models import (AlertLog, Listing, Property, PropertyFeature, PropertyMerge, Score, Simulation,
                          Status, UserNote)


def pick_survivor(a: Property, b: Property) -> tuple[Property, Property]:
    return (a, b) if (a.first_seen, a.id) <= (b.first_seen, b.id) else (b, a)


def merge_properties(db: Session, a_id: int, b_id: int, reason: str, now: datetime) -> int:
    """Merge; returns the survivor id. Derived rows (scores, simulations) are dropped and recomputed."""
    a, b = db.get(Property, a_id), db.get(Property, b_id)
    if a is None or b is None or a.id == b.id:
        return (a or b).id
    keep, lose = pick_survivor(a, b)

    for l in db.exec(select(Listing).where(Listing.property_id == lose.id)):
        l.property_id = keep.id
        db.add(l)

    for f in db.exec(select(PropertyFeature).where(PropertyFeature.property_id == lose.id)).all():
        mine = db.exec(select(PropertyFeature).where(
            PropertyFeature.property_id == keep.id, PropertyFeature.field == f.field,
            PropertyFeature.extracted_by == f.extracted_by)).first()
        if mine is None:
            f.property_id = keep.id
            db.add(f)
            continue
        if f.value is not None and (mine.value is None or f.confidence > mine.confidence):
            mine.value, mine.confidence, mine.source_span = f.value, f.confidence, f.source_span
            mine.source_listing_id, mine.extracted_at = f.source_listing_id, f.extracted_at
            db.add(mine)
        db.delete(f)

    db.exec(delete(Score).where(Score.property_id == lose.id))
    db.exec(delete(Simulation).where(Simulation.property_id == lose.id))

    ln, kn = db.get(UserNote, lose.id), db.get(UserNote, keep.id)
    if ln is not None:
        if kn is None:
            db.add(UserNote(property_id=keep.id, status=ln.status, note=ln.note, reject_reason=ln.reject_reason,
                            actual_sale_price_mad=ln.actual_sale_price_mad, updated_at=ln.updated_at))
        elif ln.note:
            kn.note = f"{kn.note or ''}\n[merged #{lose.id}] {ln.note}".strip()
            db.add(kn)
        db.delete(ln)

    for al in db.exec(select(AlertLog).where(AlertLog.property_id == lose.id)):
        al.property_id = keep.id  # keeps "already alerted" knowledge on the survivor
        db.add(al)

    keep.first_seen = min(keep.first_seen, lose.first_seen)
    keep.last_seen = max(keep.last_seen, lose.last_seen)
    if PRECISION_RANK[lose.geo_precision] > PRECISION_RANK[keep.geo_precision]:
        keep.lat, keep.lon, keep.geo_precision, keep.dist_corniche_m = (
            lose.lat, lose.lon, lose.geo_precision, lose.dist_corniche_m)
    for k in ("surface_m2", "rooms", "bedrooms", "zone"):
        if getattr(keep, k) is None:
            setattr(keep, k, getattr(lose, k))
    db.flush()
    statuses = {l.status for l in db.exec(select(Listing).where(Listing.property_id == keep.id))}
    keep.status = Status.removed if statuses == {Status.removed} else Status.active
    db.add(keep)
    db.add(PropertyMerge(survivor_id=keep.id, loser_id=lose.id, reason=reason, merged_at=now))
    db.flush()
    db.delete(lose)
    db.flush()
    return keep.id
