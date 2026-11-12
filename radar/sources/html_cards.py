"""Generic server-rendered listing site: search cards + detail page, all selectors in config."""
from __future__ import annotations

import json
import re
from urllib.parse import urljoin

from selectolax.lexbor import LexborHTMLParser as HTMLParser

from radar.sources.base import (
    DetailData,
    RawListing,
    Source,
    clean,
    parse_counts,
    parse_price_mad,
    parse_surface,
)

class HtmlCardSource(Source):
    """Subclass and set `name`; behaviour comes from the source's block in sources.yaml."""
    name = "html"

    def __init__(self, cfg, fetcher):
        super().__init__(cfg, fetcher)
        self._id = re.compile(cfg.id_pattern)

    def _abs(self, href: str | None) -> str | None:
        return urljoin(self.cfg.base_url, href) if href else None

    def _card_url(self, card) -> str | None:
        for attr in self.sel("card_url_attr"):
            if v := card.attributes.get(attr):
                return self._abs(v)
        a = self.first(card, "card_url")
        return self._abs(a.attributes.get("href")) if a is not None else None

    def parse_search_page(self, html: str) -> list[RawListing]:
        tree = HTMLParser(html)
        out: list[RawListing] = []
        for card in self.all(tree, "card"):
            url = self._card_url(card)
            m = self._id.search(url or "")
            if not m:
                continue  # ads / promoted blocks without a listing id
            feats = " | ".join(clean(n.text()) for n in self.all(card, "features"))
            counts = parse_counts(feats)
            photos = [
                self._abs(n.attributes.get("data-url") or n.attributes.get("data-src") or n.attributes.get("src"))
                for n in self.all(card, "card_photo")
            ]
            agency_node = self.first(card, "agency")
            agency = None
            if agency_node is not None:
                agency = clean(agency_node.attributes.get("alt") or agency_node.text()) or None
            out.append(
                RawListing(
                    source=self.name,
                    external_id=m.group(1),
                    url=url.split("?")[0],
                    title=self.text(card, "title"),
                    price_mad=parse_price_mad(self.text(card, "price")),
                    surface_m2=parse_surface(feats),
                    location_raw=self.text(card, "location"),
                    agency=agency,
                    photos=[p for p in dict.fromkeys(photos) if p and not p.startswith("data:")],
                    **counts,
                )
            )
        return out

    def parse_detail_page(self, html: str) -> DetailData:
        tree = HTMLParser(html)
        desc_nodes = self.all(tree, "detail_description")
        description = clean(" ".join(n.text(separator=" ") for n in desc_nodes)) or None
        feats = " | ".join(clean(n.text()) for n in self.all(tree, "detail_features"))
        photos = [
            self._abs(n.attributes.get("data-src") or n.attributes.get("src"))
            for n in self.all(tree, "detail_photos")
        ]
        data = DetailData(
            description=description,
            photos=[p for p in dict.fromkeys(photos) if p and not p.startswith("data:")],
            agency=self.text(tree, "detail_agency"),
            address_raw=self.text(tree, "detail_address"),
            surface_m2=parse_surface(feats),
            **parse_counts(feats),
        )
        data.lat, data.lon = self._coords(tree)
        return _merge_jsonld(tree, data)

    def _coords(self, tree) -> tuple[float | None, float | None]:
        pairs = [("lat", "lon"), ("lat", "lng"), ("data-lat", "data-lng"), ("data-lat", "data-lon"),
                 ("data-latitude", "data-longitude")]
        for node in self.all(tree, "detail_geo"):
            for a, b in pairs:
                try:
                    lat, lon = float(node.attributes[a]), float(node.attributes[b])
                except (KeyError, TypeError, ValueError):
                    continue
                if lat and lon:
                    return lat, lon
        return None, None


def _merge_jsonld(tree: HTMLParser, data: DetailData) -> DetailData:
    """Fill gaps from schema.org JSON-LD if the page has it. Never overrides HTML values."""
    for node in tree.css('script[type="application/ld+json"]'):
        try:
            blob = json.loads(node.text())
        except (ValueError, TypeError):
            continue
        for item in blob if isinstance(blob, list) else [blob]:
            if not isinstance(item, dict):
                continue
            if data.description is None and isinstance(item.get("description"), str):
                data.description = clean(item["description"]) or None
            offers = item.get("offers")
            if data.price_mad is None and isinstance(offers, dict):
                if str(offers.get("priceCurrency", "MAD")).upper() == "MAD" and offers.get("price"):
                    try:
                        data.price_mad = int(float(offers["price"]))
                    except (TypeError, ValueError):
                        pass
            if not data.photos and item.get("image"):
                imgs = item["image"] if isinstance(item["image"], list) else [item["image"]]
                data.photos = [i for i in imgs if isinstance(i, str)]
    return data
