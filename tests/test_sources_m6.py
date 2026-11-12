from radar.sources.avito import AvitoSource, dig, first_of
from radar.sources.sarouty import SaroutySource
from tests.conftest import AFIX, SFIX, FakeFetcher


def test_dig():
    blob = {"a": {"b": [{"c": 1}]}}
    assert dig(blob, ["a", "b", 0, "c"]) == 1 and dig(blob, ["a", "x"]) is None and dig(blob, ["a", "b", 5]) is None
    assert first_of(blob, [["nope"], ["a", "b", 0, "c"]]) == 1


def test_avito_search_and_detail(settings):
    s = AvitoSource(settings.sources["avito"], FakeFetcher({}))
    ads = {a.external_id: a for a in s.parse_search_page((AFIX / "search_p1.synthetic.html").read_text())}
    assert set(ads) == {"55500001", "55500002", "55500003"}          # block without id skipped
    a = ads["55500001"]
    assert a.url == "https://www.avito.ma/fr/ain_diab/appartements/Appartement_vue_mer_55500001.htm"
    assert (a.price_mad, a.surface_m2, a.rooms, a.bedrooms) == (2_600_000, 92.0, 4, 3)
    assert a.location_raw == "Aïn Diab" and a.agency == "Agence Atlantic Immo"
    assert ads["55500002"].photos == ["https://content.avito.ma/a2.jpg"]   # {"url": ...} form
    d = s.parse_detail_page((AFIX / "detail_55500001.synthetic.html").read_text())
    assert d.description.startswith("Magnifique") and (d.lat, d.lon) == (33.5922, -7.6771)
    d2 = s.parse_detail_page((AFIX / "detail_55500002.synthetic.html").read_text())  # alternate path
    assert d2.description.startswith("Grand appartement") and d2.photos == ["https://content.avito.ma/a2.jpg"]
    assert s.parse_detail_page("<html></html>").description is None


def test_sarouty_cards(settings):
    s = SaroutySource(settings.sources["sarouty"], FakeFetcher({}))
    cards = {c.external_id: c for c in s.parse_search_page((SFIX / "search_p1.synthetic.html").read_text())}
    g = cards["6600001"]
    assert g.url == "https://www.sarouty.ma/fr/acheter/casablanca/appartement-standing-gauthier-6600001.html"
    assert (g.price_mad, g.surface_m2, g.rooms, g.location_raw) == (2_080_000, 95.0, 4, "Gauthier, Casablanca")
    assert cards["6600002"].bedrooms == 3
