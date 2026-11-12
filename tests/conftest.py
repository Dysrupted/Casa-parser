from __future__ import annotations

import shutil
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from radar.config import Settings
from radar.db import init_db, make_engine
from radar.http import FetchError

ROOT = Path(__file__).resolve().parent.parent
FIX = ROOT / "tests" / "fixtures" / "mubawab"
SEARCH = "https://www.mubawab.ma/fr/sd/casablanca-test/appartements-a-vendre"


def fixture(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


class FakeFetcher:
    """URL -> HTML (or bytes) map. Never touches the network. Unknown URL -> FetchError."""

    def __init__(self, pages: dict[str, str | bytes | Exception]):
        self.pages, self.calls = dict(pages), []

    def get_bytes(self, url: str, kind: str = "photo") -> bytes:
        self.calls.append((url, kind))
        v = self.pages.get(url)
        if not isinstance(v, bytes):
            raise FetchError(f"no bytes fixture for {url}")
        return v

    def get(self, url: str, kind: str) -> str:
        self.calls.append((url, kind))
        v = self.pages.get(url)
        if v is None:
            raise FetchError(f"no fixture for {url}")
        if isinstance(v, Exception):
            raise v
        return v


def default_pages() -> dict[str, str]:
    return {
        SEARCH: fixture("search_p1.synthetic.html"),
        f"{SEARCH}:p:2": fixture("search_p2.synthetic.html"),
        f"{SEARCH}:p:3": fixture("search_empty.synthetic.html"),
        "https://www.mubawab.ma/fr/a/7712345/appartement-vue-mer-ain-diab": fixture("detail_7712345.synthetic.html"),
        "https://www.mubawab.ma/fr/a/7712399/bel-appartement-anfa-superieur": "<html><body></body></html>",
        "https://www.mubawab.ma/fr/a/7712400/appartement-racine": "<html><body></body></html>",
        "https://www.mubawab.ma/fr/a/7712500/appartement-gauthier": "<html><body></body></html>",
        "https://open.er-api.com/v6/latest/EUR#2026-11-01": '{"rates": {"MAD": 10.9}}',
    }


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    cfg = tmp_path / "config"
    shutil.copytree(ROOT / "config", cfg)
    src = yaml.safe_load((cfg / "sources.yaml").read_text())
    src["sources"]["mubawab"]["search_urls"] = [SEARCH]
    src["sources"]["mubawab"]["max_pages"] = 5
    (cfg / "sources.yaml").write_text(yaml.safe_dump(src))
    return Settings(config_dir=cfg, data_dir=tmp_path / "data")


@pytest.fixture
def engine(settings: Settings):
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    eng = make_engine(settings.db_url)
    init_db(eng)
    return eng


class Clock:
    def __init__(self, t: datetime):
        self.t = t

    def __call__(self) -> datetime:
        return self.t


@pytest.fixture
def clock() -> Clock:
    return Clock(datetime(2026, 11, 1, 7, 0, tzinfo=timezone.utc))


def image_bytes(seed: int, size=(320, 240), fmt="JPEG") -> bytes:
    """Deterministic synthetic 'photo'. Same seed = same picture (any size/format)."""
    import io
    import random

    from PIL import Image, ImageDraw

    rnd = random.Random(seed)
    im = Image.new("RGB", (320, 240), (rnd.randint(0, 255), rnd.randint(0, 255), rnd.randint(0, 255)))
    d = ImageDraw.Draw(im)
    for _ in range(12):
        x, y = rnd.randint(0, 300), rnd.randint(0, 220)
        d.rectangle([x, y, x + rnd.randint(10, 120), y + rnd.randint(10, 90)],
                    fill=(rnd.randint(0, 255), rnd.randint(0, 255), rnd.randint(0, 255)))
    im = im.resize(size)
    buf = io.BytesIO()
    im.save(buf, format=fmt, quality=80)
    return buf.getvalue()


AVITO = "https://www.avito.ma/fr/test/appartements"
SAROUTY = "https://www.sarouty.ma/fr/acheter/casablanca/test.html"
AFIX, SFIX = ROOT / "tests" / "fixtures" / "avito", ROOT / "tests" / "fixtures" / "sarouty"


def multi_pages() -> dict:
    p = default_pages()
    p.update({
        AVITO: (AFIX / "search_p1.synthetic.html").read_text(),
        f"{AVITO}?o=2": (AFIX / "search_empty.synthetic.html").read_text(),
        "https://www.avito.ma/fr/ain_diab/appartements/Appartement_vue_mer_55500001.htm":
            (AFIX / "detail_55500001.synthetic.html").read_text(),
        "https://www.avito.ma/fr/anfa/appartements/Grand_appartement_Anfa_55500002.htm":
            (AFIX / "detail_55500002.synthetic.html").read_text(),
        SAROUTY: (SFIX / "search_p1.synthetic.html").read_text(),
        f"{SAROUTY}?page=2": (SFIX / "search_empty.synthetic.html").read_text(),
        "https://www.sarouty.ma/fr/acheter/casablanca/appartement-standing-gauthier-6600001.html": "<html></html>",
        "https://www.sarouty.ma/fr/acheter/casablanca/appartement-racine-6600002.html": "<html></html>",
        # photos: the Gauthier flat is the same picture on both sites (re-encoded, resized)
        "https://www.mubawab.ma/photos/7712500_1.jpg": image_bytes(1),
        "https://www.sarouty.ma/img/s1.jpg": image_bytes(1, size=(640, 480), fmt="PNG"),
        "https://www.sarouty.ma/img/s2.jpg": image_bytes(2),
        "https://content.avito.ma/a1.jpg": image_bytes(3),
        "https://content.avito.ma/a2.jpg": image_bytes(4),
    })
    for i in (1, 2, 3):
        p[f"https://www.mubawab.ma/photos/7712345_{i}.jpg"] = image_bytes(10 + i)
    p["https://www.mubawab.ma/photos/7712399_1.jpg"] = image_bytes(20)
    return p


@pytest.fixture
def multi_settings(settings):
    settings.sources["avito"].search_urls = [AVITO]
    settings.sources["sarouty"].search_urls = [SAROUTY]
    settings.sources["sarouty"].enabled = True   # synthetic fixture; disabled in prod config
    settings.sources["avito"].fetch_details = True   # synthetic scenario has the text on the detail page
    return settings
