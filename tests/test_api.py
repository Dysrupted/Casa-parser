import base64

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from radar.api.app import create_app, svg_line
from radar.models import Listing, UserNote
from radar.simulation import FinancingParams
from tests.test_dedupe import run_all
from tests.test_simulation import SYNTH


@pytest.fixture
def populated(multi_settings, engine, clock):
    multi_settings.financing = FinancingParams(**SYNTH)
    run_all(multi_settings, engine, clock)
    return multi_settings, engine


@pytest.fixture
def client(populated, monkeypatch):
    for k in ("RADAR_DASHBOARD_PASSWORD", "RADAR_API_TOKEN", "RADAR_SHARE_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    s, engine = populated
    return TestClient(create_app(s, engine))


def pid_of(engine, ext):
    with Session(engine) as db:
        return db.exec(select(Listing).where(Listing.external_id == ext)).one().property_id


def test_svg_line():
    assert svg_line([{"v": 1}], "v") == ""
    svg = svg_line([{"v": 1}, {"v": None}, {"v": 3}], "v")
    assert svg.startswith("<svg") and "M" in svg and "L" in svg


def test_dashboard_pages_render(client, populated):
    _, engine = populated
    r = client.get("/")
    assert r.status_code == 200 and "Casa Radar" in r.text and "Appartement vue mer" in r.text
    assert 'name="viewport"' in r.text                                   # phone-friendly
    pid = pid_of(engine, "7712345")
    d = client.get(f"/p/{pid}")
    assert d.status_code == 200
    for needle in ("Why this score", "Purchase simulation", "Cash needed", "Price history", "mubawab ↗", "avito ↗"):
        assert needle in d.text
    assert client.get("/market").status_code == 200 and "Ain Diab" in client.get("/market").text
    assert client.get("/p/99999").status_code == 404
    assert client.get("/?zone=anfa&sort=price&all=1").status_code == 200


def test_api_listings_and_market(client):
    rows = client.get("/api/listings?all=1").json()
    assert rows and {"id", "score", "price_vs_zone", "dom", "sources"} <= set(rows[0])
    assert [r["score"] for r in rows] == sorted((r["score"] for r in rows), reverse=True)
    merged = [r for r in rows if len(r["sources"]) > 1]
    assert merged, "deduped flats show several sources"
    anfa = client.get("/api/listings?zone=anfa").json()
    assert all(r["zone"] == "anfa" for r in anfa)
    m = client.get("/api/market/ain_diab").json()
    assert m["zone"] == "ain_diab" and m["current"]["median_m2"] > 0 and m["inventory"] >= 1
    assert client.get("/api/market/narnia").status_code == 404


def test_goal_lifecycle(client, populated):
    _, engine = populated
    assert client.get("/api/goal").json() == {"set": False}
    pid = pid_of(engine, "7712345")
    g = client.put("/api/goal", json={"property_id": pid}).json()
    assert g["set"] and g["type"] == "listing" and g["property_id"] == pid
    assert g["simulation_ok"] and g["total_cash_needed_mad"] > 0 and g["monthly_payment_mad"] > 0
    g = client.put("/api/goal", json={"manual_price_mad": 2_000_000}).json()
    assert g["type"] == "manual" and g["price_mad"] == 2_000_000 and g["total_cash_needed_mad"] > 0
    assert client.put("/api/goal", json={}).status_code == 422
    assert client.put("/api/goal", json={"property_id": 424242}).status_code == 404
    assert client.post(f"/p/{pid}/goal", follow_redirects=False).status_code == 303
    assert client.get("/api/goal").json()["property_id"] == pid


def test_notes_via_form_and_api(client, populated):
    _, engine = populated
    pid = pid_of(engine, "7712399")
    r = client.post(f"/p/{pid}/note", data={"status": "visited", "note": "nice light", "reject_reason": "",
                                            "actual_sale_price_mad": "1900000"}, follow_redirects=False)
    assert r.status_code == 303
    with Session(engine) as db:
        n = db.get(UserNote, pid)
        assert n.status == "visited" and n.note == "nice light" and n.actual_sale_price_mad == 1_900_000
    assert any(r["id"] == pid for r in client.get("/api/listings?watch=true&all=1").json())
    client.put(f"/api/properties/{pid}/note", json={"status": "rejected", "reject_reason": "ground floor"})
    assert not any(r["id"] == pid for r in client.get("/api/listings?watch=true").json())
    client.delete(f"/api/properties/{pid}/note")
    with Session(engine) as db:
        assert db.get(UserNote, pid) is None


def test_auth_password_token_and_share(populated, monkeypatch):
    s, engine = populated
    monkeypatch.setenv("RADAR_DASHBOARD_PASSWORD", "pw")
    monkeypatch.setenv("RADAR_API_TOKEN", "tok")
    monkeypatch.setenv("RADAR_SHARE_TOKEN", "fam")
    c = TestClient(create_app(s, engine))
    assert c.get("/").status_code == 401 and "Basic" in c.get("/").headers["www-authenticate"]
    basic = {"Authorization": "Basic " + base64.b64encode(b"me:pw").decode()}
    assert c.get("/", headers=basic).status_code == 200
    assert c.get("/", headers={"Authorization": "Basic " + base64.b64encode(b"me:no").decode()}).status_code == 401
    assert c.get("/api/goal", headers={"Authorization": "Bearer tok"}).status_code == 200
    assert c.get("/", headers={"Authorization": "Bearer tok"}).status_code == 401      # token is API-only
    shared = c.get("/?share=fam")
    assert shared.status_code == 200 and "Read-only view" in shared.text
    assert c.get("/market").status_code == 200                                         # cookie remembered
    pid = pid_of(engine, "7712345")
    assert c.post(f"/p/{pid}/note", data={"status": "watching"}).status_code == 403    # share can't write
    assert c.get("/api/goal?share=fam").status_code == 401                             # share can't use API
    assert c.get("/healthz").status_code == 200


def test_static_site_build(populated, tmp_path, monkeypatch):
    import json
    import re

    from typer.testing import CliRunner

    from radar.cli import app
    s, _ = populated
    monkeypatch.setenv("RADAR_CONFIG_DIR", str(s.config_dir))
    monkeypatch.setenv("RADAR_DATA_DIR", str(s.data_dir))
    res = CliRunner().invoke(app, ["site", "--out", str(tmp_path / "site")])
    assert res.exit_code == 0, res.output
    html = (tmp_path / "site" / "index.html").read_text()
    data = json.loads(re.search(r'<script id="data" type="application/json">(.*?)</script>', html, re.S).group(1))
    assert data["listings"] and {"url", "price_mad", "score", "in_profile", "reasons"} <= set(data["listings"][0])
    assert "description" not in data["listings"][0]          # public page: no copied ad text or photos
    assert (tmp_path / "site" / ".nojekyll").exists()
