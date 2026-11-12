"""Parsers against pages RECORDED from the live sites (2026-11-12), not synthetic fixtures."""
from pathlib import Path

from radar.robots import Robots
from radar.sources.avito import AvitoSource
from radar.sources.mubawab import MubawabSource
from tests.conftest import FakeFetcher

REAL = Path(__file__).parent / "fixtures" / "real"


def test_mubawab_real_search_page(settings):
    src = MubawabSource(settings.sources["mubawab"], FakeFetcher({}))
    cards = src.parse_search_page((REAL / "mubawab_ain-diab.html").read_text())
    assert len(cards) >= 30
    assert all(settings.zones.resolve(c.location_raw) == "ain_diab" for c in cards)
    priced = [c for c in cards if c.price_mad]
    assert len(priced) >= len(cards) - 2 and all(500_000 < c.price_mad < 50_000_000 for c in priced)
    assert all(c.surface_m2 and 20 < c.surface_m2 < 1000 for c in cards)
    assert all(c.photos and c.photos[0].startswith("https://") for c in cards)
    assert len({c.external_id for c in cards}) == len(cards)


def test_mubawab_real_detail_page(settings):
    src = MubawabSource(settings.sources["mubawab"], FakeFetcher({}))
    d = src.parse_detail_page((REAL / "mubawab_detail.html").read_text())
    assert d.description and len(d.description) > 80
    assert d.lat and d.lon and settings.geo.bbox.contains((d.lat, d.lon))
    assert d.surface_m2 == 116.0 and d.bedrooms == 3 and d.address_raw


def test_avito_real_search_page(settings):
    src = AvitoSource(settings.sources["avito"], FakeFetcher({}))
    ads = src.parse_search_page((REAL / "avito_casablanca.html").read_text())
    assert len(ads) >= 35
    assert all(a.url.startswith("https://www.avito.ma/") and a.url.endswith(".htm") for a in ads)
    with_surface = [a for a in ads if a.surface_m2]
    assert len(with_surface) >= 0.7 * len(ads)   # real data: ~79% of Avito ads state a surface
    assert sum(1 for a in ads if a.description) >= 0.8 * len(ads)
    assert any(settings.zones.resolve(a.location_raw) is not None or "Casablanca" in (a.location_raw or "")
               for a in ads)


def test_mubawab_robots_blocks_pagination():
    r = Robots("User-agent: *\nDisallow: /*:\n")
    ua = "CasaRadar/0.1"
    assert not r.can_fetch(ua, "https://www.mubawab.ma/fr/sd/casablanca/anfa/appartements-a-vendre:p:2")
