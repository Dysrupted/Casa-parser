"""Run the simulation for every active priced listing and persist it."""
from __future__ import annotations

from datetime import date, datetime

from sqlmodel import Session, select

from radar.config import Settings
from radar.enrich.fx import rate_for
from radar.models import Listing, Property, Simulation, Status
from radar.normalize.extract import resolved_features
from radar.simulation.engine import simulate


def is_agency_listing(db: Session, listing: Listing, min_conf: float) -> bool:
    f = resolved_features(db, listing.property_id, min_conf).get("agency_or_private")
    if f is not None:
        return f.value == "agency"
    return listing.agency is not None


def simulate_all(db: Session, s: Settings, now: datetime, today: date) -> dict:
    params, h = s.financing, s.financing.hash()
    if (missing := params.missing()):
        return {"status": "skipped", "missing_params": missing}
    eur = rate_for(db, s.fx, today)
    n = 0
    from radar.scoring.store import best_listing  # local: avoid import cycle

    for prop in db.exec(select(Property).where(Property.status != Status.removed)).all():
        l = best_listing(db, prop.id)
        if l is None or not l.price_mad:
            continue
        res = simulate(l.price_mad, params, is_agency_listing(db, l, s.llm.min_confidence), eur)
        row = db.exec(select(Simulation).where(Simulation.property_id == l.property_id,
                                               Simulation.params_hash == h)).first()
        row = row or Simulation(property_id=l.property_id, params_hash=h, price_mad=l.price_mad,
                                ok=res.ok, computed_at=now)
        row.price_mad, row.ok, row.outputs, row.computed_at = l.price_mad, res.ok, res.to_dict(), now
        db.add(row)
        n += 1
    return {"status": "ok", "simulated": n, "params_hash": h, "eur_mad": eur,
            "scenario": params.default_scenario}
