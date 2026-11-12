from radar.sources.base import CrawlResult, DetailData, RawListing, Source
from radar.sources.mubawab import MubawabSource

REGISTRY: dict[str, type[Source]] = {MubawabSource.name: MubawabSource}

__all__ = ["CrawlResult", "DetailData", "RawListing", "Source", "REGISTRY"]
