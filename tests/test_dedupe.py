from datetime import timedelta

from sqlmodel import Session, select

from radar.dedupe.merge import merge_properties
from radar.dedupe.similarity import description_similarity, min_hamming, phash_hex, within
from radar.dedupe.stage import Deduper
from radar.models import (AlertLog, DedupePair, Listing, Property, PropertyFeature, PropertyMerge, Score,
                          UserNote)
from radar.pipeline.analyze import Analyzer
from radar.pipeline.enrich import Enricher
from radar.pipeline.run import Pipeline, build_sources
from tests.conftest import FakeFetcher, image_bytes, multi_pages


def test_similarity_primitives():
    a = "Magnifique appartement au 5ème étage avec ascenseur, vue mer partielle, parking."
    assert description_similarity(a, a + " Visite sur rendez-vous.") > 0.6
    assert description_similarity(a, "Studio meublé au centre ville proche tramway et gare.") < 0.1
    assert description_similarity(None, a) == 0.0
    same = min_hamming([phash_hex(image_bytes(1))], [phash_hex(image_bytes(1, size=(640, 480), fmt="PNG"))])
    diff = min_hamming([phash_hex(image_bytes(1))], [phash_hex(image_bytes(2))])
    assert same <= 4 < 16 <= diff
    assert phash_hex(b"not an image") is None and min_hamming([], ["0" * 16]) is None
    assert within(100, 104, 0.05) and not within(100, 106, 0.05)


def run_all(settings, engine, clock, pages=None):
    settings.scoring["market_index"]["min_points"] = 1
    fetcher = FakeFetcher(pages or multi_pages())
    stages = [Enricher(settings, fetcher, None), Deduper(settings, fetcher), Analyzer(settings)]
    return Pipeline(settings, engine, build_sources(settings, fetcher), now=clock, stages=stages).run(), fetcher


def by_ext(db, ext):
    return db.exec(select(Listing).where(Listing.external_id == ext)).one()


def test_cross_site_duplicates_merged(multi_settings, engine, clock):
    run, fetcher = run_all(multi_settings, engine, clock)
    st = run.stats["dedupe"]
    assert st["merged"] == 2, st
    with Session(engine) as db:
        mub, avi = by_ext(db, "7712345"), by_ext(db, "55500001")
        assert mub.property_id == avi.property_id                      # by description
        g1, g2 = by_ext(db, "7712500"), by_ext(db, "6600001")
        assert g1.property_id == g2.property_id                        # by photo hash
        assert by_ext(db, "7712399").property_id != by_ext(db, "55500002").property_id
        reasons = [m.reason for m in db.exec(select(PropertyMerge))]
        assert any("desc_sim=" in r and "photo_dist=None" in r for r in reasons)
        assert any("photo_dist=0" in r or "photo_dist=1" in r or "photo_dist=2" in r for r in reasons)
        # one score per physical flat
        assert len(db.exec(select(Score)).all()) == len(db.exec(select(Property)).all())
        p = db.get(Property, mub.property_id)
        assert p.first_seen == min(mub.first_seen, avi.first_seen)
    # photos only fetched for candidate pairs (gauthier), never for the description-confirmed pair
    photo_calls = {u for u, k in fetcher.calls if k == "photo"}
    assert photo_calls == {"https://www.mubawab.ma/photos/7712500_1.jpg", "https://www.sarouty.ma/img/s1.jpg"}


def test_rerun_is_stable_and_distinct_pairs_not_reevaluated(multi_settings, engine, clock):
    pages = multi_pages()
    pages["https://www.sarouty.ma/img/s1.jpg"] = image_bytes(99)       # different photo -> distinct
    run_all(multi_settings, engine, clock, pages)
    with Session(engine) as db:
        assert db.exec(select(DedupePair).where(DedupePair.decision == "distinct")).first()
        n_props = len(db.exec(select(Property)).all())
    clock.t += timedelta(days=1)
    run, fetcher = run_all(multi_settings, engine, clock, pages)
    assert run.stats["dedupe"]["evaluated"] == 0 and run.stats["dedupe"]["merged"] == 0
    with Session(engine) as db:
        assert len(db.exec(select(Property)).all()) == n_props


def test_merge_moves_everything(settings, engine, clock):
    t0 = clock()
    with Session(engine) as db:
        a = Property(zone="anfa", first_seen=t0 + timedelta(days=2), last_seen=t0, geo_precision="zone")
        b = Property(zone="anfa", first_seen=t0, last_seen=t0, lat=33.59, lon=-7.65, geo_precision="page")
        db.add_all([a, b])
        db.flush()
        la = Listing(property_id=a.id, source="x", external_id="1", url="u1", first_seen=t0, last_seen=t0)
        lb = Listing(property_id=b.id, source="y", external_id="2", url="u2", first_seen=t0, last_seen=t0)
        db.add_all([la, lb])
        db.flush()
        db.add_all([
            PropertyFeature(property_id=a.id, source_listing_id=la.id, field="parking", value=True,
                            confidence=0.9, extracted_by="rules", extracted_at=t0),
            PropertyFeature(property_id=b.id, source_listing_id=lb.id, field="parking", value=None,
                            confidence=0.0, extracted_by="rules", extracted_at=t0),
            PropertyFeature(property_id=a.id, source_listing_id=la.id, field="elevator", value=True,
                            confidence=0.9, extracted_by="rules", extracted_at=t0),
            UserNote(property_id=a.id, note="called agent", updated_at=t0),
            Score(property_id=a.id, date=t0.date(), computed_at=t0, total=50, in_profile=True),
            AlertLog(key="new_high_score:%d" % a.id, type="new_high_score", property_id=a.id, created_at=t0),
        ])
        db.commit()
        aid, bid = a.id, b.id
        keep = merge_properties(db, aid, bid, "manual", t0)
        db.commit()
        assert keep == bid                                             # earliest first_seen survives
        assert db.get(Property, aid) is None
        assert {l.property_id for l in db.exec(select(Listing))} == {bid}
        feats = {f.field: f for f in db.exec(select(PropertyFeature))}
        assert feats["parking"].value is True and feats["elevator"].property_id == bid
        assert len(db.exec(select(PropertyFeature)).all()) == 2
        assert db.get(UserNote, bid).note == "called agent"
        assert db.exec(select(Score)).all() == []                      # derived rows recomputed later
        assert db.exec(select(AlertLog)).one().property_id == bid       # won't re-alert
        p = db.get(Property, bid)
        assert p.geo_precision == "page" and p.first_seen == t0
