from radar.sources.avito import AvitoSource
from radar.sources.base import CrawlResult, DetailData, RawListing, Source
from radar.sources.mubawab import MubawabSource
from radar.sources.sarouty import SaroutySource

REGISTRY: dict[str, type[Source]] = {s.name: s for s in (MubawabSource, AvitoSource, SaroutySource)}

__all__ = ["CrawlResult", "DetailData", "RawListing", "Source", "REGISTRY"]
