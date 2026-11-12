from radar.sources.base import parse_counts, parse_price_mad, parse_surface
from radar.sources.mubawab import MubawabSource
from tests.conftest import FakeFetcher, fixture


def src(settings):
    return MubawabSource(settings.sources["mubawab"], FakeFetcher({}))


def test_search_page_cards(settings):
    cards = src(settings).parse_search_page(fixture("search_p1.synthetic.html"))
    by_id = {c.external_id: c for c in cards}
    assert set(by_id) == {"7712345", "7712399", "7712400", "7712411"}  # promo block skipped

    a = by_id["7712345"]
    assert a.url == "https://www.mubawab.ma/fr/a/7712345/appartement-vue-mer-ain-diab"
    assert (a.price_mad, a.surface_m2, a.rooms, a.bedrooms, a.bathrooms) == (2_650_000, 92.0, 4, 3, 2)
    assert a.location_raw == "Aïn Diab à Casablanca"
    assert a.agency == "Agence Atlantic Immo"
    assert a.photos == ["https://www.mubawab.ma/photos/7712345_1.jpg"]

    b = by_id["7712399"]
    assert b.url == "https://www.mubawab.ma/fr/a/7712399/bel-appartement-anfa-superieur"  # relative + query stripped
    assert b.price_mad == 1_980_000
    assert b.photos == ["https://www.mubawab.ma/photos/7712399_1.jpg"]  # lazy data-url, data: URI dropped

    c = by_id["7712400"]
    assert c.price_mad is None          # "Prix à consulter" -> null, never guessed
    assert c.surface_m2 == 97.5
    assert c.rooms is None and c.bedrooms == 3


def test_detail_page(settings):
    d = src(settings).parse_detail_page(fixture("detail_7712345.synthetic.html"))
    assert d.description.startswith("Magnifique appartement au 5ème étage")
    assert "Syndic 600 DH/mois" in d.description
    assert d.photos == [f"https://www.mubawab.ma/photos/7712345_{i}.jpg" for i in (1, 2, 3)]
    assert d.agency == "Agence Atlantic Immo"
    assert (d.surface_m2, d.rooms, d.bedrooms, d.bathrooms) == (92.0, 4, 3, 2)
    assert d.price_mad == 2_650_000     # from JSON-LD


def test_empty_page(settings):
    assert src(settings).parse_search_page(fixture("search_empty.synthetic.html")) == []


def test_field_parsers():
    assert parse_price_mad("2 500 000 DH") == 2_500_000
    assert parse_price_mad("2\xa0500\xa0000 MAD") == 2_500_000
    assert parse_price_mad("1.250.000,00 DH") == 1_250_000
    assert parse_price_mad("250 000 €") is None
    assert parse_price_mad("Prix à consulter") is None
    assert parse_price_mad(None) is None
    assert parse_surface("Surface 85m2") == 85.0
    assert parse_counts("3 pièces | 2 chambres")["rooms"] == 3


def test_zone_resolution(settings):
    z = settings.zones
    assert z.resolve("Aïn Diab à Casablanca") == "ain_diab"
    assert z.resolve("Ain Diab Extension, Casablanca") == "ain_diab"
    assert z.resolve("Anfa Supérieur à Casablanca") == "anfa"
    assert z.resolve("Maârif à Casablanca") is None
    assert z.resolve(None) is None
    assert z.resolve("Ain Diab, Casablanca") == "ain_diab"      # real Mubawab format (comma)
    assert z.resolve("Gauthier,Casablanca") == "gauthier"
