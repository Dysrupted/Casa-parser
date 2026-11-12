from datetime import timedelta

from sqlmodel import Session, select

from radar.http import FetchError
from radar.models import EventType, Listing, ListingEvent, ListingSnapshot, Property, Status
from radar.pipeline.run import Pipeline
from radar.sources.mubawab import MubawabSource
from tests.conftest import SEARCH, FakeFetcher, default_pages


def make(settings, engine, clock, pages=None, notify=None):
    fetcher = FakeFetcher(pages or default_pages())
    src = MubawabSource(settings.sources["mubawab"], fetcher)
    kw = {"notify": notify} if notify else {}
    return Pipeline(settings, engine, [src], now=clock, **kw), fetcher


def counts(engine):
    with Session(engine) as db:
        return {m.__name__: len(db.exec(select(m)).all())
                for m in (Property, Listing, ListingSnapshot, ListingEvent)}


def listing(engine, ext):
    with Session(engine) as db:
        return db.exec(select(Listing).where(Listing.external_id == ext)).one()


def events(engine, ext, type_=None):
    l = listing(engine, ext)
    with Session(engine) as db:
        q = select(ListingEvent).where(ListingEvent.listing_id == l.id)
        if type_:
            q = q.where(ListingEvent.type == type_)
        return db.exec(q).all()


def test_first_run_stores_in_zone_listings(settings, engine, clock):
    p, _ = make(settings, engine, clock)
    run = p.run()
    assert run.status == "ok"
    st = run.stats["mubawab"]
    assert st["cards"] == 5 and st["in_zone"] == 4 and st["new"] == 4  # Maârif filtered out
    assert counts(engine) == {"Property": 4, "Listing": 4, "ListingSnapshot": 4, "ListingEvent": 4}

    a = listing(engine, "7712345")
    assert a.zone == "ain_diab" and a.price_mad == 2_650_000
    assert a.description and "ascenseur" in a.description
    assert (settings.data_dir / a.raw_html_path).exists()
    assert len(a.photos) == 3


def test_same_day_rerun_is_idempotent(settings, engine, clock):
    p, fetcher = make(settings, engine, clock)
    p.run()
    before = counts(engine)
    clock.t += timedelta(hours=3)
    p.run()
    p.run()
    assert counts(engine) == before
    assert listing(engine, "7712345").missing_runs == 0
    # details are not re-fetched within refresh window
    detail_calls = [u for u, k in fetcher.calls if k == "detail"]
    assert len(detail_calls) == 4


def test_price_drop_creates_event_and_history(settings, engine, clock):
    p, _ = make(settings, engine, clock)
    p.run()
    pages = default_pages()
    pages[SEARCH] = pages[SEARCH].replace("2 650 000 DH", "2 450 000 DH")
    clock.t += timedelta(days=1)
    make(settings, engine, clock, pages)[0].run()

    ev = events(engine, "7712345", EventType.price_change)
    assert [(e.old_value, e.new_value) for e in ev] == [(2_650_000, 2_450_000)]
    with Session(engine) as db:
        l = listing(engine, "7712345")
        snaps = db.exec(select(ListingSnapshot).where(ListingSnapshot.listing_id == l.id)
                        .order_by(ListingSnapshot.run_date)).all()
    assert [s.price_mad for s in snaps] == [2_650_000, 2_450_000]


def test_removal_after_n_complete_crawls_then_relist(settings, engine, clock):
    p, _ = make(settings, engine, clock)
    p.run()
    gone = default_pages()
    gone[f"{SEARCH}:p:2"] = gone[f"{SEARCH}:p:3"]  # gauthier listing disappears

    clock.t += timedelta(days=1)
    make(settings, engine, clock, gone)[0].run()
    make(settings, engine, clock, gone)[0].run()   # same-day rerun must not count twice
    g = listing(engine, "7712500")
    assert g.status == Status.missing and g.missing_runs == 1

    clock.t += timedelta(days=1)
    run = make(settings, engine, clock, gone)[0].run()
    assert run.stats["mubawab"]["removed"] == 1
    g = listing(engine, "7712500")
    assert g.status == Status.removed
    with Session(engine) as db:
        assert db.get(Property, g.property_id).status == Status.removed

    clock.t += timedelta(days=1)
    run = make(settings, engine, clock)[0].run()
    g = listing(engine, "7712500")
    assert g.status == Status.active and g.relist_count == 1 and g.missing_runs == 0
    assert run.stats["mubawab"]["relisted"] == 1
    assert len(events(engine, "7712500", EventType.relisted)) == 1


def test_incomplete_crawl_never_marks_removals(settings, engine, clock):
    p, _ = make(settings, engine, clock)
    p.run()
    broken = default_pages()
    broken[f"{SEARCH}:p:2"] = FetchError("HTTP 503")
    msgs = []
    for _ in range(3):
        clock.t += timedelta(days=1)
        run = make(settings, engine, clock, broken, notify=msgs.append)[0].run()
        assert run.status == "partial"
    g = listing(engine, "7712500")
    assert g.status == Status.active and g.missing_runs == 0
    assert any("incomplete" in m for m in msgs)


