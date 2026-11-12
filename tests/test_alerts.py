from datetime import timedelta

import httpx
import pytest
import respx
from sqlmodel import Session, select

from radar.alerts.engine import AlertStage
from radar.alerts.telegram import API, TelegramClient, TelegramError, format_alert, format_digest
from radar.config import QuietHours
from radar.models import AlertLog, Listing, NoteStatus, UserNote
from radar.pipeline.analyze import Analyzer
from radar.pipeline.enrich import Enricher
from radar.pipeline.run import Pipeline
from radar.sources.mubawab import MubawabSource
from tests.conftest import SEARCH, FakeFetcher, default_pages


class FakeTelegram:
    def __init__(self, fail=False):
        self.sent, self.fail = [], fail

    def send(self, text, photo=None):
        if self.fail:
            raise TelegramError("boom")
        self.sent.append((text, photo))


def test_quiet_hours():
    q = QuietHours(start="22:00", end="08:00")
    assert q.contains("23:30") and q.contains("07:59") and not q.contains("08:00") and not q.contains("12:00")
    assert QuietHours(start="13:00", end="14:00").contains("13:30")


def test_format_alert_and_digest():
    a = {"type": "price_drop", "zone_label": "Ain Diab", "score": 81.2, "title": "Vue mer <b>top</b>",
         "price_mad": 2_450_000, "old_price_mad": 2_650_000, "price_eur": 225_000, "surface_m2": 92,
         "price_m2": 26_630, "price_vs_zone": 0.88, "reasons": ["12% below Ain Diab median", "400 m from the Corniche",
         "sea view, parking", "ignored 4th"], "cash_needed_mad": 600_000, "monthly_payment_mad": 12_000,
         "url": "https://x/a/1"}
    t = format_alert(a)
    assert "📉 Price drop" in t and "<s>2 650 000 MAD</s> → <b>2 450 000 MAD</b> (−8%)" in t
    assert "&lt;b&gt;top&lt;/b&gt;" in t                      # listing text is escaped
    assert "0.88× zone median" in t and "ignored 4th" not in t
    assert "Cash needed 600 000 MAD" in t and 'href="https://x/a/1"' in t
    d = format_digest([a, {"type": "run_errors", "messages": ["x"]}])
    assert "2 alerts" in d and "⚠️ 1 run issue" in d


@respx.mock
def test_telegram_client_photo_fallback_and_errors():
    base = f"{API}/botTOKEN"
    respx.post(f"{base}/sendPhoto").respond(200, json={"ok": False, "description": "wrong file"})
    msg = respx.post(f"{base}/sendMessage").respond(200, json={"ok": True})
    TelegramClient("TOKEN", "42").send("hi", photo="https://x/p.jpg")
    assert msg.called and b'"chat_id":"42"' in msg.calls[0].request.content.replace(b" ", b"")
    respx.post(f"{base}/sendMessage").respond(401, json={"ok": False, "description": "Unauthorized"})
    with pytest.raises(TelegramError) as e:
        TelegramClient("TOKEN", "42").send("hi")
    assert "TOKEN" not in str(e.value)


def setup(settings, engine, clock, pages=None, tg=None, threshold=0):
    settings.alerts.score_threshold = threshold
    settings.alerts.quiet_hours = None
    settings.scoring["market_index"]["min_points"] = 1
    fetcher = FakeFetcher(pages or default_pages())
    alerts = AlertStage(settings, client=tg if tg is not None else FakeTelegram())
    stages = [Enricher(settings, fetcher, None), Analyzer(settings), alerts]
    src = MubawabSource(settings.sources["mubawab"], fetcher)
    return Pipeline(settings, engine, [src], now=clock, stages=stages, notify=alerts.buffer_error), alerts


def logs(engine, type_=None):
    with Session(engine) as db:
        q = select(AlertLog)
        if type_:
            q = q.where(AlertLog.type == type_)
        return db.exec(q).all()


