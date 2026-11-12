"""Daily EUR/MAD rate. MAD is primary everywhere; EUR is derived, never stored on listings."""
from __future__ import annotations

import json
import logging
from datetime import date, timedelta

from sqlmodel import Session, select

from radar.config import FxConfig
from radar.http import FetchError, Fetcher
from radar.models import FxRate

log = logging.getLogger(__name__)


def refresh_rate(db: Session, cfg: FxConfig, fetcher: Fetcher, today: date) -> FxRate | None:
    """Ensure today's rate is stored. Fail soft: returns None if the feed is unavailable."""
    if (row := db.get(FxRate, today)) is not None:
        return row
    if cfg.manual_rate is not None:
        row = FxRate(date=today, eur_mad=float(cfg.manual_rate), source="manual")
    else:
        try:
            node = json.loads(fetcher.get(f"{cfg.url}#{today.isoformat()}", "api"))  # fragment = per-day cache key
            for key in cfg.json_path:
                node = node[key]
            rate = float(node)
        except (FetchError, ValueError, KeyError, TypeError) as e:
            log.warning("FX refresh failed: %s", e)
            return None
        lo, hi = cfg.sanity_range
        if not lo < rate < hi:  # outside the band = broken feed, not a market move
            log.error("FX feed returned implausible EUR/MAD %s; ignored", rate)
            return None
        row = FxRate(date=today, eur_mad=rate, source=cfg.url)
    db.add(row)
    return row


def rate_for(db: Session, cfg: FxConfig, day: date) -> float | None:
    """Latest rate on or before `day`, if not staler than max_staleness_days."""
    if cfg.manual_rate is not None:
        return float(cfg.manual_rate)
    row = db.exec(select(FxRate).where(FxRate.date <= day).order_by(FxRate.date.desc())).first()
    if row is None or day - row.date > timedelta(days=cfg.max_staleness_days):
        return None
    return row.eur_mad


def to_eur(mad: float | None, rate: float | None) -> float | None:
    return None if mad is None or not rate else round(mad / rate, 2)