def test_max_pages_reached_is_incomplete(settings, engine, clock):
    settings.sources["mubawab"].max_pages = 1
    run = make(settings, engine, clock)[0].run()
    assert run.status == "partial"
    assert any("max_pages" in e for e in run.stats["mubawab"]["errors"])


def test_failing_source_does_not_kill_run(settings, engine, clock):
    class Boom(MubawabSource):
        name = "boom"

        def crawl(self):
            raise RuntimeError("site redesigned")

    good, _ = make(settings, engine, clock)
    bad = Boom(settings.sources["mubawab"], FakeFetcher({}))
    msgs = []
    p = Pipeline(settings, engine, [bad, *good.sources], now=clock, notify=msgs.append)
    run = p.run()
    assert run.status == "partial"
    assert run.stats["boom"]["status"] == "failed"
    assert run.stats["mubawab"]["new"] == 4
    assert any("boom" in m for m in msgs)


def test_detail_failure_keeps_card_data(settings, engine, clock):
    pages = default_pages()
    pages["https://www.mubawab.ma/fr/a/7712345/appartement-vue-mer-ain-diab"] = FetchError("timeout")
    run = make(settings, engine, clock, pages)[0].run()
    a = listing(engine, "7712345")
    assert a.price_mad == 2_650_000 and a.raw_html_path is None and a.detail_fetched_at is None
    assert run.stats["mubawab"]["details_fetched"] == 3


def test_unreachable_source_is_failed(settings, engine, clock):
    run = make(settings, engine, clock, {SEARCH: FetchError("blocked")})[0].run()
    assert run.status == "failed" and run.stats["mubawab"]["status"] == "failed"
    assert "FetchError: blocked" in run.stats["mubawab"]["errors"][0]


def test_pipeline_with_enrichment(settings, engine, clock):
    import json as _json

    from radar.pipeline.enrich import Enricher
    from tests.test_extract import FakeProvider, payload

    pages = default_pages()
    pages[f"{settings.fx.url}#2026-11-01"] = _json.dumps({"rates": {"MAD": 10.9}})
    fetcher = FakeFetcher(pages)
    prov = FakeProvider(payload(elevator={"value": True, "confidence": 0.95, "source_span": "avec ascenseur"}))
    enr = Enricher(settings, fetcher, prov)
    src = MubawabSource(settings.sources["mubawab"], fetcher)
    run = Pipeline(settings, engine, [src], now=clock, enricher=enr).run()
    e = run.stats["enrich"]
    assert e["fx"] == 10.9 and e["llm"] == 1 and e["errors"] == []    # 1 listing has a description
    assert e["geo"] == {"page": 1, "address": 0, "zone": 3, "none": 0}
    a = listing(engine, "7712345")
    assert a.address_raw == "Rue des Dunes, Aïn Diab, Casablanca"
    with Session(engine) as db:
        p = db.get(Property, a.property_id)
        assert (p.lat, p.lon, p.geo_precision) == (33.5921, -7.6772, "page")
        assert p.dist_corniche_m < 1500

    # Same-day rerun: no new LLM calls (description unchanged)
    Pipeline(settings, engine, [src], now=clock, enricher=enr).run()
    assert prov.calls == 1


def test_llm_unavailable_degrades_to_rules(settings, engine, clock):
    from radar.llm.provider import LLMUnavailable
    from radar.pipeline.enrich import Enricher
    from tests.test_extract import FakeProvider

    fetcher = FakeFetcher(default_pages())
    msgs = []
    enr = Enricher(settings, fetcher, FakeProvider(exc=LLMUnavailable("no key")), notify=msgs.append)
    run = Pipeline(settings, engine, [MubawabSource(settings.sources["mubawab"], fetcher)],
                   now=clock, enricher=enr, notify=msgs.append).run()
    e = run.stats["enrich"]
    assert e["llm"] == 0 and e["llm_pending"] == 1 and e["rules"] == 1
    assert any("LLM unavailable" in m for m in msgs)
    assert listing(engine, "7712345").extracted_hash is None   # will retry next run


def test_pipeline_simulates_priced_listings(settings, engine, clock):
    from radar.models import Simulation
    from radar.pipeline.enrich import Enricher
    from radar.simulation import FinancingParams
    from tests.test_simulation import SYNTH

    fetcher = FakeFetcher(default_pages())
    enr = Enricher(settings, fetcher, None)
    src = MubawabSource(settings.sources["mubawab"], fetcher)
    # shipped config: rates missing -> skipped, never guessed
    run = Pipeline(settings, engine, [src], now=clock, enricher=enr).run()
    assert run.stats["enrich"]["simulation"]["status"] == "skipped"

    settings.financing = FinancingParams(**SYNTH)
    run = Pipeline(settings, engine, [src], now=clock, enricher=enr).run()
    assert run.stats["enrich"]["simulation"]["simulated"] == 3      # 'Prix à consulter' excluded
    with Session(engine) as db:
        sims = {s.price_mad: s for s in db.exec(select(Simulation))}
    a = sims[2_650_000].outputs
    assert a["ok"] and "agency_fee" in a["cost_breakdown_mad"]       # agency listing
    assert "agency_fee" not in sims[1_980_000].outputs["cost_breakdown_mad"]
