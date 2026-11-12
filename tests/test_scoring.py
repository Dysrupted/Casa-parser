import copy
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlmodel import Session, select

from radar.market.index import compute_index, drop_outliers, latest_index, quantiles
from radar.models import Listing, Property
from radar.scoring.score import ScoreInput, ZoneStats, interp, score

ZS = ZoneStats(p25=20_000, median=25_000, p75=30_000, count=40, reliable=True)


def base(**kw) -> ScoreInput:
    d = dict(zone="ain_diab", price_mad=2_000_000, surface_m2=90, zone_stats=ZS, dist_corniche_m=400,
             geo_precision="page", features={"sea_view": "full", "parking": True, "elevator": True,
                                             "terrace_m2": 12, "building_year": 2018, "condition": "good",
                                             "concierge": True},
             days_on_market=10, has_photos=True, has_description=True, current_year=2026)
    d.update(kw)
    return ScoreInput(**d)


def test_interp():
    assert interp(5, [10, 20], [1, 0]) == 1 and interp(25, [10, 20], [1, 0]) == 0
    assert interp(15, [10, 20], [1, 0]) == 0.5


def test_quantiles_and_outliers():
    assert quantiles([1, 2, 3, 4, 5]) == (2, 3, 4)
    assert drop_outliers([10, 11, 12, 13, 100], 1.5) == [10, 11, 12, 13]


def test_score_is_explainable_and_bounded(settings):
    r = score(base(), settings.scoring, settings.profile)
    assert 0 <= r.total <= 100 and r.in_profile
    assert {f.name for f in r.factors} == set(settings.scoring["weights"])
    assert abs(sum(f.weight for f in r.factors) - 100) < 0.01
    assert abs(sum(f.points for f in r.factors) - r.penalty - r.total) < 0.11   # breakdown adds up
    assert r.top_reasons()[0].startswith("11% below Ain Diab median")
    d = r.to_dict()
    assert d["factors"][0]["reason"] and len(d["top_reasons"]) == 3


def test_price_below_p25_scores_max_and_above_p75_zero(settings):
    cheap = score(base(price_mad=1_700_000), settings.scoring, settings.profile)   # 18.9k/m2 < p25
    dear = score(base(price_mad=2_900_000), settings.scoring, settings.profile)    # 32.2k/m2 > p75
    f = lambda r: next(x for x in r.factors if x.name == "price_vs_zone")
    assert f(cheap).value == 1.0 and f(dear).value == 0.0
    assert cheap.total - dear.total == pytest.approx(f(cheap).points, abs=0.11)


def test_unreliable_index_is_neutral_not_zero(settings):
    r = score(base(zone_stats=ZoneStats(1, 2, 3, count=2, reliable=False)), settings.scoring, settings.profile)
    f = next(x for x in r.factors if x.name == "price_vs_zone")
    assert f.value is None and f.points == pytest.approx(f.weight * settings.scoring["missing_factor_value"])
    assert "unreliable (n=2)" in f.reason


def test_profile_filters_and_must_have(settings):
    p = copy.deepcopy(settings.profile)
    assert not score(base(surface_m2=120, price_mad=3_000_000), settings.scoring, p).in_profile
    p.must_have = ["parking"]
    r = score(base(features={"parking": False}), settings.scoring, p)
    assert not r.in_profile and "must-have 'parking' missing" in r.profile_notes
    r = score(base(features={}), settings.scoring, p)
    assert r.in_profile and "must-have 'parking' unverified" in r.profile_notes
    p.max_price_mad = 1_500_000
    assert not score(base(), settings.scoring, p).in_profile


def test_data_quality_penalty_and_flags(settings):
    r = score(base(has_photos=False, has_description=False, geo_precision="zone", relist_count=5),
              settings.scoring, settings.profile)
    assert r.penalty == pytest.approx(15 * 3 / 5)
    assert set(r.penalty_reasons) == {"missing photos", "missing description", "missing geo_precise"}
    assert r.flags and "relisted 5×" in r.flags[0]


def test_market_signals_reward_price_drop_and_dom(settings):
    fresh = score(base(), settings.scoring, settings.profile)
    stale = score(base(days_on_market=150, price_drop_pct=0.10), settings.scoring, settings.profile)
    m = lambda r: next(x for x in r.factors if x.name == "market_signals")
    assert m(stale).value == 1.0 and m(fresh).value < 0.1
    assert "price cut 10%" in m(stale).reason


def _add(db, zone, price, surface, seen):
    p = Property(zone=zone, surface_m2=surface, first_seen=seen, last_seen=seen)
    db.add(p)
    db.flush()
    db.add(Listing(property_id=p.id, source="t", external_id=f"{p.id}", url="u", zone=zone,
                   price_mad=price, surface_m2=surface, first_seen=seen, last_seen=seen))


def test_zone_index_window_reliability_trend(settings, engine):
    cfg = settings.scoring["market_index"]
    now = datetime(2026, 11, 1, tzinfo=timezone.utc)
    with Session(engine) as db:
        for i in range(6):
            _add(db, "anfa", 2_000_000 + i * 100_000, 100, now)      # 20k..25k /m2
        _add(db, "anfa", 9_000_000, 100, now)                        # outlier, dropped
        _add(db, "anfa", 1_000_000, 100, now - timedelta(days=200))  # outside window
        _add(db, "racine", 2_000_000, 100, now)
        db.commit()
        old = compute_index(db, now - timedelta(days=31), date(2026, 10, 1), cfg)  # baseline 31 d earlier
        db.commit()
        idx = compute_index(db, now, date(2026, 11, 1), cfg)
        db.commit()
        a = idx["anfa"]
        assert a.count == 6 and a.median_m2 == 22_500 and a.reliable
        assert not idx["racine"].reliable
        assert a.trend_30d is not None and latest_index(db, "anfa").date == date(2026, 11, 1)
        # same-day recompute is idempotent
        compute_index(db, now, date(2026, 11, 1), cfg)
        db.commit()


def test_single_listing_zone_scores_neutral_end_to_end(settings, engine):
    """Regression: a zone with one listing must not give that listing best-in-zone points."""
    from radar.scoring.store import score_all
    cfg = dict(settings.scoring["market_index"], min_points=1)
    now = datetime(2026, 11, 1, tzinfo=timezone.utc)
    with Session(engine) as db:
        _add(db, "anfa", 2_480_000, 92, now)          # 26 956.52 MAD/m2: rounding would break equality
        db.commit()
        compute_index(db, now, now.date(), cfg)
        score_all(db, settings, now, now.date())
        db.commit()
        from radar.models import Score
        sc = db.exec(select(Score)).one()
    f = next(x for x in sc.breakdown["factors"] if x["name"] == "price_vs_zone")
    assert f["value"] == 0.5


def test_degenerate_zone_distribution_is_not_best_in_zone(settings):
    """Regression: with p25 == median == p75 a listing AT the median must not get full points."""
    zs = ZoneStats(p25=25_000, median=25_000, p75=25_000, count=5, reliable=True)
    f = lambda price: next(x for x in score(base(zone_stats=zs, price_mad=price), settings.scoring,
                                            settings.profile).factors if x.name == "price_vs_zone").value
    assert f(2_250_000) == 0.5 and f(2_000_000) == 1.0 and f(2_500_000) == 0.0
