import json
from datetime import date

from sqlmodel import Session

from radar.enrich.fx import rate_for, refresh_rate, to_eur
from radar.enrich.geo import Geocoder, dist_to_polyline_m, haversine_m
from radar.http import FetchError
from radar.models import FxRate
from tests.conftest import FakeFetcher


def test_haversine_and_polyline():
    assert abs(haversine_m((33.6, -7.65), (33.6, -7.65)) ) < 1e-6
    d = haversine_m((33.59, -7.65), (33.60, -7.65))
    assert 1100 < d < 1120                                   # ~1.11 km per 0.01° lat
    line = [(33.60, -7.70), (33.60, -7.60)]
    assert abs(dist_to_polyline_m((33.59, -7.65), line) - d) < 5   # perpendicular foot
    assert dist_to_polyline_m((33.60, -7.65), line) < 1             # on the line


def test_locate_precision_order(settings):
    url_part = settings.geo.nominatim.url
    resp = json.dumps([{"lat": "48.85", "lon": "2.35"},              # Paris: outside bbox, skipped
                       {"lat": "33.5930", "lon": "-7.6760"}])
    g = Geocoder(settings.geo, FakeFetcher({}))
    g.fetcher.get = lambda url, kind: resp if url.startswith(url_part) and kind == "api" else (_ for _ in ()).throw(FetchError())
    assert g.locate((33.59, -7.67), "Rue X", "anfa") == ((33.59, -7.67), "page")
    assert g.locate(None, "Rue des Dunes", "ain_diab") == ((33.5930, -7.6760), "address")
    assert g.locate((0.0, 0.0), None, "anfa") == (tuple(settings.geo.zone_centroids["anfa"]), "zone")
    assert g.locate(None, None, None) == (None, None)
    # Ain Diab centroid is near the sea, Gauthier is downtown
    ain = g.corniche_distance_m(tuple(settings.geo.zone_centroids["ain_diab"]))
    gau = g.corniche_distance_m(tuple(settings.geo.zone_centroids["gauthier"]))
    assert ain < 1500 < gau


def test_geocode_failure_falls_back_to_zone(settings):
    g = Geocoder(settings.geo, FakeFetcher({}))   # every fetch raises FetchError
    assert g.locate(None, "Rue X", "racine")[1] == "zone"


def test_fx_refresh_staleness_and_sanity(settings, engine):
    day = date(2026, 11, 1)
    with Session(engine) as db:
        f = FakeFetcher({f"{settings.fx.url}#{day}": json.dumps({"rates": {"MAD": 10.85}})})
        assert refresh_rate(db, settings.fx, f, day).eur_mad == 10.85
        db.commit()
        assert rate_for(db, settings.fx, date(2026, 11, 5)) == 10.85
        assert rate_for(db, settings.fx, date(2026, 11, 30)) is None   # too stale
        bad = FakeFetcher({f"{settings.fx.url}#2026-11-02": json.dumps({"rates": {"MAD": 0.09}})})
        assert refresh_rate(db, settings.fx, bad, date(2026, 11, 2)) is None
        assert refresh_rate(db, settings.fx, FakeFetcher({}), date(2026, 11, 3)) is None
        settings.fx.manual_rate = 11.0
        assert rate_for(db, settings.fx, date(2026, 12, 31)) == 11.0
    assert to_eur(2_170_000, 10.85) == 200_000.0
    assert to_eur(None, 10.85) is None and to_eur(1, None) is None


def test_migrations_upgrade_downgrade(tmp_path):
    from alembic import command

    from radar.db import alembic_config
    cfg = alembic_config(f"sqlite:///{tmp_path / 'm.db'}")
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
