import pytest

from radar.normalize.rules import extract_rules

DESC = ("Magnifique appartement au 5ème étage sur 7 avec ascenseur, vue mer partielle. "
        "Place de parking au sous-sol, terrasse de 12 m². Syndic 600 DH/mois. Orienté plein sud. "
        "Immeuble construit en 2015, très bon état, gardien.")


def test_full_description():
    r = extract_rules(DESC, agency=None)
    v = {k: x[0] for k, x in r.items()}
    assert v == {"elevator": True, "parking": True, "concierge": True, "sea_view": "partial",
                 "terrace_m2": 12.0, "floor": 5, "total_floors": 7, "syndic_fees_mad": 600,
                 "building_year": 2015, "condition": "good", "orientation": "sud"}
    for _, conf, span in r.values():
        assert span in DESC and 0 < conf <= 1


@pytest.mark.parametrize("text,field,value", [
    ("Appartement sans ascenseur", "elevator", False),
    ("Bel appartement en rez-de-chaussée", "floor", 0),
    ("1er étage", "floor", 1),
    ("vue sur la mer imprenable", "sea_view", "full"),
    ("appartement refait à neuf", "condition", "good"),
    ("résidence neuve, jamais habité", "condition", "new"),
    ("à rénover entièrement", "condition", "to_renovate"),
    ("syndic 7 200 DH par an", "syndic_fees_mad", 600),
    ("Vente directe par particulier", "agency_or_private", "private"),
])
def test_patterns(text, field, value):
    assert extract_rules(text)[field][0] == value


def test_agency_from_listing_field_and_nothing_invented():
    r = extract_rules("Bel appartement lumineux.", agency="Atlantic Immo")
    assert r == {"agency_or_private": ("agency", 0.8, "Atlantic Immo")}
    assert extract_rules(None) == {}
