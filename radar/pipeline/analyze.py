"""Analysis stage (M4): zone index, then scores."""
from __future__ import annotations

import logging
from datetime import date, datetime

from sqlmodel import Session

from radar.config import Settings
from radar.market.index import compute_index
from radar.scoring.store import score_all

log = logging.getLogger(__name__)


class Analyzer:
    name = "analyze"

    def __init__(self, settings: Settings):
        self.s = settings

    def run(self, db: Session, now: datetime, today: date) -> dict:
        st: dict = {"errors": []}
        idx = compute_index(db, now, today, self.s.scoring["market_index"])
        db.flush()
        st["index"] = {z: {"median_m2": r.median_m2, "count": r.count, "reliable": r.reliable}
                       for z, r in idx.items()}
        unreliable = [z for z, r in idx.items() if not r.reliable]
        if unreliable:
            log.info("zone index unreliable (few points) for %s", unreliable)
        st["scores"] = score_all(db, self.s, now, today)
        return st
