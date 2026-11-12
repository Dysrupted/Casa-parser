"""Pluggable source interface. Adding a site = one subclass + a block in sources.yaml."""
from __future__ import annotations

import logging
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from pydantic import BaseModel
from selectolax.lexbor import LexborHTMLParser as HTMLParser, LexborNode as Node

from radar.config import SourceConfig
from radar.http import FetchError, Fetcher

log = logging.getLogger(__name__)


class RawListing(BaseModel):
    """What a search-results card yields. Deterministic parsing only; unknown = None."""
    source: str
    external_id: str
    url: str
    title: str | None = None
    price_mad: int | None = None
    surface_m2: float | None = None
    rooms: int | None = None
    bedrooms: int | None = None
    bathrooms: int | None = None
    location_raw: str | None = None
    agency: str | None = None
    photos: list[str] = []


class DetailData(BaseModel):
    description: str | None = None
    photos: list[str] = []
    agency: str | None = None
    surface_m2: float | None = None
    rooms: int | None = None
    bedrooms: int | None = None
    bathrooms: int | None = None
    price_mad: int | None = None
    address_raw: str | None = None
    lat: float | None = None
    lon: float | None = None


@dataclass
class CrawlResult:
    listings: list[RawListing] = field(default_factory=list)
    complete: bool = True          # every search URL paginated to its natural end, no errors
    errors: list[str] = field(default_factory=list)
    pages_fetched: int = 0


class Source(ABC):
    name: str

    def __init__(self, cfg: SourceConfig, fetcher: Fetcher):
        self.cfg, self.fetcher = cfg, fetcher

    # Implemented per site -----------------------------------------------------
    @abstractmethod
    def parse_search_page(self, html: str) -> list[RawListing]: ...

    @abstractmethod
    def parse_detail_page(self, html: str) -> DetailData: ...

    def page_url(self, url: str, page: int) -> str:
        return url if page == 1 else self.cfg.pagination.format(url=url, page=page)

    # Generic crawl ------------------------------------------------------------
    def crawl(self) -> CrawlResult:
        """Paginate every search URL. Never raises: errors mark the crawl incomplete."""
        res = CrawlResult()
        seen: dict[str, RawListing] = {}
        for base in self.cfg.search_urls:
            prev_ids: set[str] | None = None
            for page in range(1, self.cfg.max_pages + 1):
                url = self.page_url(base, page)
                try:
                    cards = self.parse_search_page(self.fetcher.get(url, "search"))
                except FetchError as e:
                    res.errors.append(f"{url}: {type(e).__name__}: {e}")
                    res.complete = False
                    break
                except Exception as e:  # parser bug must not kill the run
                    log.exception("parse failure on %s", url)
                    res.errors.append(f"{url}: parse error {e!r}")
                    res.complete = False
                    break
                res.pages_fetched += 1
                ids = {c.external_id for c in cards}
                if not ids or ids == prev_ids:  # empty page or site repeats last page
                    break
                for c in cards:
                    seen.setdefault(c.external_id, c)
                prev_ids = ids
            else:
                res.complete = False  # hit max_pages: unseen listings may still exist
                res.errors.append(f"{base}: max_pages={self.cfg.max_pages} reached")
        res.listings = list(seen.values())
        return res

    def fetch_detail(self, url: str) -> tuple[str, DetailData]:
        html = self.fetcher.get(url, "detail")
        return html, self.parse_detail_page(html)

    # Selector helpers (selector chains from config) ----------------------------
    def sel(self, key: str) -> list[str]:
        return self.cfg.selectors.get(key, [])

    def first(self, node: HTMLParser | Node, key: str) -> Node | None:
        for css in self.sel(key):
            n = node.css_first(css)
            if n is not None:
                return n
        return None

    def all(self, node: HTMLParser | Node, key: str) -> list[Node]:
        for css in self.sel(key):
            ns = node.css(css)
            if ns:
                return ns
        return []

    def text(self, node: HTMLParser | Node, key: str) -> str | None:
        n = self.first(node, key)
        t = clean(n.text(separator=" ")) if n is not None else None
        return t or None


# -- deterministic field parsers (shared across sources) -------------------------
_WS = re.compile(r"\s+")


def clean(s: str | None) -> str:
    return _WS.sub(" ", (s or "").replace("\xa0", " ")).strip()


def parse_price_mad(text: str | None) -> int | None:
    """'2 500 000 DH' -> 2500000. Non-MAD currency or 'Prix à consulter' -> None."""
    if not text:
        return None
    t = clean(text).lower()
    if "€" in t or "eur" in t or "$" in t:
        return None
    digits = re.sub(r"[^\d]", "", re.sub(r"[.,]\d{1,2}\s*(dh|mad)?$", "", t))
    if len(digits) < 5:   # < 10 000 MAD is not an apartment asking price
        return None
    return int(digits)


_SURFACE = re.compile(r"(\d+(?:[.,]\d+)?)\s*m(?:²|2)", re.I)
_COUNTS = {
    "rooms": re.compile(r"(\d+)\s*(?:pi[eè]ces?|pcs?)\b", re.I),
    "bedrooms": re.compile(r"(\d+)\s*(?:chambres?|ch\.?)\b", re.I),
    "bathrooms": re.compile(r"(\d+)\s*(?:salles? de bains?|sdb|salles? d'eau)\b", re.I),
}


def parse_surface(text: str | None) -> float | None:
    m = _SURFACE.search(text or "")
    return float(m.group(1).replace(",", ".")) if m else None


def parse_counts(text: str | None) -> dict[str, int | None]:
    return {k: (int(m.group(1)) if (m := rx.search(text or "")) else None) for k, rx in _COUNTS.items()}
