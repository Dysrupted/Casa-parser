"""Read models shared by the JSON API and the HTML dashboard."""
from __future__ import annotations

import statistics
from datetime import date, datetime, timezone

from sqlmodel import Session, select

from radar.config import Settings
from radar.enrich.fx import rate_for, to_eur
from radar.market.index import latest_index
from radar.models import (Goal, Listing, ListingSnapshot, Property, PropertyFeature, Score, Simulation, Status,
                          UserNote, ZoneIndex)
from radar.scoring.store import best_listing, latest_score
from radar.simulation import simulate

SORTS = {"score", "price", "price_vs_zone", "dom", "price_m2", "new"}


def today_local(s: Settings) -> date:
    from zoneinfo import ZoneInfo

    return datetime.now(timezone.utc).astimezone(ZoneInfo(s.pipeline.timezone)).date()


def _zone_label(z):
    return (z or "?").replace("_", " ").title()


def listing_rows(db: Session, s: Settings, zone: str | None = None, min_score: float | None = None,
                 in_profile: bool | None = None, watch_only: bool = False, include_removed: bool = False,
                 sort: str = "score", limit: int = 200) -> list[dict]:
    today = today_local(s)
    rate = rate_for(db, s.fx, today)
    notes = {n.property_id: n for n in db.exec(select(UserNote))}
    q = select(Property)
    if not include_removed:
        q = q.where(Property.status != Status.removed)
    if zone:
        q = q.where(Property.zone == zone)
    rows = []
    for p in db.exec(q):
        l = best_listing(db, p.id)
        if l is None:
            continue
        sc = latest_score(db, p.id)
        n = notes.get(p.id)
        if watch_only and (n is None or n.status.value == "rejected"):
            continue
        if min_score is not None and (sc is None or sc.total < min_score):
            continue
        if in_profile is not None and (sc is None or sc.in_profile != in_profile):
            continue
        sources = db.exec(select(Listing.source).where(Listing.property_id == p.id)).all()
        rows.append({
            "id": p.id, "title": l.title, "url": l.url, "zone": p.zone, "zone_label": _zone_label(p.zone),
            "price_mad": l.price_mad, "price_eur": to_eur(l.price_mad, rate), "surface_m2": l.surface_m2,
            "price_m2": round(l.price_mad / l.surface_m2) if l.price_mad and l.surface_m2 else None,
            "price_vs_zone": sc.breakdown.get("price_vs_zone") if sc else None,
            "score": sc.total if sc else None, "in_profile": sc.in_profile if sc else None,
            "top_reasons": sc.breakdown.get("top_reasons", []) if sc else [],
            "dom": (today - p.first_seen.date()).days, "first_seen": p.first_seen.isoformat(),
            "status": p.status.value, "note_status": n.status.value if n else None,
            "photo": (l.photos or [None])[0], "sources": sorted(set(sources)), "dist_corniche_m": p.dist_corniche_m,
        })
    key = {"score": lambda r: -(r["score"] or -1), "price": lambda r: r["price_mad"] or 1e12,
           "price_vs_zone": lambda r: r["price_vs_zone"] or 9, "dom": lambda r: -r["dom"],
           "price_m2": lambda r: r["price_m2"] or 1e12, "new": lambda r: r["first_seen"]}[sort if sort in SORTS else "score"]
    rows.sort(key=key, reverse=(sort == "new"))
    return rows[:limit]


def property_detail(db: Session, s: Settings, pid: int) -> dict | None:
    p = db.get(Property, pid)
    if p is None:
        return None
    today = today_local(s)
    listings = db.exec(select(Listing).where(Listing.property_id == pid)).all()
    best = best_listing(db, pid) or listings[0]
    feats = db.exec(select(PropertyFeature).where(PropertyFeature.property_id == pid)
                    .order_by(PropertyFeature.field, PropertyFeature.extracted_by)).all()
    history: dict[str, list] = {}
    for l in listings:
        for sn in db.exec(select(ListingSnapshot).where(ListingSnapshot.listing_id == l.id)
                          .order_by(ListingSnapshot.run_date)):
            history.setdefault(l.source, []).append({"date": sn.run_date.isoformat(), "price_mad": sn.price_mad})
    sc = latest_score(db, pid)
    sim = db.exec(select(Simulation).where(Simulation.property_id == pid)
                  .order_by(Simulation.computed_at.desc())).first()
    n = db.get(UserNote, pid)
    zi = latest_index(db, p.zone) if p.zone else None
    photos = list(dict.fromkeys(ph for l in listings for ph in (l.photos or [])))
    return {
        "id": pid, "zone": p.zone, "zone_label": _zone_label(p.zone), "status": p.status.value,
        "title": best.title, "description": best.description, "price_mad": best.price_mad,
        "price_eur": to_eur(best.price_mad, rate_for(db, s.fx, today)), "surface_m2": best.surface_m2,
        "rooms": best.rooms or p.rooms, "bedrooms": best.bedrooms or p.bedrooms,
        "lat": p.lat, "lon": p.lon, "geo_precision": p.geo_precision, "dist_corniche_m": p.dist_corniche_m,
        "first_seen": p.first_seen.isoformat(), "dom": (today - p.first_seen.date()).days,
        "photos": photos[:12],
        "listings": [{"source": l.source, "url": l.url, "price_mad": l.price_mad, "status": l.status.value,
                      "agency": l.agency, "relist_count": l.relist_count} for l in listings],
        "features": [{"field": f.field, "value": f.value, "confidence": f.confidence, "by": f.extracted_by,
                      "span": f.source_span} for f in feats if f.value is not None],
        "score": sc.breakdown | {"date": sc.date.isoformat()} if sc else None,
        "simulation": sim.outputs if sim else None,
        "history": history,
        "zone_index": {"median_m2": zi.median_m2, "p25": zi.p25, "p75": zi.p75, "count": zi.count,
                       "reliable": zi.reliable} if zi else None,
        "note": {"status": n.status.value, "note": n.note, "reject_reason": n.reject_reason,
                 "actual_sale_price_mad": n.actual_sale_price_mad} if n else None,
    }


