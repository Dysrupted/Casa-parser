"""Geo enrichment: coordinates + distance to the Corniche reference polyline."""
from __future__ import annotations

import json
import logging
import math
from urllib.parse import urlencode

from radar.config import GeoConfig, LatLon
from radar.http import FetchError, Fetcher

log = logging.getLogger(__name__)
EARTH_R = 6_371_000.0
PRECISION_RANK = {"page": 3, "address": 2, "zone": 1, None: 0}


def haversine_m(a: LatLon, b: LatLon) -> float:
    la1, lo1, la2, lo2 = map(math.radians, (*a, *b))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * EARTH_R * math.asin(math.sqrt(h))


def dist_to_polyline_m(p: LatLon, line: list[LatLon]) -> float:
    """Point-to-polyline distance on a local equirectangular projection (fine at city scale)."""
    k = math.cos(math.radians(p[0]))

    def xy(q: LatLon) -> tuple[float, float]:
        return (math.radians(q[1] - p[1]) * k * EARTH_R, math.radians(q[0] - p[0]) * EARTH_R)

    best = math.inf
    pts = [xy(q) for q in line]
    for (x1, y1), (x2, y2) in zip(pts, pts[1:]):
        dx, dy = x2 - x1, y2 - y1
        seg2 = dx * dx + dy * dy
        t = 0.0 if seg2 == 0 else max(0.0, min(1.0, -(x1 * dx + y1 * dy) / seg2))
        best = min(best, math.hypot(x1 + t * dx, y1 + t * dy))
    return best


class Geocoder:
    def __init__(self, cfg: GeoConfig, fetcher: Fetcher):
        self.cfg, self.fetcher = cfg, fetcher

    def geocode(self, address: str, zone: str | None) -> LatLon | None:
        if not self.cfg.nominatim.enabled or not address:
            return None
        q = ", ".join(x for x in (address, (zone or "").replace("_", " "), "Casablanca", "Maroc") if x)
        url = f"{self.cfg.nominatim.url}?{urlencode({'q': q, 'format': 'jsonv2', 'limit': 3, 'countrycodes': 'ma'})}"
        try:
            results = json.loads(self.fetcher.get(url, "api"))
        except (FetchError, ValueError) as e:
            log.warning("geocode failed for %r: %s", q, e)
            return None
        for r in results if isinstance(results, list) else []:
            try:
                p = (float(r["lat"]), float(r["lon"]))
            except (KeyError, TypeError, ValueError):
                continue
            if self.cfg.bbox.contains(p):
                return p
        return None

    def locate(self, page: LatLon | None, address: str | None, zone: str | None) -> tuple[LatLon | None, str | None]:
        """Best available coordinates and their precision label."""
        if page and self.cfg.bbox.contains(page):
            return page, "page"
        if address and (p := self.geocode(address, zone)):
            return p, "address"
        if zone and zone in self.cfg.zone_centroids:
            return tuple(self.cfg.zone_centroids[zone]), "zone"
        return None, None

    def corniche_distance_m(self, p: LatLon) -> float:
        return dist_to_polyline_m(p, [tuple(q) for q in self.cfg.corniche_polyline])