def test_new_high_score_alert_once(settings, engine, clock):
    p, alerts = setup(settings, engine, clock)
    run = p.run()
    assert run.stats["alerts"]["queued"]["new_high_score"] == 4       # all 4 fixtures are 80-100 m²
    assert run.stats["alerts"]["sent"] == 4
    text, photo = alerts.client.sent[0]
    assert "New match" in text and "Open listing" in text
    p.run()                                                          # same-day rerun
    clock.t += timedelta(days=1)
    p.run()                                                          # next day, unchanged
    assert len(alerts.client.sent) == 4 and len(logs(engine, "new_high_score")) == 4


def test_threshold_filters(settings, engine, clock):
    p, alerts = setup(settings, engine, clock, threshold=101)
    assert p.run().stats["alerts"]["queued"]["new_high_score"] == 0


def test_price_drop_on_watched_listing_and_removal(settings, engine, clock):
    p, alerts = setup(settings, engine, clock, threshold=101)        # no score alerts: isolate drops
    p.run()
    with Session(engine) as db:
        l = db.exec(select(Listing).where(Listing.external_id == "7712500")).one()
        db.add(UserNote(property_id=l.property_id, status=NoteStatus.watching, updated_at=clock()))
        db.commit()
    pages = default_pages()
    pages[f"{SEARCH}:p:2"] = pages[f"{SEARCH}:p:2"].replace("2 100 000 DH", "1 900 000 DH")   # -9.5 %
    clock.t += timedelta(days=1)
    p2, a2 = setup(settings, engine, clock, pages, threshold=101)
    run = p2.run()
    assert run.stats["alerts"]["queued"]["price_drop"] == 1
    assert "−10%" in a2.client.sent[0][0]
    # watched listing disappears for 2 complete crawls -> removed alert
    gone = default_pages()
    gone[f"{SEARCH}:p:2"] = gone[f"{SEARCH}:p:3"]
    for _ in range(2):
        clock.t += timedelta(days=1)
        p3, a3 = setup(settings, engine, clock, gone, threshold=101)
        p3.run()
    assert len(logs(engine, "watch_removed")) == 1


def test_rejected_listing_is_silenced(settings, engine, clock):
    p, _ = setup(settings, engine, clock, tg=FakeTelegram(), threshold=101)
    p.run()
    with Session(engine) as db:
        for l in db.exec(select(Listing)):
            db.add(UserNote(property_id=l.property_id, status=NoteStatus.rejected, reject_reason="ground floor",
                            updated_at=clock()))
        db.commit()
    p2, a2 = setup(settings, engine, clock, threshold=0)
    assert p2.run().stats["alerts"]["queued"]["new_high_score"] == 0


def test_quiet_hours_hold_then_release(settings, engine, clock):
    p, alerts = setup(settings, engine, clock)
    settings.alerts.quiet_hours = QuietHours(start="00:00", end="23:59")   # clock is 08:00 local
    run = p.run()
    assert run.stats["alerts"]["held"] == "quiet hours" and alerts.client.sent == []
    settings.alerts.quiet_hours = None
    run = p.run()
    assert run.stats["alerts"]["sent"] == 4


def test_no_credentials_is_dry_run(settings, engine, clock):
    settings.telegram_token = None
    p, alerts = setup(settings, engine, clock, tg=None)
    alerts.client = None
    run = p.run()
    assert run.stats["alerts"]["held"].startswith("no TELEGRAM") and all(a.status == "pending" for a in logs(engine))


def test_send_failures_retry_then_give_up(settings, engine, clock):
    p, alerts = setup(settings, engine, clock, tg=FakeTelegram(fail=True))
    for _ in range(4):
        p.run()
    assert {a.status for a in logs(engine, "new_high_score")} == {"failed"}
    assert all(a.attempts == settings.alerts.max_send_attempts for a in logs(engine, "new_high_score"))


def test_overflow_goes_to_digest_and_errors_are_aggregated(settings, engine, clock):
    settings.alerts.max_alerts_per_run = 1
    pages = default_pages()
    pages["https://www.mubawab.ma/fr/a/7712400/appartement-racine"] = Exception("boom")  # parse-level failure
    p, alerts = setup(settings, engine, clock, pages)
    run = p.run()
    sent = alerts.client.sent
    assert len(sent) == 1 and "digest · 5 alerts" in sent[0][0]    # 4 matches + 1 error alert -> 1 digest
    assert len(logs(engine, "run_errors")) == 1
