"""Avito.ma — Next.js: listings are read from the embedded JSON (`__NEXT_DATA__`).

Paths into the JSON live in sources.yaml (`json:` block) as lists of alternatives, so a schema
change on Avito's side is a config edit. Each path is a list of keys/indexes.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any
from urllib.parse import urljoin

from selectolax.lexbor import LexborHTMLParser as HTMLParser

from radar.sources.base import (DetailData, RawListing, Source, clean, parse_counts, parse_price_mad,
                                parse_surface)

log = logging.getLogger(__name__)


def dig(node: Any, path: list) -> Any:
    """Follow keys/indexes; a "k=v" step selects the first list item whose field k equals v."""
    for k in path:
        if isinstance(k, str) and "=" in k and isinstance(node, list):
            f, v = k.split("=", 1)
            node = next((x for x in node if isinstance(x, dict) and str(x.get(f)) == v), None)
        elif isinstance(node, dict):
            node = node.get(k)
        elif isinstance(node, list) and isinstance(k, int) and -len(node) <= k < len(node):
            node = node[k]
        else:
            return None
        if node is None:
            return None
    return node


def first_of(node: Any, paths: list[list]) -> Any:
    for p in paths:
        v = dig(node, p)
        if v not in (None, "", []):
            return v
    return None


def _int_or_none(v) -> int | None:
    try:
        return int(float(v)) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


class AvitoSource(Source):
    name = "avito"

    def __init__(self, cfg, fetcher):
        super().__init__(cfg, fetcher)
        self._id = re.compile(cfg.id_pattern)

    def _blob(self, html: str) -> dict | None:
        tree = HTMLParser(html)
        node = tree.css_first(self.cfg.embedded_json["script"])
        if node is None:
            return None
        try:
            return json.loads(node.text())
        except ValueError:
            log.warning("avito: embedded JSON not parseable")
            return None

    def _f(self, ad: dict, key: str) -> Any:
        return first_of(ad, self.cfg.embedded_json["fields"].get(key, []))

    def _num(self, v) -> float | None:
        if v is None:
            return None
        if isinstance(v, (int, float)):
            return float(v)
        return parse_surface(f"{v} m²") if "m" not in str(v) else parse_surface(str(v))

    def parse_search_page(self, html: str) -> list[RawListing]:
        blob = self._blob(html)
        ads = first_of(blob, self.cfg.embedded_json["ads_paths"]) if blob else None
        out = []
        for ad in ads if isinstance(ads, list) else []:
            if not isinstance(ad, dict):
                continue
            ext, url = self._f(ad, "external_id"), self._f(ad, "url")
            if ext is None or url is None or not self._id.search(str(url)):
                continue  # e.g. immoneuf.avito.ma project units: other host, no ad id
            price = self._f(ad, "price")
            price_mad = int(price) if isinstance(price, (int, float)) and price >= 10_000 else parse_price_mad(
                str(price) if price is not None else None)
            params_text = " | ".join(str(x) for x in (self._f(ad, "rooms_text"), self._f(ad, "bedrooms_text")) if x)
            counts = parse_counts(params_text)
            rooms, beds = _int_or_none(self._f(ad, "rooms")), _int_or_none(self._f(ad, "bedrooms"))
            photos = self._f(ad, "photos") or []
            photos = [p if isinstance(p, str) else first_of(p, [["url"], ["src"]]) for p in photos]
            loc = self._f(ad, "location")
            out.append(RawListing(
                source=self.name, external_id=str(ext), url=urljoin(self.cfg.base_url, str(url)).split("?")[0],
                title=clean(self._f(ad, "title")) or None, price_mad=price_mad,
                surface_m2=self._num(self._f(ad, "surface")),
                rooms=rooms if rooms is not None else counts["rooms"],
                bedrooms=beds if beds is not None else counts["bedrooms"],
                bathrooms=_int_or_none(self._f(ad, "bathrooms")),
                description=clean(self._f(ad, "description")) or None,
                location_raw=", ".join(loc) if isinstance(loc, list) else (clean(str(loc)) if loc else None),
                agency=clean(self._f(ad, "agency")) or None,
                photos=[p for p in photos if isinstance(p, str)],
            ))
        return out

    def parse_detail_page(self, html: str) -> DetailData:
        blob = self._blob(html)
        ad = first_of(blob, self.cfg.embedded_json.get("detail_ad_paths", [])) if blob else None
        if not isinstance(ad, dict):
            return DetailData()
        photos = self._f(ad, "photos") or []
        lat, lon = self._f(ad, "lat"), self._f(ad, "lon")
        photos = [p if isinstance(p, str) else first_of(p, [["url"], ["src"]]) for p in photos]
        return DetailData(
            description=clean(self._f(ad, "description")) or None,
            photos=[p for p in photos if isinstance(p, str)],
            agency=clean(self._f(ad, "agency")) or None,
            surface_m2=self._num(self._f(ad, "surface")),
            address_raw=clean(self._f(ad, "address")) or None,
            lat=float(lat) if isinstance(lat, (int, float)) else None,
            lon=float(lon) if isinstance(lon, (int, float)) else None,
        )
