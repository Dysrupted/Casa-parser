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
    """URL -> HTML map. Never touches the network. Unknown URL -> FetchError."""

    def __init__(self, pages: dict[str, str | Exception]):
        self.pages, self.calls = dict(pages), []

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