def market(db: Session, s: Settings, zone: str | None = None) -> list[dict]:
    today = today_local(s)
    zones = [zone] if zone else list(s.profile.zones)
    out = []
    for z in zones:
        series = db.exec(select(ZoneIndex).where(ZoneIndex.zone == z).order_by(ZoneIndex.date)).all()
        props = db.exec(select(Property).where(Property.zone == z, Property.status != Status.removed)).all()
        doms = [(today - p.first_seen.date()).days for p in props]
        cur = series[-1] if series else None
        out.append({
            "zone": z, "zone_label": _zone_label(z), "inventory": len(props),
            "median_dom": statistics.median(doms) if doms else None,
            "current": {"date": cur.date.isoformat(), "median_m2": cur.median_m2, "p25": cur.p25, "p75": cur.p75,
                        "count": cur.count, "reliable": cur.reliable, "trend_30d": cur.trend_30d,
                        "trend_90d": cur.trend_90d} if cur else None,
            "series": [{"date": r.date.isoformat(), "median_m2": r.median_m2, "count": r.count} for r in series],
        })
    return out


def goal(db: Session, s: Settings) -> dict:
    """PRD F9: target listing (or manual price) with cash needed + monthly payment."""
    g = db.get(Goal, 1)
    if g is None:
        return {"set": False}
    today = today_local(s)
    eur = rate_for(db, s.fx, today)
    if g.property_id:
        d = property_detail(db, s, g.property_id)
        if d is None:
            return {"set": False, "error": "target property no longer exists"}
        sim_out = d["simulation"]
        if sim_out is None and d["price_mad"]:
            sim_out = simulate(d["price_mad"], s.financing, False, eur).to_dict()
        src = {"type": "listing", "property_id": g.property_id, "title": d["title"], "zone": d["zone"],
               "status": d["status"], "url": d["listings"][0]["url"] if d["listings"] else None,
               "price_mad": d["price_mad"]}
    else:
        sim_out = simulate(g.manual_price_mad, s.financing, g.agency_listing, eur).to_dict()
        src = {"type": "manual", "price_mad": g.manual_price_mad}
    sim_out = sim_out or {"ok": False, "missing_params": ["price"]}
    return {
        "set": True, **src, "updated_at": g.updated_at.isoformat(), "eur_mad": eur,
        "simulation_ok": sim_out.get("ok", False), "missing_params": sim_out.get("missing_params", []),
        "total_cash_needed_mad": sim_out.get("total_cash_needed_mad"),
        "total_cash_needed_eur": sim_out.get("total_cash_needed_eur"),
        "monthly_payment_mad": sim_out.get("monthly_payment_mad"),
        "all_in_price_mad": sim_out.get("all_in_price_mad"), "all_in_price_eur": sim_out.get("all_in_price_eur"),
    }


def set_goal(db: Session, property_id: int | None, manual_price_mad: int | None, agency: bool = False) -> None:
    if (property_id is None) == (manual_price_mad is None):
        raise ValueError("set exactly one of property_id or manual_price_mad")
    if property_id is not None and db.get(Property, property_id) is None:
        raise LookupError(f"no property {property_id}")
    g = db.get(Goal, 1) or Goal(id=1, updated_at=datetime.now(timezone.utc))
    g.property_id, g.manual_price_mad, g.agency_listing = property_id, manual_price_mad, agency
    g.updated_at = datetime.now(timezone.utc)
    db.add(g)
    db.commit()
