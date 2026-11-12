"""Alert selection, idempotent queue, quiet hours, sending (PRD F7)."""
from __future__ import annotations

import logging
from datetime import date, datetime
from zoneinfo import ZoneInfo

from sqlmodel import Session, select

from radar.alerts.telegram import TelegramClient, TelegramError, format_alert, format_digest
from radar.config import Settings
from radar.enrich.fx import rate_for, to_eur
from radar.models import (AlertLog, EventType, Listing, ListingEvent, NoteStatus, Property, Score,
                          Simulation, Status, UserNote)
from radar.scoring.store import best_listing, latest_score

log = logging.getLogger(__name__)


def listing_payload(db: Session, s: Settings, prop: Property, l: Listing, today: date) -> dict:
    sc = latest_score(db, prop.id)
    b = sc.breakdown if sc else {}
    sim = db.exec(select(Simulation).where(Simulation.property_id == prop.id, Simulation.ok)
                  .order_by(Simulation.computed_at.desc())).first()
    rate = rate_for(db, s.fx, today)
    return {
        "property_id": prop.id, "title": l.title, "url": l.url, "zone": prop.zone,
        "zone_label": (prop.zone or "").replace("_", " ").title(), "price_mad": l.price_mad,
        "price_eur": to_eur(l.price_mad, rate), "surface_m2": l.surface_m2,
        "price_m2": round(l.price_mad / l.surface_m2) if l.price_mad and l.surface_m2 else None,
        "price_vs_zone": b.get("price_vs_zone"), "score": sc.total if sc else None,
        "reasons": b.get("top_reasons", []), "warnings": b.get("flags", []) + b.get("profile_notes", [])[:1],
        "photo": (l.photos or [None])[0],
        "cash_needed_mad": sim.outputs.get("total_cash_needed_mad") if sim else None,
        "monthly_payment_mad": sim.outputs.get("monthly_payment_mad") if sim else None,
    }


class AlertStage:
    name = "alerts"

    def __init__(self, settings: Settings, client: TelegramClient | None = None):
        self.s, self.cfg = settings, settings.alerts
        if client is None and settings.telegram_token and settings.telegram_chat_id:
            client = TelegramClient(settings.telegram_token, settings.telegram_chat_id)
        self.client = client
        self.errors: list[str] = []
        self.tz = ZoneInfo(settings.pipeline.timezone)

    def buffer_error(self, msg: str) -> None:
        """Pipeline `notify` hook: collect fail-soft messages, sent as one alert."""
        log.error(msg)
        self.errors.append(msg)

    # -- queueing ----------------------------------------------------------------
    def _queue(self, db: Session, key: str, type_: str, payload: dict, now: datetime,
               property_id: int | None = None) -> bool:
        if db.exec(select(AlertLog).where(AlertLog.key == key)).first():
            return False
        db.add(AlertLog(key=key, type=type_, property_id=property_id, payload={"type": type_, **payload},
                        created_at=now))
        db.flush()
        return True

    def collect(self, db: Session, now: datetime, today: date) -> dict:
        c, t = self.cfg, self.cfg.triggers
        q = {"new_high_score": 0, "price_drop": 0, "watch_removed": 0, "run_errors": 0}
        watched = {n.property_id for n in db.exec(select(UserNote).where(UserNote.status != NoteStatus.rejected))}
        rejected = {n.property_id for n in db.exec(select(UserNote).where(UserNote.status == NoteStatus.rejected))}

        if t.new_high_score:
            for sc in db.exec(select(Score).where(Score.date == today, Score.in_profile,
                                                  Score.total >= c.score_threshold)):
                prop = db.get(Property, sc.property_id)
                l = best_listing(db, prop.id)
                if prop.status == Status.removed or l is None or prop.id in rejected:
                    continue
                if self._queue(db, f"new_high_score:{prop.id}", "new_high_score",
                               listing_payload(db, self.s, prop, l, today), now, prop.id):
                    q["new_high_score"] += 1

        for ev in db.exec(select(ListingEvent).where(ListingEvent.run_date == today)):
            l = db.get(Listing, ev.listing_id)
            prop = db.get(Property, l.property_id)
            if ev.type == EventType.price_change and t.price_drop and ev.old_value and ev.new_value:
                drop = (ev.old_value - ev.new_value) / ev.old_value
                sc = latest_score(db, prop.id)
                high = sc is not None and sc.in_profile and sc.total >= c.score_threshold
                if drop >= c.price_drop_pct and (prop.id in watched or high) and prop.id not in rejected:
                    p = listing_payload(db, self.s, prop, l, today) | {"old_price_mad": ev.old_value}
                    if self._queue(db, f"price_drop:{prop.id}:{today}", "price_drop", p, now, prop.id):
                        q["price_drop"] += 1
            elif ev.type == EventType.removed and t.watch_removed and prop.id in watched:
                if self._queue(db, f"watch_removed:{prop.id}:{today}", "watch_removed",
                               listing_payload(db, self.s, prop, l, today), now, prop.id):
                    q["watch_removed"] += 1

        if t.run_errors and self.errors:
            if self._queue(db, f"run_errors:{now.isoformat()}", "run_errors", {"messages": self.errors[:10]}, now):
                q["run_errors"] += 1
            self.errors = []
        return q

    # -- sending -----------------------------------------------------------------
    def in_quiet_hours(self, now: datetime) -> bool:
        qh = self.cfg.quiet_hours
        return bool(qh) and qh.contains(now.astimezone(self.tz).strftime("%H:%M"))

    def flush(self, db: Session, now: datetime) -> dict:
        pending = db.exec(select(AlertLog).where(AlertLog.status == "pending",
                                                 AlertLog.attempts < self.cfg.max_send_attempts)
                          .order_by(AlertLog.created_at, AlertLog.id)).all()
        st = {"pending": len(pending), "sent": 0, "failed": 0, "held": None}
        if not pending:
            return st
        if self.client is None:
            st["held"] = "no TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID"
            for a in pending:
                log.info("[dry-run alert] %s", format_alert(a.payload))
            return st
        if self.in_quiet_hours(now):
            st["held"] = "quiet hours"
            return st
        if self.cfg.mode == "digest":
            singles, digest = [], pending
        else:
            cut = self.cfg.max_alerts_per_run
            singles, digest = (pending, []) if len(pending) <= cut else (pending[:cut - 1], pending[cut - 1:])
        for a in singles:
            self._send(db, [a], now, st, lambda: self.client.send(format_alert(a.payload), a.payload.get("photo")))
        if digest:
            self._send(db, digest, now, st, lambda: self.client.send(format_digest([a.payload for a in digest])))
        return st

    def _send(self, db, alerts: list[AlertLog], now, st, fn) -> None:
        for a in alerts:
            a.attempts += 1
        try:
            fn()
        except TelegramError as e:
            log.error("telegram send failed: %s", e)
            for a in alerts:
                a.error = str(e)
                a.status = "failed" if a.attempts >= self.cfg.max_send_attempts else "pending"
            st["failed"] += len(alerts)
        else:
            for a in alerts:
                a.status, a.sent_at, a.error = "sent", now, None
            st["sent"] += len(alerts)
        db.add_all(alerts)

    def run(self, db: Session, now: datetime, today: date) -> dict:
        if not self.cfg.enabled:
            return {"status": "disabled", "errors": []}
        queued = self.collect(db, now, today)
        db.commit()
        sent = self.flush(db, now)
        return {"queued": queued, **sent, "errors": []}
